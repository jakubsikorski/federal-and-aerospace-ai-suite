#!/usr/bin/env bash
# Start / stop synchronized RTSP "cameras" on several machines from this one.
#
#   ./start-synced-streams.sh up       # copy files, start all at one common time
#   ./start-synced-streams.sh status   # container state + recent log per machine
#   ./start-synced-streams.sh down     # stop and remove the containers
#
# Each machine runs docker-compose.yml (mediamtx + ffmpeg) for one camera. All publishers
# wait for the same epoch second T on their PTP-synced system clock, then loop their
# file in lockstep. A consumer can connect at any time; the SEI frame number in the
# stream identifies each frame.
#
# Environment overrides:
#   STREAM_HOSTS="tsn-machine-2:Cam_x1_0 tsn-machine-3:Cam_x2_0"   host:camera pairs
#   MEDIA_DIR=<dir with <cam>_1k_sei.ts>   LEAD=20 (seconds before start)
#   VLAN_IF=tsn0.1 (interface whose IP is printed)   RTSP_PORT=8554   REMOTE_DIR=synced-stream
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/../../lib/label.sh"
STREAM_HOSTS=${STREAM_HOSTS:-"tsn-machine-2:Cam_x1_0 tsn-machine-3:Cam_x2_0"}
MEDIA_DIR=${MEDIA_DIR:-$HERE/../../data/media}
LEAD=${LEAD:-20}
VLAN_IF=${VLAN_IF:-tsn0.1}
RTSP_PORT=${RTSP_PORT:-8554}
REMOTE_DIR=${REMOTE_DIR:-synced-stream}
IMAGE=${IMAGE:-bluenviron/mediamtx:1.19.2-ffmpeg}

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=5)
die() { echo "error: $*" >&2; exit 1; }

each() {  # each <fn>: run fn <host> <cam> for every pair
  local fn=$1 pair
  for pair in $STREAM_HOSTS; do "$fn" "${pair%%:*}" "${pair##*:}"; done
}

each_parallel() {  # same as each, but all hosts at once
  local fn=$1 pair
  for pair in $STREAM_HOSTS; do "$fn" "${pair%%:*}" "${pair##*:}" & done
  wait
}

prepare() {
  local host=$1 cam=$2 file="${2}_1k_sei.ts"
  [ -r "$MEDIA_DIR/$file" ] || die "missing $MEDIA_DIR/$file"
  echo "[$(label "$host")] checking ssh + docker"
  "${SSH[@]}" "$host" 'docker compose version >/dev/null' || die "$host: ssh or docker compose not working"
  echo "[$(label "$host")] copying compose files"
  "${SSH[@]}" "$host" "mkdir -p $REMOTE_DIR/media"
  scp -q -o BatchMode=yes "$HERE/docker-compose.yml" "$HERE/entrypoint.sh" "$host:$REMOTE_DIR/"
  # copy the video only when the remote copy is missing or has a different size
  local want have
  want=$(stat -c %s "$MEDIA_DIR/$file")
  have=$("${SSH[@]}" "$host" "stat -c %s $REMOTE_DIR/media/$file 2>/dev/null || echo 0")
  if [ "$want" != "$have" ]; then
    echo "[$(label "$host")] copying $file"
    scp -q -o BatchMode=yes "$MEDIA_DIR/$file" "$host:$REMOTE_DIR/media/"
  fi
  "${SSH[@]}" "$host" "printf 'CAM=%s\nIMAGE=%s\nRTSP_PORT=%s\n' '$cam' '$IMAGE' '$RTSP_PORT' > $REMOTE_DIR/.env"
}

launch() {
  local host=$1
  # stop any previous run first so ports are free
  "${SSH[@]}" "$host" "cd $REMOTE_DIR && docker compose down -t 2 >/dev/null 2>&1; START_AT=$T docker compose up -d" \
    >/dev/null 2>&1 || echo "[$(label "$host")] FAILED to start" >&2
}

report() {
  local host=$1 cam=$2 ip
  ip=$("${SSH[@]}" "$host" "ip -4 -o addr show $VLAN_IF | awk '{print \$4}' | cut -d/ -f1")
  echo "  rtsp://${ip:-$host}:$RTSP_PORT/$cam"
  "${SSH[@]}" "$host" "cd $REMOTE_DIR && docker compose logs --no-log-prefix --tail 4 2>&1 | grep -E 'WARNING|ERROR|publishing'" || true
}

cmd_up() {
  each prepare
  T=$(( $(date +%s) + LEAD ))
  echo "Start time T=$T ($(date -d "@$T" +%T)), in ${LEAD}s"
  local pids=() pair
  for pair in $STREAM_HOSTS; do launch "${pair%%:*}" & pids+=($!); done
  wait "${pids[@]}"
  local rest=$(( T - $(date +%s) + 4 ))
  [ "$rest" -gt 0 ] && { echo "waiting ${rest}s for the streams to start..."; sleep "$rest"; }
  echo "Streams:"
  each report
  echo "Consume with these URLs (TCP). Frame 0 repeats every ~61.9 s."
}

stop_one() { "${SSH[@]}" "$1" "cd $REMOTE_DIR && docker compose down -t 2" 2>&1 | tail -1 | sed "s/^/[$(label "$1")] /"; }
status_one() {
  echo "== $(label "$1")"
  "${SSH[@]}" "$1" "cd $REMOTE_DIR && docker compose ps -a --format 'table {{.Service}}\t{{.Status}}' && docker compose logs --no-log-prefix --tail 3 2>&1" || true
}

case "${1:-}" in
  up)     cmd_up ;;
  down)   each_parallel stop_one ;;
  status) each status_one ;;
  *) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
