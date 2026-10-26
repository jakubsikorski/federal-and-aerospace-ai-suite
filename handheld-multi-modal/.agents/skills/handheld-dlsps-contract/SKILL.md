---
name: handheld-dlsps-contract
description: Use when changing the handheld DL Streamer Pipeline Server integration - the fixed CPU/GPU/NPU pipeline variants, per-camera pipeline lifecycle, annotated WebRTC output, MQTT observations or tracked-object crops.
---

# DL Streamer Pipeline Server Integration

Image `intel/dlstreamer-pipeline-server:2026.2.0-ubuntu24`, Compose service
`dlsps`, REST API on `127.0.0.1:8080`. For stock APIs see the `dlsps-user`
and `dlstreamer-coding-agent` skills; for the crop element see
`scripts/dlsps/README.md`.

## Pipeline Variants

- `config/dlsps/{cpu,gpu,npu}/config.json`, selected by
  `DLSPS_CONFIG_VARIANT` (default `npu`). Switching variants needs
  `docker compose up -d --force-recreate dlsps`; a plain restart keeps the
  same bind mount.
- Default model: YOLO26s OpenVINO IR (INT8 preferred) on NPU, with CPU
  `gvatrack`. Do not fall back to a nano model.
- NPU and GPU variants use VA-memory hardware decode (`vah264dec`) and
  hardware H.264 WebRTC encode. The CPU variant uses `decodebin` and VP9.
- Devices: GPU `/dev/dri/renderD128`, NPU `/dev/accel/accel0`, plus the host
  render group (`RENDER_GID`, often 992; confirm on the target).
- Pipelines are fixed. Do not edit them at runtime or add a pipeline-editing
  API.

## Lifecycle

The Backend (`backend/app/pipelines.py`) owns pipeline instances and removes
any instance it did not start. Use `make demo-up`, `make stop` or
`make pipelines-restart` instead of calling the API by hand while it runs.

- Start: `POST /pipelines/user_defined_pipelines/{name}` returns a bare JSON
  string instance ID.
- Status: `GET /pipelines/{id}/status` (`state`, `frame_fps`,
  `elapsed_time`). `GET /pipelines/status` lists every instance ever
  started, including stopped ones.
- Stop: `DELETE /pipelines/{id}`.

Each camera gets one instance. The start request sets `source.uri`
`rtsp://mediamtx:8554/camN`, WebRTC `peer-id` `annotatedN` (WHIP push to
MediaMTX), a unique `model_instance` and a `crop-config` with the camera's
`stream_id`. Model and device stay fixed in `config.json`.

## Outputs

- Detections: MQTT topic `detections/live` through the documented
  `appsink` + `mqtt_publisher` mechanism. The raw `gvametapublish` element
  does not work in this image.
- Tracked-object crops: the `gvapython` element runs
  `scripts/dlsps/crop_candidates.py`, which crops each tracked object from
  `GstVideoRegionOfInterestMeta` before encoding and announces it on
  `detections/snips`. Stock DLSPS destinations only write whole frames. Do
  not add other crop or `snip` configuration keys.
- The stock RTSP destination sends JPEG over RTP, which browsers cannot play
  as H.264. Use the WebRTC output.

## Verification

Configuration alone does not prove GPU or NPU execution. Check
`gvafpscounter` output and the container logs for NPU compile or plugin
errors. Standalone smoke tests `scripts/smoke/02`–`06` need the Backend
stopped (they exit 2 while it runs).
