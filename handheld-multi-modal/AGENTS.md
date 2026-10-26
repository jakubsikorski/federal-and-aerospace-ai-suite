<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Handheld Multi-Modal Application — Agent Context File

The Handheld Multi-Modal application is an operator console for Intel® edge devices. It is
built from unmodified Open Edge Platform `release-2026.2.0` microservices, an application
Backend (FastAPI), and a browser UI, orchestrated with Docker Compose and a Make lifecycle.
Read [README.md](README.md) before changing the deployment.

## Component Map

| Path | Purpose |
| ---- | ------- |
| `Makefile` | All lifecycle, readiness, bundle, and test targets (run `make` for the list) |
| `compose/handheld-multi-modal.yaml` | Application services; every host port binds to `127.0.0.1` |
| `compose/cameras.yaml` | FFmpeg publishers for the demo cameras (`make demo-up` only) |
| `backend/` | Backend (`app/`), browser UI (`app/static/`), Dockerfile, tests, and API fuzzing |
| `config/dlsps/` | Pipeline configurations per device (`cpu/`, `gpu/`, `npu/`) and `detector-profile.json` |
| `config/backend/backend.yaml` | Cameras, evidence classes, mission limits, and Backend settings |
| `config/chatqna/` | Chat Q&A prompt template and model configuration |
| `config/proxy/`, `config/coturn/`, `config/mediamtx/` | TLS proxy, loopback TURN relay, and RTSP/WebRTC relay |
| `config/model-download/` | YOLO26s download and export request |
| `scripts/` | Demo asset preparation, live checks, and per-service smoke tests |
| `.agents/skills/` | Agent skills: application skills and copies of upstream skills from `open-edge-platform/skills` |

## Useful Commands

```bash
make demo-up            # start with demo cameras (also restarts)
make up                 # start without cameras
make status             # services and per-camera feed health
make check              # read-only readiness check
make pipelines-restart  # restart Backend and DLSPS only
make stop               # stop every service
make down               # remove containers; keep models, history, and TLS
make test               # Backend checks in the Backend image
```

`clean-history` and `clean-all` are destructive and require `CONFIRM=yes`.

## Architectural Boundaries

```text
MediaMTX -> DLSPS            clean RTSP
DLSPS -> MediaMTX            annotated WebRTC (WHIP)
MediaMTX -> UI               WebRTC (WHEP)
DLSPS -> MQTT -> Backend     observations
DLSPS -> shared crops        Backend static evidence images
Metrics Manager -> Backend   hardware telemetry
Backend -> UI                HTTP, SSE, WebSocket
Coturn <-> MediaMTX          WebRTC relay only
```

- The Backend is not in the video path, does not crop images, and does not reconfigure DLSPS
  pipelines at runtime. It starts and stops pipelines through the DLSPS REST API.
- The UI never accesses SQLite or crop file paths directly.
- Every host port binds to `127.0.0.1`. Do not introduce all-interface bindings or host
  networking. Remote access is through an SSH tunnel (`-L 8443 -L 3478`).
- Business rules, speech arbitration, and telemetry aggregation belong to the Backend, never to
  the Open Edge Platform microservices.

## Models and Microservices

- Pin exact container tags and model digests. No floating image tags and no silent model swaps;
  a failed startup must stop, not fall back to another model or device.
- Use the microservices unmodified: Chat Q&A downloads, converts, and loads its own models; do
  not add separate exporters, configuration rewrites, or custom model servers.
- Detector: YOLO26s OpenVINO™ IR INT8, NPU by default; CPU and GPU are fixed alternative
  pipeline configurations. To change the model, update `config/model-download/`,
  `config/dlsps/detector-profile.json`, every `config/dlsps/*/config.json`, and
  `third-party-components.spdx`.
- Language model: `Intel/neural-chat-7b-v3-3` on GPU through Chat Q&A. Speech-to-text:
  Whisper-medium on GPU through Audio Analyzer. Text-to-speech: SpeechT5 on CPU by default.
- Alerts use rule-based dispatch without an LLM in the critical path.
- Every model needs a recorded source, license, and lineage in `third-party-components.spdx`. Uncertain
  provenance blocks use.
- Never persist `HF_TOKEN` or other access tokens in configuration, logs, or Git.

## Development Rules

- Keep setup, start, stop, restart, and readiness checks in the Makefile; do not add scripts
  only to launch pipelines.
- Detect device groups and addresses instead of hard-coding a host.
- Smoke tests in `scripts/smoke/` exit `0` on pass, non-zero on failure, and `2` when blocked.
  A blocked test is not a pass.
- Do not edit upstream skill copies in `.agents/skills/`; application-specific guidance belongs
  in the `handheld-*` skills and this file.
