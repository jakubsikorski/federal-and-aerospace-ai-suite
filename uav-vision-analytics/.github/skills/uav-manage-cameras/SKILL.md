---
name: uav-manage-cameras
description: >-
  Add, edit, enable/disable, or remove a camera in an already-deployed
  uav-vision-analytics stack by editing configs/cameras-pymavlink.yaml (or
  cameras-uavsdk.yaml) — switch a camera's source between a video file, a
  live camera (Intel RealSense/USB webcam), or an RTSP stream, change its
  detection device, toggle overlay/captioning, then validate and apply the
  change safely. USE FOR: "add a camera", "point this camera at my own
  video", "use my RealSense as a camera", "switch front_camera to the GPU",
  "turn off the overlay on back_camera", "why isn't my camera showing up".
  DO NOT USE FOR: scaffolding a brand-new uav-vision-analytics stack from
  scratch (use the `uav-vision-analytics` skill), swapping in a different
  detection or captioning *model file* (use `uav-change-detection-model` /
  `uav-change-vlm-model`), editing a pipeline's GStreamer elements or REST
  parameters (use `uav-manage-pipelines`), or changing what telemetry text
  is drawn on the stream (use `uav-customize-telemetry-overlay`).
license: Apache-2.0
compatibility: >-
  Requires an already-deployed uav-vision-analytics stack (pymavlink or
  uavsdk mode) checked out at a known path, with `make pymav-up` /
  `make uavsdk-up` already run at least once (so `.env` and
  `configs/cameras-*.yaml` exist). Docker Compose v2 and `make` available.
---

<!-- SPDX-FileCopyrightText: (C) 2026 Intel Corporation -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Manage UAV Cameras Skill

Cameras are declared in one YAML file, never in pipeline definitions. This
skill walks through adding, editing, re-pointing, or removing a camera entry
in `configs/cameras-pymavlink.yaml` / `configs/cameras-uavsdk.yaml` of an
**already-deployed** uav-vision-analytics stack, and applying the change
safely.

## Canonical Documentation (read before editing — do not rely on memorized snippets)

| Task | Read this file in the target repo first |
| --- | --- |
| Camera file schema, fields, which pipeline a camera selects | `docs/user-guide/how-to-customize/camera-management/configure-cameras.md` |
| Add an Intel RealSense / USB webcam as a live camera | `docs/user-guide/how-to-customize/camera-management/realsense-guide.md` |
| Switch a camera between file / live camera / RTSP source | `docs/user-guide/how-to-customize/model-and-video-management/change-video-source.md` |
| Convert and use your own video footage | `docs/user-guide/how-to-customize/model-and-video-management/use-your-own-video.md` |

These docs are the single source of truth for exact commands and file
formats — they are generated from, and tested against, the actual shipped
code, so read the live copy in the target repo rather than quoting a
remembered version (the format can change between app releases).

## How to Use This Skill

1. Confirm the path to the deployed stack (`{{STACK_DIR}}`) and its
   deployment mode (pymavlink or uavsdk) — if unclear, check which
   `docker-compose-*.yml` is running (`docker compose ps` / `docker ps`).
2. Ask the user what they want to do, in ONE batched question covering the
   fields below; accept partial answers and fill the rest from the existing
   camera entry (for an edit) or the schema defaults (for a new camera).
3. Read `configs/cameras-{pymavlink,uavsdk}.yaml` as it exists today — never
   overwrite the whole file; edit only the camera(s) in scope.
4. Read the relevant doc(s) from the table above for the exact field names,
   validation patterns, and any source-specific prerequisite (e.g. `.env`
   `CAMERA_DEVICE_*` for a live camera).
5. Apply the edit, then validate and apply per Execution Guardrails below.
6. Verify and report back using the Completion Criteria.

## Questions to Ask (single batched prompt)

1. Action: add a new camera / edit an existing one / enable or disable one / remove one
2. Camera `id` (new camera) or which existing camera (edit/enable/disable/remove)
3. Source: video file (which file, already in `resources/videos`, or new
   footage to convert first) / live camera (which `/dev/v4l/by-id/...` or
   `/dev/videoN` node) / RTSP stream (uavsdk mode only — which URL)
