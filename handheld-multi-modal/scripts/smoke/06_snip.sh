#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$root/scripts/smoke/_dlsps_lib.sh"

# This test proves the application-owned gvapython crop callback
# (scripts/dlsps/crop_candidates.py) actually produces per-tracked-object
# snip files through the SHIPPED config/dlsps/{cpu,gpu,npu}/config.json +
# compose/handheld-multi-modal.yaml wiring — not a scripts docker-cp'd into a
# bare container. See scripts/dlsps/README.md.
#
# Requires: the mounted config.json declares a `cropcandidates`-named
# gvapython element and a `crop-config` parameter mapped to its `kwarg`
# property; compose mounts scripts/dlsps -> /opt/handheld-crops (ro),
# config/dlsps/crops -> /opt/handheld-crops-config (ro), and the writable
# store root -> /opt/handheld-crops-output. Crops land in the smoke stream's
# own store (<root>/$SMOKE_STREAM_ID), never a live camera's.
: "${CROPS_HOST_DIR:=${ASSET_ROOT:-$root}/data/dlsps/crops/$SMOKE_STREAM_ID}"
: "${CROP_CONFIG_CONTAINER_PATH:=/opt/handheld-crops-config/template.json}"

dlsps_require_container

if [ ! -d "$(dirname "$CROPS_HOST_DIR")" ]; then
  echo "BLOCKED: $(dirname "$CROPS_HOST_DIR") does not exist; run make setup first." >&2
  exit 2
fi

# The store may be unreadable to this user (UID 1999, mode 2700); count inside
# the container, which owns it.
count_crops() {
  docker exec "$DLSPS_CONTAINER" sh -c "ls /opt/handheld-crops-output/$SMOKE_STREAM_ID/*.jpg 2>/dev/null | wc -l"
}
before_count=$(count_crops)

echo "Starting NPU pipeline with crop callback enabled..." >&2
DLSPS_PIPELINE_NAME=handheld_npu_detect
resp=$(curl --fail --silent --show-error --max-time 15 \
  -X POST "$DLSPS_REST/pipelines/user_defined_pipelines/$DLSPS_PIPELINE_NAME" \
  -H 'Content-Type: application/json' \
  -d "{\"source\": {\"uri\": \"$DLSPS_DEMO_URI\", \"type\": \"uri\"}, \"destination\": {\"metadata\": {\"type\": \"mqtt\", \"publish_frame\": false, \"topic\": \"detections/live\"}}, \"parameters\": $(dlsps_smoke_parameters "$CROP_CONFIG_CONTAINER_PATH")}")
iid=$(python3 -c "import json,sys; print(json.loads(sys.argv[1]))" "$resp")
echo "Started instance $iid" >&2
trap 'dlsps_stop_pipeline "$iid" || echo "WARN: instance $iid may still be running, check manually" >&2' EXIT

dlsps_wait_pipeline "$iid" 20 5 >/dev/null

echo "Letting the crop producer admit tracked objects for 15s..." >&2
sleep 15

after_count=$(count_crops)
new_crops=$((after_count - before_count))

if [ "$new_crops" -le 0 ]; then
  echo "FAIL: no new .jpg crop files appeared in $CROPS_HOST_DIR (before=$before_count, after=$after_count). Check dlsps container logs for gvapython 'Error creating Python callback' (bad kwarg/config path) or a stale advisory .lock in the output dir requiring a dlsps container restart." >&2
  exit 1
fi

# Each crop must have a matching JSON sidecar (crop_ready event with
# tracker/label/confidence/rect metadata) — a lone .jpg without its sidecar
# indicates the producer wrote the image but failed before its durable
# JSON event, which the real Backend consumer would treat as incomplete.
orphans=$(docker exec "$DLSPS_CONTAINER" sh -c "cd /opt/handheld-crops-output/$SMOKE_STREAM_ID && for f in *.jpg; do [ -e \"\$f\" ] && [ ! -f \"\${f%.jpg}.json\" ] && echo x; done | wc -l")
if [ "$orphans" -gt 0 ]; then
  echo "FAIL: $orphans crop .jpg file(s) in $CROPS_HOST_DIR have no matching .json sidecar." >&2
  exit 1
fi

echo "PASS: crop callback (scripts/dlsps/crop_candidates.py, wired via shipped config.json + Compose) produced $new_crops new matched .jpg+.json snip pairs in $CROPS_HOST_DIR during instance $iid."
