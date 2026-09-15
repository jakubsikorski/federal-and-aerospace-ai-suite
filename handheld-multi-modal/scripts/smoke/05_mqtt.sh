#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$root/scripts/smoke/_dlsps_lib.sh"
: "${MQTT_TOPIC:=detections/live}"
: "${MQTT_CONTAINER:=$(dlsps_resolve_container mqtt)}"

dlsps_require_container
dlsps_require_container "$MQTT_CONTAINER"

# Requires config.json currently mounted in the DLSPS container to use the
# documented appsink+mqtt_publisher mechanism (NOT the raw gvametapublish
# element, which is broken in this rc3 image — see docs/bootstrap/video-contracts.md).
echo "Starting NPU pipeline for this test..." >&2
DLSPS_PIPELINE_NAME=handheld_npu_detect
iid=$(dlsps_start_pipeline NPU)
echo "Started instance $iid" >&2
trap 'dlsps_stop_pipeline "$iid" || echo "WARN: instance $iid may still be running, check manually" >&2' EXIT
dlsps_wait_pipeline "$iid" 15 5 >/dev/null

echo "Subscribing to $MQTT_TOPIC for 10s..." >&2
raw=$(timeout 10 docker run --rm --network container:"$MQTT_CONTAINER" \
  eclipse-mosquitto:2.0.22 mosquitto_sub -h 127.0.0.1 -p 1883 -t "$MQTT_TOPIC" 2>/dev/null || true)

# Parse each payload: only count messages that (a) are valid JSON from the
# instance this test itself started (metadata.pipeline.instance_id), with
# (b) a nonempty tracked-object list, and (c) a strictly advancing frame_id —
# proving the tested pipeline, not a stale or unrelated one, is progressing.
result=$(printf '%s' "$raw" | python3 -c "
import json, sys
iid = sys.argv[1]
matched = 0
prev_frame = None
advancing = False
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    meta = msg.get('metadata', {})
    pipeline = meta.get('pipeline', {})
    if pipeline.get('instance_id') != iid:
        continue
    if not meta.get('objects'):
        continue
    matched += 1
    frame_id = meta.get('frame_id')
    if prev_frame is not None and isinstance(frame_id, (int, float)) and frame_id > prev_frame:
        advancing = True
    prev_frame = frame_id
print(f'{matched} {int(advancing)}')
" "$iid")

matched=$(echo "$result" | awk '{print $1}')
advancing=$(echo "$result" | awk '{print $2}')

if [ "${matched:-0}" -lt 5 ] || [ "${advancing:-0}" != "1" ]; then
  echo "FAIL: only $matched tracked-detection messages matched instance $iid (advancing=$advancing) on $MQTT_TOPIC in 10s; expected 5+ with an advancing frame_id." >&2
  exit 1
fi

echo "PASS: DLSPS published $matched real tracked-detection messages for instance $iid to $MQTT_TOPIC (advancing frame_id) via the documented appsink+mqtt_publisher mechanism."
