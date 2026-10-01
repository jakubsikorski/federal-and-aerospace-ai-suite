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
# A RealSense exposes several /dev/video* nodes (depth, IR, colour, metadata):
# use the one that lists YUYV in `v4l2-ctl -d /dev/videoN --list-formats-ext`.
set -euo pipefail

DEVICE=${DEVICE:-/dev/video8}
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
