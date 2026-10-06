#!/usr/bin/env bash
# Publish an Intel RealSense colour stream to the handheld application as an
# RTSP camera.
#
# 1. In the console open Cameras > + Add camera > "Publisher on this device"
#    and add it. The card shows the address to publish to, for example
#    rtsp://127.0.0.1:8554/cam4.
# 2. Run on the same device:
#      RTSP_URL=rtsp://127.0.0.1:8554/cam4 examples/realsense-rtsp.sh
#
# RealSense colour is raw YUYV over UVC, so it must be encoded to H.264.
# ENCODER=vaapi (default) encodes on the Intel GPU (user needs the render
# group); ENCODER=cpu uses libx264 (~20-40% of one core at 720p30).
#
# Selecting the device: a RealSense exposes several /dev/video* nodes (depth,
# IR, colour, metadata) and only the colour node offers YUYV. With DEVICE
# unset the script picks the first RealSense node that offers YUYV. To see
# the candidates, or to choose one of several cameras:
#      examples/realsense-rtsp.sh --list
#      DEVICE=/dev/video4 RTSP_URL=... examples/realsense-rtsp.sh
# /dev/videoN numbers can change after replugging; the /dev/v4l/by-id/ and
# /dev/v4l/by-path/ links printed by --list stay the same for a given camera
# or USB port.
set -euo pipefail

# Prints "<node> <name>" for every video node that offers YUYV (yuyv422).
yuyv_nodes() {
  local node name
  for node in /dev/video*; do
    [[ -c $node ]] || continue
    name=$(cat "/sys/class/video4linux/${node#/dev/}/name" 2>/dev/null || echo unknown)
    ffmpeg -hide_banner -f v4l2 -list_formats raw -i "$node" 2>&1 | grep -q yuyv422 \
      && echo "$node $name"
  done
  return 0
}

if [[ ${1:-} == --list ]]; then
  echo "Video nodes offering YUYV (a RealSense colour node is one of these):"
  yuyv_nodes | sed 's/^/  /'
  for link in /dev/v4l/by-id/* /dev/v4l/by-path/*; do
    [[ -e $link ]] && echo "  $link -> $(readlink -f "$link")"
  done
  exit 0
fi

if [[ -z ${DEVICE:-} ]]; then
  DEVICE=$(yuyv_nodes | grep -i realsense | head -n1 | cut -d" " -f1 || true)
  [[ -n $DEVICE ]] || { echo "No RealSense colour node found; check the USB connection, run with --list, or set DEVICE=/dev/videoN" >&2; exit 1; }
  echo "Using $DEVICE (set DEVICE to choose another)" >&2
fi
RTSP_URL=${RTSP_URL:?set RTSP_URL to the address shown when adding the camera}
SIZE=${SIZE:-1280x720}
FPS=${FPS:-30}
ENCODER=${ENCODER:-vaapi}
RENDER_NODE=${RENDER_NODE:-/dev/dri/renderD128}

input=(-f v4l2 -input_format yuyv422 -video_size "$SIZE" -framerate "$FPS" -i "$DEVICE")
# One keyframe per second and no B-frames keep start-up and latency short.
case "$ENCODER" in
  vaapi) encode=(-vaapi_device "$RENDER_NODE" -vf format=nv12,hwupload
                 -c:v h264_vaapi -g "$FPS" -bf 0) ;;
  cpu)   encode=(-pix_fmt yuv420p -c:v libx264 -preset ultrafast -tune zerolatency
                 -g "$FPS" -bf 0) ;;
  *)     echo "ENCODER must be vaapi or cpu" >&2; exit 2 ;;
esac

exec ffmpeg -nostdin -hide_banner -loglevel warning "${input[@]}" "${encode[@]}" \
  -an -f rtsp -rtsp_transport tcp "$RTSP_URL"
