#!/bin/sh
# Start mediamtx, wait until START_AT (epoch seconds, PTP-synced system clock), then
# publish /media/<CAM>_1k_sei.ts to it in a loop.
set -eu

CAM=${CAM:?CAM is required}
START_AT=${START_AT:-0}
RTSP_PORT=${RTSP_PORT:-8554}
HOST=${BIND_IP:-127.0.0.1}
FILE=/media/${CAM}_1k_sei.ts

log() { echo "$(date +%H:%M:%S) [$CAM] $*"; }

[ -r "$FILE" ] || { log "ERROR: $FILE not found (mount the media directory)"; exit 1; }
case "$START_AT" in ''|*[!0-9]*) log "ERROR: START_AT must be integer epoch seconds"; exit 1 ;; esac

/mediamtx /mediamtx.yml &
MTX_PID=$!
FF_PID=""
trap 'kill $MTX_PID $FF_PID 2>/dev/null || true' TERM INT EXIT

sleep 1
kill -0 "$MTX_PID" 2>/dev/null || { log "ERROR: mediamtx failed to start"; exit 1; }

if [ "$START_AT" -gt 0 ]; then
  now=$(date +%s)
  if [ "$now" -gt "$START_AT" ]; then
    log "WARNING: START_AT is $((now - START_AT))s in the past; streams will NOT be synchronized"
  else
    log "waiting $((START_AT - now))s until START_AT=$START_AT"
    # busybox date has no sub-second field; poll the second counter (~2 ms resolution)
    while [ "$(date +%s)" -lt "$START_AT" ]; do sleep 0.002; done
  fi
fi

log "publishing $FILE to rtsp://$HOST:$RTSP_PORT/$CAM"
# small probe window: less (and less variable) start-up work between START_AT and first packet
ffmpeg -nostdin -loglevel warning -probesize 200000 -analyzeduration 200000 \
  -re -stream_loop -1 -i "$FILE" \
  -map 0:v -c copy -f rtsp -rtsp_transport tcp "rtsp://$HOST:$RTSP_PORT/$CAM" &
FF_PID=$!
wait "$FF_PID"
