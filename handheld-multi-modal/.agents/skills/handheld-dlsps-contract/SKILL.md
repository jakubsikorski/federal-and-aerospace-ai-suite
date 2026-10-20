---
name: handheld-dlsps-contract
description: Use when integrating fixed CPU/GPU/NPU Pipeline Server variants, annotated WebRTC (WHIP/WHEP), observation MQTT, or tracked-object snips for the handheld multi-modal application.
---

# Fixed DLSPS integration

Pinned image from release source:
`intel/dlstreamer-pipeline-server:2026.2.0-ubuntu24`.
Use official `dlsps-user` and `dlstreamer-coding-agent` skills for stock APIs.
Repository-specific source findings + live evidence: `docs/application/video-contracts.md`.
**Runtime-verified**: all three variants (CPU/GPU/NPU) deployed and smoke-tested
against real running containers, not source-only anymore (see `STATUS.md`
and `scripts/smoke/{02,03,04,05}_*.sh`, hardened 2026-09-10).

Config locations: `config/dlsps/{cpu,gpu,npu}/config.json`, selected at
deployment time via `DLSPS_CONFIG_VARIANT` in `compose/handheld-multi-modal.yaml`
(default `npu`). Changing the mounted variant requires recreating the
container (`docker compose up -d --force-recreate dlsps`), not a plain
restart — a restart re-reads the same bind-mounted file, it does not swap it.
GPU `/dev/dri/renderD128`, NPU `/dev/accel/accel0`; render GID 992 observed on
this host, confirm on target device.
REST port 8080 (host `127.0.0.1:8080`); start fixed user-defined pipeline at
`POST /pipelines/user_defined_pipelines/{configured-name}` (returns a bare
JSON string instance id), poll `GET /pipelines/{id}/status` for
`state`/`frame_fps`/`elapsed_time`, and stop with `DELETE /pipelines/{id}`
(only that instance; `GET /pipelines/status` lists all instances ever
started, including stopped ones, never auto-pruned). The Backend
(`backend/app/pipelines.py`) owns this lifecycle for the application;
`make demo-up` / `demo-stop` / `demo-down` drive it.

Required default: YOLO26s IR, INT8 preferred, NPU inference, CPU `gvatrack`
(owner-directed 2026-09-24 replacement for YOLO11s; never substitute nano),
Intel hardware decode (`vah264dec`). NPU/GPU variants use VA-memory hardware
decode and DLSPS WebRTC hardware H.264 encode; CPU variant uses software
`decodebin`/VP9. See `docs/application/video-contracts.md`; do not runtime-edit
pipelines or fall back to nano.
The source is set per pipeline instance in the REST start request body, so
no config.json change is needed per camera. The application starts one
instance per camera with `source.uri` `rtsp://mediamtx:8554/camN` and WebRTC
`peer-id` `annotatedN` (native WHIP push to MediaMTX, not RTSP — see
`docs/application/video-contracts.md` for the owner-approved amendment).
Each start also sends a unique `model_instance` and a `crop-config` with the
camera's `stream_id`; model and device are fixed in config.json. The Backend
deletes every instance it does not own, so the standalone smokes
(`scripts/smoke/0[1-6]_*.sh`) exit 2 while it runs; they read live `cam1` and
tag their output as stream `smoke`.
Topics: `detections/live` (confirmed real payload shape in
`docs/application/video-contracts.md`); `detections/snips` is implemented by the application crop producer.

Do not invent a `snip` configuration key beyond the shipped `crop-config`
pipeline parameter. No stock DLSPS destination performs a per-object crop;
`destination.frame` only supports whole-frame `rtsp`, `webrtc`, `s3_write`,
`application`. An earlier `s3_write`/SeaweedFS whole-frame "frame-saver"
was built then fully rejected/backed out by the owner — no SeaweedFS
container or `s3_write` destination exists in this repo. The implemented
extension point is a stock `gvapython` element (no
`ADDITIONAL_GST_PLUGIN_PATH` needed) reading
`GstVideoRegionOfInterestMeta` per tracked object and cropping before any
encode step: `scripts/dlsps/crop_candidates.py`, wired into all three
shipped `config/dlsps/{cpu,gpu,npu}/config.json` variants and
`compose/handheld-multi-modal.yaml` — see
`scripts/smoke/06_snip.sh` (validated PASS).

Do not mistake stock RTSP destination's JPEG RTP output for a browser-compatible
H.264 push to MediaMTX. Use the documented `appsink`+`mqtt_publisher` MQTT
mechanism, not the raw `gvametapublish` element (confirmed broken in the
pinned image). Do not claim GPU/NPU execution from configuration alone — verify
via `gvafpscounter` output and the absence of NPU compile/plugin errors in
container logs (see `scripts/smoke/04_dlsps_npu.sh`).
