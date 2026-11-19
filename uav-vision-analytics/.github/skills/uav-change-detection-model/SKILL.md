---
name: uav-change-detection-model
description: >-
  Export, install, or swap the object-detection model used by one or more
  cameras in an already-deployed uav-vision-analytics stack — another
  Ultralytics size (yolo11n/m/l/x, yolov8s, ...), a custom fine-tuned
  detector, or re-exporting the bundled YOLO11s — and point camera(s) at it
  via configs/cameras-*.yaml. USE FOR: "use a bigger/smaller YOLO model",
  "my own fine-tuned detector isn't being picked up", "re-export the
  detection model", "the model file is missing or won't load", "which
  classes does the detector know". DO NOT USE FOR: the captioning/VLM model
  (use `uav-change-vlm-model`), adding/editing a camera's source or device
  assignment unrelated to the model file (use `uav-manage-cameras`), editing
  a pipeline's GStreamer elements such as `threshold=`/`inference-interval=`
  (use `uav-manage-pipelines`), or scaffolding a brand-new stack
  (use `uav-vision-analytics`).
license: Apache-2.0
compatibility: >-
  Requires an already-deployed uav-vision-analytics stack checked out at a
  known path, with the Python virtual environment created by `make model`
  available (or creatable) for Ultralytics exports. OpenVINO IR models only
  — `gvadetect` does not load PyTorch/ONNX directly.
---

<!-- SPDX-FileCopyrightText: (C) 2026 Intel Corporation -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Change UAV Detection Model Skill

Swap the object-detection model used by one, several, or all cameras in an
**already-deployed** uav-vision-analytics stack. The detector is always an
OpenVINO IR model loaded by `gvadetect`; which file a camera uses is just one
field (`detection.model`) in `configs/cameras-*.yaml` — no pipeline or
compose edit is required to point a camera at a different model.

## Canonical Documentation (read before acting — do not rely on memorized snippets)

| Task | Read this file in the target repo first |
| --- | --- |
| The bundled YOLO11s model, how `make model` produces it, where it lives | `docs/user-guide/how-to-customize/model-and-video-management/model.md` |
| Export another Ultralytics size, or bring a custom/fine-tuned OpenVINO IR detector, point a camera at it | `docs/user-guide/how-to-customize/model-and-video-management/modify-model.md` |
| Which field in the camera file selects a model, validation | `docs/user-guide/how-to-customize/camera-management/configure-cameras.md` |

Read the live copy of these docs in the target repo — the exact `yolo export`
options and pinned `ultralytics` version are maintained there and can change
between app releases; do not hardcode a remembered command.

## How to Use This Skill

1. Confirm `{{STACK_DIR}}` and whether the requested model is (a) another
   Ultralytics size, (b) a custom/fine-tuned model already in OpenVINO IR
   format, or (c) a custom model still in PyTorch/ONNX that needs conversion.
2. Ask which camera(s) should use it (all, or specific `id`s from
   `configs/cameras-*.yaml`) and on which detection device(s) those cameras
   already run (`cpu`/`gpu`/`npu`) — the model must work on that device.
3. Read `modify-model.md` (and `model.md` for the pinned Ultralytics version)
   for the exact export/placement commands, and follow them for the model
   kind identified in step 1.
4. Update `detection.model` for the target camera(s) in `configs/cameras-*.yaml`.
5. Validate, apply, and verify per the guardrails and completion criteria below.

## Parameter Validation (enforce BEFORE editing the camera file)

| Param | Rule | Why |
| --- | --- | --- |
| Model path | ends in `.xml`, with a matching `.bin` alongside it, under `resources/models/` | `gvadetect`/the pipeline schema rejects any other location or extension |
| Model format | OpenVINO IR (not `.pt`/`.onnx`/safetensors) | must be converted first — see Option A/B in `modify-model.md` |
| `detection.model` value in the camera file | path relative to `resources/models/`, matching `^[A-Za-z0-9_./-]+\.xml$`, no `..` | path traversal / malformed path is rejected by `camera_config.py` |
| Ultralytics version (Option A) | keep the version pinned in the target repo's `model.md`/`requirements.txt` | a newer version has been observed to export a graph that fails on GPU/NPU |

## Execution Guardrails

- `make model` (and a manual `yolo export`/`optimum-cli` run) execute on the
  **host**, in the calling shell — not inside a Docker build. If a download
  hangs or TLS-fails behind a corporate proxy, the fix is exporting
  `http_proxy`/`https_proxy`/`no_proxy` in that shell (and trusting the
  proxy's root CA if it does TLS inspection), not a Docker build arg; see
  `docs/user-guide/how-to-guides/troubleshooting.md#make-model--make-vlm-model-fail-behind-a-corporate-proxy`.
- Never invent export flags — use exactly what `model.md`/`modify-model.md`
  in the target repo specifies (e.g. `dynamic=True opset=18 imgsz=640
  half=True`), since these are tuned for the pipelines' expectations.
- Do not touch `configs/config-*.json` — the pipeline's `gvadetect` element
  already takes the model path as a parameter (`detection_model`); switching
  models is a camera-file change, never a pipeline-definition change.
- A model converted from a non-Ultralytics framework may need a model-proc
  JSON for its output layout — flag this to the user rather than guessing
  one; point to the DL Streamer `gvadetect model-proc=` documentation.
- After copying/exporting the model files, run `make check-cameras` — it
  fails fast with "detection model not found" if the path is wrong, before
  anything is restarted.
- Apply with `make start-rtsp` or disarm/arm (file is re-read at every
  arming); a pipeline already running keeps its old model until the next
  start — tell the user if a restart is required to see the change live.
- Cameras sharing the same device + model path load one shared model
  instance; changing the model for one camera that shares its path with
  another affects both — confirm this is intended before editing a model
  used by multiple cameras.
- Never delete an existing model folder the user did not explicitly ask to
  remove — a camera referencing it would then fail validation.

## Completion Criteria

1. The new model's `.xml`/`.bin` exist under `resources/models/<name>/...`.
2. `make check-cameras` reports no "detection model not found" for any
   affected camera and shows the expected `detection.model` in its table.
3. `make cameras-status` (once armed) shows the camera's pipeline running.
4. `ffplay -rtsp_transport tcp rtsp://<HOST_IP>:8555/uav-<id>` shows boxes/labels reflecting the
   new model's classes (or the detection log, see `view-detection-logs.md`).
5. No `400` from the REST API when a pipeline is (re)started with the new
   `detection_model` parameter.
