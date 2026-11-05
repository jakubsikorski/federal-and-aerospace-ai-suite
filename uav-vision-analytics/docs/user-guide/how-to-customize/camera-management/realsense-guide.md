<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Intel RealSense — UAV Vision Analytics

A RealSense (or any UVC webcam) is a live camera in [`configs/cameras-pymavlink.yaml`](../../../../configs/cameras-pymavlink.yaml): the camera has a
`device` instead of a `video` file. The settings are described in [Configure the cameras](./configure-cameras.md#live-cameras-realsense-usb-webcams);
this page is the short walk-through.

## Prerequisites

See [System Requirements](../../get-started/system-requirements.md) for the full list of software and hardware prerequisites.

## Steps

1. **Find the colour node.** List the cameras and check the formats of the nodes:

   ```bash
   lsusb
   v4l2-ctl --list-devices
   v4l2-ctl -d /dev/video4 --list-formats-ext     # the colour node offers YUYV or UYVY; depth is Z16, infrared GREY
   ls -l /dev/v4l/by-id/                           # stable names; prefer them over /dev/videoN
   ```

2. **Make it visible to the container.** Put the path in `.env` (`make init` fills `CAMERA_DEVICE_1` with the first USB camera;
   up to `CAMERA_DEVICE_4`) and recreate the container:

   ```bash
   CAMERA_DEVICE_1=/dev/v4l/by-id/usb-Intel_R__RealSense_TM__Depth_Camera_435_..._<serial>-video-index0
   make pymav-up
   ```

3. **Enable the camera** in `configs/cameras-pymavlink.yaml` (it ships as `realsense_camera`, disabled), with the same path as `device:`,
   then check and start:

   ```bash
   make check-cameras
   make start-rtsp          # the cameras start when the vehicle is armed
   ```

   Without a vehicle: `docker exec -it uav-va-dlstreamer-pipeline-server python3 /home/pipeline-server/scripts/pipeline_manager.py --start-now`.

4. **View the stream** at `rtsp://<HOST_IP>:8555/uav-realsense-camera` (`ffplay`, VLC or the ground-station dashboard, where the
   card has a `LIVE` badge).

## Pipelines

Live cameras use `uav_camera_<det>` and `uav_camera_<det>_<vlm>`. `configs/config-pymavlink.json` has `uav_camera_npu` and
`uav_camera_npu_gpu`; for a CPU or GPU camera copy the pipeline you need from
[`configs/config-pymavlink-realsense.json`](../../../../configs/config-pymavlink-realsense.json) (all nine) into it and recreate the
container. `make check-cameras` says which pipeline is missing.

## UAV Mission Compute SDK stack

The SDK stack streams its cameras over RTSP and uses `configs/cameras-uavsdk.yaml`; see
[Get started with the UAV Mission Compute SDK](../../get-started/get-started-uavsdk.md).