4. Detection device: `cpu` / `gpu` / `npu`
5. Captioning: off, or on with device (`cpu`/`gpu`), prompt, and max tokens
6. Overlay: on (default) or off

## Parameter Validation (enforce BEFORE writing the file)

| Param | Rule | Why |
| --- | --- | --- |
| `id` | `^[a-z][a-z0-9_]{0,31}$`, not already used by another camera (unless editing it) | Also names the RTSP stream and MQTT caption topic |
| source | exactly one of `video` / `device` / `rtsp` set | `camera_config.py` rejects zero or more than one |
| `video` | file already exists under `resources/videos`, or will be created by `scripts/convert_video.sh` first | otherwise `make check-cameras` reports "video not found" |
| `device` | path exists on the host (`v4l2-ctl --list-devices`) and is also set as `CAMERA_DEVICE_1`..`_4` in `.env` | the container only sees device paths passed through `.env` |
| `rtsp` | `rtsp://` URL, uavsdk mode only | pymavlink mode has no RTSP-source pipelines by default |
| `detection.device` | `cpu`/`gpu`/`npu`, and a matching pipeline exists for the camera's source kind | `make check-cameras` reports an unavailable pipeline instead of guessing |
| `captioning.device` | `off`/`cpu`/`gpu`, never `npu` | the VLM element has no NPU plugin |
| `captioning.prompt` | letters, digits, spaces, and `, . ? ' -` only | pipeline parameter pattern rejects anything else |

## Execution Guardrails

- Never hand-write a new pipeline name or edit `config-*.json` to "make room"
  for a camera — the fixed pipeline matrix already covers every
  `(source, detection device, caption device)` combination; if
  `make check-cameras` reports the pipeline unavailable for a live camera on
  `cpu`/`gpu`, point to copying it from `configs/config-pymavlink-realsense.json`
  (see the RealSense doc) rather than writing a new pipeline from scratch.
- Edit the YAML file in place (preserve other cameras, comments, and key
  order as much as the editor allows); never regenerate the whole file from
  scratch unless the user asked to start over.
- Always run `make check-cameras` after every edit, before telling the user it is
  done — a file with errors is refused by the pipeline manager and the
  previous configuration silently keeps running, so an unvalidated typo can
  look like success until the next arming.
- Apply with `make start-rtsp` (foreground; re-reads the file on every
  arming) or by disarming/arming the vehicle; to start the cameras without
  arming, use the `--start-now` option documented in `configure-cameras.md`. Never
  tell the user the change is live without one of these.
- Edit the active mode's file only: `cameras-pymavlink.yaml` (`make pymav-up`) or
  `cameras-uavsdk.yaml` (`make uavsdk-up`). `make check-cameras CAMERAS=<file>` takes the
  path as seen inside the container (`/home/pipeline-server/configs/<file>`).
- A live-camera device node can only be used by one camera at a time; check
  the existing cameras before assigning a node that is already in use.
- If the device path changed or is new, remind the user it must also be set
  in `.env` (`CAMERA_DEVICE_1`..`_4`) and the stack recreated
  (`make pymav-up`) for the container to see it — editing the YAML alone is
  not enough for a new live-camera node.
- Report exact file paths changed and the exact commands run; do not
  silently run destructive commands (`make pymav-down`, deleting a camera's
  video file) without the user's confirmation.

## Completion Criteria

1. `make check-cameras` reports the file valid, with the expected camera(s)
   listed and the correct pipeline/stream-path/detection+captioning per row.
2. No "pipeline unavailable", "video not found", or "captioning model not
   found" warnings for the changed camera(s).
3. `make cameras-status` (if the stack is armed/running) shows the camera's
   actual running pipeline matching the file.
4. `ffplay -rtsp_transport tcp rtsp://<HOST_IP>:8555/uav-<id>` (or the ground-station dashboard)
   shows the expected video for the changed camera.
5. For a removed/disabled camera: it no longer appears in `make cameras-status`
   after the next arm/disarm cycle.
