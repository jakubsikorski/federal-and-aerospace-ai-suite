<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Change the Video Source

A camera's video can come from a **file**, a **live camera** (Intel RealSense
or USB webcam) or an **RTSP stream**. The source is one field of the camera
in [`configs/cameras-pymavlink.yaml`](../camera-management/configure-cameras.md)
(or `cameras-uavsdk.yaml`); detection, captioning and overlay settings are
the same for all three.

| Source | Camera file | REST start request | Pipelines used |
| --- | --- | --- | --- |
| Video file | `video: my_clip.avi` — a file in `resources/videos` ([convert it first](./use-your-own-video.md)) | `"parameters": {"video": "my_clip.avi"}` | `uav_object_detection_<det>[_<vlm>]` (`config-pymavlink.json`) |
| Live camera | `device: /dev/v4l/by-id/<name>` plus optional `width`, `height`, `fps` ([details](../camera-management/configure-cameras.md#live-cameras-realsense-usb-webcams)) | `"parameters": {"device": "/dev/v4l/by-id/<name>", "camera_width": 1280, "camera_height": 720, "camera_fps": 30}` | `uav_camera_<det>[_<vlm>]` (`config-pymavlink.json`; CPU/GPU variants in `config-pymavlink-realsense.json`) |
| RTSP stream | `rtsp: rtsp://host:8554/{uav_id}/nadir` ([details](../camera-management/configure-cameras.md#rtsp-streams-uav-mission-compute-sdk)) | `"parameters": {"rtsp_url": "rtsp://host:8554/uav-1/nadir"}` | `uav_object_detection_<det>[_<vlm>]` (`config-uavsdk.json`) |

Set exactly one of `video`, `device` or `rtsp` per camera. Then:

```bash
make check-cameras     # validates the file; reports a missing video file, device node or pipeline
make start-rtsp        # or disarm/arm; the file is re-read at every arming
```

## Notes per source

- **Video file** — the folder `resources/videos` is mounted read-only in the
  container, so a new file is visible without a restart; a running pipeline
  switches to it at its next start. Files must be H.264; use
  `scripts/convert_video.sh` for anything else.
- **Live camera** — the device node must also be listed in `.env`
  (`CAMERA_DEVICE_1` … `CAMERA_DEVICE_4`) so the compose file passes it into
  the container; recreate the container (`make pymav-up`) after changing it.
  Each node can be used by one camera only. See
  [Intel RealSense Camera](../camera-management/realsense-guide.md).
- **RTSP stream** — used in UAV Mission Compute SDK mode, where the camera
  file is `configs/cameras-uavsdk.yaml` and `{uav_id}` is replaced by
  `UAV_ID` from `.env`. The manager starts a camera only once its stream can
  be read; a stream that is not available is skipped and retried. Any RTSP
  source without authentication works, not only the SDK's.

For a pipeline started by hand through the REST API, the same values go in
the request's `parameters` — see
[Manage Pipelines — Start a pipeline](../pipeline-management/manage-pipelines.md#start-a-pipeline).
