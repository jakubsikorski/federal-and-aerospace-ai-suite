#!/usr/bin/env bash
# Shared helpers for DLSPS smoke scripts. Not a standalone entry point.
_dlsps_lib_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
: "${COMPOSE_FILE:=$_dlsps_lib_root/compose/bootstrap-compose.yaml}"
: "${DLSPS_REST:=http://127.0.0.1:8080}"
: "${DLSPS_PIPELINE_NAME:=handheld_smoke}"
: "${DLSPS_DEMO_URI:=file:///home/pipeline-server/resources/videos/classroom.avi}"
: "${DLSPS_MODEL:=/home/pipeline-server/yolo_models/yolo11s/INT8/yolo11s.xml}"

# dlsps_resolve_container <service> — resolve the real container name via
# Compose service identity (docker compose ps -a), not a guessed
# container_name (dlsps/mediamtx/mqtt have none in compose/bootstrap-compose.yaml,
# so their actual names depend on the Compose project name, e.g.
# handheld-bootstrap-dlsps-1, not the previously hardcoded handheld-bootstrap-dlsps).
# An explicit <SERVICE>_CONTAINER override (e.g. DLSPS_CONTAINER=foo) still wins,
# for non-Compose setups.
dlsps_resolve_container() {
  local service="$1"
  local override_var override
  override_var="$(echo "$service" | tr '[:lower:]-' '[:upper:]_')_CONTAINER"
  override="${!override_var:-}"
  if [ -n "$override" ]; then
    echo "$override"
    return 0
  fi
  local name
  name=$(docker compose -f "$COMPOSE_FILE" ps -a --format '{{.Name}}' "$service" 2>/dev/null | head -1)
  if [ -z "$name" ]; then
    echo "BLOCKED: could not resolve a container for Compose service '$service' via $COMPOSE_FILE; start it first (docker compose -f $COMPOSE_FILE up -d $service)." >&2
    exit 2
  fi
  echo "$name"
}

: "${DLSPS_CONTAINER:=$(dlsps_resolve_container dlsps)}"

# dlsps_require_container [container] — assert the container is actually
# running. A successful `docker inspect` only proves the container exists;
# its State.Running can still print "false". Check the printed boolean, not
# just the command's exit code.
dlsps_require_container() {
  local container="${1:-$DLSPS_CONTAINER}"
  local running
  running=$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || echo "false")
  if [ "$running" != "true" ]; then
    echo "BLOCKED: container $container is not running (State.Running=$running); start it via docker compose -f $COMPOSE_FILE up -d dlsps with the desired config/dlsps/{cpu,gpu,npu}/config.json mounted first." >&2
    exit 2
  fi
}

# dlsps_start_pipeline <device> [extra_destination_json]
# Prints the real pipeline instance id returned by DLSPS on stdout.
# Callers MUST capture this id and use it with dlsps_wait_pipeline /
# dlsps_stop_pipeline — never assume "a pipeline is running" proves this
# specific call succeeded, and never sweep/stop pipelines this call didn't
# start (an unrelated operator pipeline must survive every smoke script).
dlsps_start_pipeline() {
  local device="$1" extra_dest="${2:-}"
  local dest
  if [ -n "$extra_dest" ]; then
    dest="$extra_dest"
  else
    dest='{"frame": {"type": "webrtc", "peer-id": "annotated", "overlay": true}}'
  fi
  local resp
  resp=$(curl --fail --silent --show-error --max-time 15 \
    -X POST "$DLSPS_REST/pipelines/user_defined_pipelines/$DLSPS_PIPELINE_NAME" \
    -H 'Content-Type: application/json' \
    -d "{\"source\": {\"uri\": \"$DLSPS_DEMO_URI\", \"type\": \"uri\"}, \"destination\": $dest, \"parameters\": {\"detection-properties\": {\"model\": \"$DLSPS_MODEL\", \"device\": \"$device\"}}}")
  python3 -c "import json,sys; print(json.loads(sys.argv[1]))" "$resp"
}

# dlsps_stop_pipeline <instance_id> — stop/delete only this specific
# instance (GET/DELETE /pipelines/{id}, confirmed real endpoints). Never
# calls the old blanket "stop every RUNNING pipeline" sweep.
# Surfaces cleanup failures (HTTP or connection errors) instead of silently
# swallowing them with `|| true` — a smoke run must not report PASS while
# its own test-started instance is left running. Callers should log a loud
# warning on nonzero return (see trap usage in 02/03/04/05_*.sh); this
# function itself never calls `exit`, so it stays safe to invoke from an
# EXIT trap without corrupting the script's real exit code.
dlsps_stop_pipeline() {
  local id="${1:-}"
  [ -n "$id" ] || return 0
  local http_code
  http_code=$(curl --silent --max-time 10 -o /dev/null -w '%{http_code}' -X DELETE "$DLSPS_REST/pipelines/$id" 2>/dev/null) || {
    echo "WARN: cleanup request for instance $id failed (connection error); instance may still be running" >&2
    return 1
  }
  if [ "$http_code" != "200" ]; then
    echo "WARN: cleanup request for instance $id returned HTTP $http_code; instance may still be running" >&2
    return 1
  fi
  return 0
}

# dlsps_wait_pipeline <instance_id> [seconds] [min_fps]
# Polls THIS instance's own /pipelines/{id}/status (not container logs,
# which can show a stale run or an unrelated pipeline's fps). Passes only
# once two samples show elapsed_time advancing and frame_fps >= min_fps;
# fails immediately if the instance leaves RUNNING/QUEUED state. Prints the
# last real status JSON on success.
dlsps_wait_pipeline() {
  local id="$1" wait_s="${2:-15}" min_fps="${3:-5}"
  local deadline=$((SECONDS + wait_s))
  local prev_elapsed="" status state fps elapsed
  while [ "$SECONDS" -lt "$deadline" ]; do
    status=$(curl --silent --max-time 5 "$DLSPS_REST/pipelines/$id/status" 2>/dev/null || true)
    state=$(echo "$status" | python3 -c "import json,sys
try:
    print(json.load(sys.stdin).get('state',''))
except Exception:
    print('')" 2>/dev/null)
    if [ "$state" != "RUNNING" ] && [ "$state" != "QUEUED" ]; then
      echo "FAIL: instance $id left RUNNING state (state='$state', resp=$status)" >&2
      return 1
    fi
    fps=$(echo "$status" | python3 -c "import json,sys; print(json.load(sys.stdin).get('frame_fps',0))" 2>/dev/null || echo 0)
    elapsed=$(echo "$status" | python3 -c "import json,sys; print(json.load(sys.stdin).get('elapsed_time',0))" 2>/dev/null || echo 0)
    if [ -n "$prev_elapsed" ] && python3 -c "
import sys
elapsed, prev, fps, min_fps = float('$elapsed'), float('$prev_elapsed'), float('$fps'), float('$min_fps')
sys.exit(0 if elapsed > prev and fps >= min_fps else 1)"; then
      echo "$status"
      return 0
    fi
    prev_elapsed="$elapsed"
    sleep 2
  done
  echo "FAIL: instance $id never reached >= ${min_fps}fps with advancing elapsed_time within ${wait_s}s (last=$status)" >&2
  return 1
}
