#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$root/scripts/smoke/_dlsps_lib.sh"
: "${MEDIAMTX_HOST_FOR_CONTAINER:=$(dlsps_resolve_container mediamtx)}"

for tool in curl ffmpeg timeout docker; do
  command -v "$tool" >/dev/null || { echo "BLOCKED: missing $tool" >&2; exit 2; }
done

dlsps_require_container
dlsps_require_container "$MEDIAMTX_HOST_FOR_CONTAINER"

# Clean feed: decode one real frame from /cam1 over RTSP.
curl --fail --silent --show-error --max-time 10 \
  http://127.0.0.1:9997/v3/paths/list >/dev/null
timeout 20 ffmpeg -nostdin -hide_banner -loglevel error -abort_on empty_output -rtsp_transport tcp \
  -i "rtsp://127.0.0.1:8554/cam1" -map 0:v:0 -frames:v 1 -f null -

# Start this test's own pipeline instance (isolation: don't rely on some
# other script or operator having left a pipeline running, and don't touch
# any pipeline this test didn't start).
echo "Starting NPU pipeline (shipped default) to exercise the annotated WebRTC leg..." >&2
DLSPS_PIPELINE_NAME=handheld_npu_detect
iid=$(dlsps_start_pipeline NPU)
echo "Started instance $iid" >&2
trap 'dlsps_stop_pipeline "$iid" || echo "WARN: instance $iid may still be running, check manually" >&2' EXIT
dlsps_wait_pipeline "$iid" 15 5 >/dev/null

# Annotated feed is native DLSPS WebRTC (WHIP) push, not RTSP, per owner
# direction. Pull it back with a real WHEP client and prove MediaMTX served
# real, decodable media — not just a 201/SDP exchange or a log line.
#
# Gotcha: DLSPS's own JPEG-appsink-fed WebRTC branch always negotiates VP9
# (src/server/webrtc/gstreamer_webrtc_manager.py picks vp9enc whenever the
# frame source is JPEG/system-memory, which our webrtc+jpeg pipeline always
# is). aiortc (scripts/smoke/whep_client.py) has no VP9 decoder, so it is
# not usable against this pipeline shape; keep it only as an H264-only
# fallback for future pipeline variants that push VA-memory frames directly.
# The proven client here is the in-container `rswebrtc` GStreamer plugin's
# `whepclientsrc`, which does support VP9 — piped through `decodebin` and
# `videoconvert` so the gate requires real decoded video/x-raw buffers, not
# raw encoded RTP payload chunks.
for i in $(seq 1 15); do
  readiness=$(curl --fail --silent --max-time 5 http://127.0.0.1:9997/v3/paths/list \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print('ready' if any(i['name']=='annotated' and i['ready'] for i in d['items']) else 'not-ready')")
  [ "$readiness" = "ready" ] && break
  sleep 1
done
if [ "$readiness" != "ready" ]; then
  echo "FAIL: /annotated never became ready on MediaMTX within 15s of starting instance $iid." >&2
  exit 1
fi

decode_log=$(docker exec "$DLSPS_CONTAINER" sh -c \
  "timeout 8 gst-launch-1.0 -v whepclientsrc signaller::whep-endpoint=http://${MEDIAMTX_HOST_FOR_CONTAINER}:8889/annotated/whep video-codecs='<VP9>' audio-codecs='<>' name=src src. ! queue ! decodebin ! videoconvert ! identity silent=false ! fakesink 2>&1" || true)

decoded_frames=$(echo "$decode_log" | grep -c "GstIdentity:identity0: last-message = chain" || true)
if [ "${decoded_frames:-0}" -lt 3 ]; then
  echo "FAIL: WHEP client decoded only $decoded_frames real video/x-raw frame(s) from /annotated (need >=3); client errors are not suppressed:" >&2
  echo "$decode_log" >&2
  exit 1
fi

if ! docker logs --tail 20 "$MEDIAMTX_HOST_FOR_CONTAINER" 2>&1 | grep -q "is reading from path 'annotated'"; then
  echo "FAIL: MediaMTX did not log a real WHEP read session for /annotated (decoded frames were seen client-side, but server-side session log is also required)." >&2
  exit 1
fi

echo "PASS: MediaMTX served real RTSP frame(/cam1) and a real, decoded WHEP pull session for instance $iid (/annotated, VP9, $decoded_frames real video/x-raw frames decoded), confirmed both client-side (decodebin) and via server log."
