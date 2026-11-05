#!/usr/bin/env bash
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Start / stop the camera side on machine-2 and machine-3 from machine-1.
#
#   ./deploy.sh up       # RTSP streams (unless STREAMS=0), then a DLSPS container per machine
#   ./deploy.sh down
#   ./deploy.sh status
#
# Everything is controlled over the lab network (ssh to the lab addresses); only the MQTT
# results travel over the TSN VLAN, to MQTT_HOST:MQTT_PORT on machine-1.
#
# Environment overrides:
#   EDGE_HOSTS="tsndemo@10.106.147.105:Cam_x1_0 tsndemo@10.106.147.201:Cam_x2_0"
#   MQTT_HOST=192.168.127.11  MQTT_PORT=1884  STREAMS=1  REMOTE_DIR=camera-pipeline
#   IMAGE=intel/dlstreamer-pipeline-server:2026.3.0-ubuntu24-rc1
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/../../lib/label.sh"
ROOT=$(cd "$HERE/../../.." && pwd)
EDGE_HOSTS=${EDGE_HOSTS:-"tsndemo@10.106.147.105:Cam_x1_0 tsndemo@10.106.147.201:Cam_x2_0"}
MQTT_HOST=${MQTT_HOST:-192.168.127.11}
MQTT_PORT=${MQTT_PORT:-1884}
STREAMS=${STREAMS:-1}
REMOTE_DIR=${REMOTE_DIR:-camera-pipeline}
IMAGE=${IMAGE:-intel/dlstreamer-pipeline-server:2026.3.0-ubuntu24-rc1}
MODELS=$ROOT/src/data/models

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new)
die() { echo "error: $*" >&2; exit 1; }

each() { local fn=$1 pair; for pair in $EDGE_HOSTS; do "$fn" "${pair%:*}" "${pair##*:}"; done; }

each_parallel() { local fn=$1 pair; for pair in $EDGE_HOSTS; do "$fn" "${pair%:*}" "${pair##*:}" & done; wait; }

streams() {
  [ "$STREAMS" = 1 ] || return 0
  STREAM_HOSTS="$EDGE_HOSTS" "$HERE/../streams/start-synced-streams.sh" "$1"
}

prepare() {
  local host=$1 cam=$2 rtsp="${RTSP_URL:-rtsp://127.0.0.1:8554/$2}"
  echo "[$(label "$host")] copying files for $cam"
  "${SSH[@]}" "$host" "mkdir -p $REMOTE_DIR/models" || die "$host: ssh failed"
  sed -e "s#@RTSP_URL@#$rtsp#" -e "s#@TOPIC@#tsn_demo/$cam/inference#" "$HERE/config.template.json" \
    | "${SSH[@]}" "$host" "cat > $REMOTE_DIR/config.json"
  scp -q -o BatchMode=yes "$HERE/docker-compose.yml" "$HERE/latency_stamp.py" "$host:$REMOTE_DIR/"
  tar -C "$MODELS" -cf - . | "${SSH[@]}" "$host" "tar -C $REMOTE_DIR/models -xf -"
  "${SSH[@]}" "$host" "printf 'MQTT_HOST=%s\nMQTT_PORT=%s\nDLSTREAMER_PIPELINE_SERVER_IMAGE=%s\n' '$MQTT_HOST' '$MQTT_PORT' '$IMAGE' > $REMOTE_DIR/.env"
  # `docker compose pull` always asks the registry; only pull when the image is not on the machine
  "${SSH[@]}" "$host" "docker image inspect '$IMAGE' >/dev/null 2>&1" \
    && echo "[$(label "$host")] image already present, not pulling" \
    || { echo "[$(label "$host")] pulling image"; "${SSH[@]}" "$host" "cd $REMOTE_DIR && docker compose pull -q"; }
}

launch() {
  local host=$1
  "${SSH[@]}" "$host" "cd $REMOTE_DIR && docker compose down -t 2 >/dev/null 2>&1; docker compose up -d" >/dev/null 2>&1 \
    && echo "[$(label "$host")] started" || echo "[$(label "$host")] FAILED to start" >&2
}

down_one() { "${SSH[@]}" "$1" "cd $REMOTE_DIR && docker compose down -t 2" 2>&1 | tail -1 | sed "s/^/[$(label "$1")] /"; }

status_one() {
  echo "== $(label "$1") ($2)"
  "${SSH[@]}" "$1" "cd $REMOTE_DIR && docker compose ps -a --format 'table {{.Service}}\t{{.Status}}'; docker compose logs --no-log-prefix --tail 3 2>&1 | cut -c1-180" || true
}

case "${1:-}" in
  up)     streams up; each prepare; each launch ;;
  down)   each_parallel down_one; streams down ;;
  status) each status_one ;;
  *) sed -n '4,12p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
