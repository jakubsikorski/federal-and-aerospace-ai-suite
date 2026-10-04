# Handheld multi-modal application

An operator console for Intel edge devices that combines live camera
analytics, evidence search, mission documents, alerts and speech. It runs
entirely on the device, built from unmodified Open Edge Platform
`release-2026.2.0` components plus an application Backend and browser UI.

![Operator UI: live camera with detection boxes, object tracks with evidence images, open alerts, model placement and resource telemetry](docs/images/operator-ui.jpg)

## What it does

- **Live cameras** — YOLO26s INT8 detection and tracking on the NPU, with an
  annotated WebRTC stream per camera and cropped evidence images per track.
- **Evidence and history** — browse current and past tracks, filter by object
  type, and ask questions such as "how many people on camera 2 in the last
  ten minutes?".
- **Mission** — upload PDF/DOCX orders; a Mission Brief and document-grounded
  answers come from stock Chat Q&A (`Intel/neural-chat-7b-v3-3` on the GPU).
- **Alerts** — operator rules (e.g. "person on Camera 1") dispatched through
  the stock Alert Agent and spoken via SpeechT5 TTS on the CPU
  (`make speech-device TTS_DEVICE=GPU` moves it to the GPU).
- **Voice** — push-to-talk questions (**Talk**) transcribed by Whisper-medium
  on the GPU; answers and alerts are spoken.
- **Resources** — live CPU, memory, GPU, NPU and package power telemetry.

## Quick start

Requirements: an Intel GPU/NPU host (`/dev/dri/renderD128`,
`/dev/accel/accel0`), Docker Compose 2.24.4+, GNU Make, Python 3.11+, curl
and OpenSSL. The first start downloads several GB of models and needs internet
access; later starts and restarts run without internet. Each start rebuilds
the Backend image when `backend/` changed (for example after `git pull`);
without internet it warns that the Backend is not the current version and
starts the previous one.
Behind a proxy, export `http_proxy` and `https_proxy` (or the uppercase
names) in the shell that runs `make`, with `localhost,127.0.0.1` in
`no_proxy`. The containers that download take their proxy from that shell, not
from the Docker daemon or `~/.docker/config.json`. While waiting, `make` shows
each service's latest log line every 30 s.

```sh
make demo-up        # demo: pinned sample videos as looping cameras
make up             # or: your own cameras, published to rtsp://127.0.0.1:8554/camN
```

Open `https://<device-address>:8443/` after trusting `config/tls/server.crt`.
Run `make` for the full command list (status, stop, clean-up, tests); run
`make demo-up` (or `make up`) again to restart.
`make demo-mission` loads the example mission packet from `examples/`.
`examples/realsense-rtsp.sh` publishes an Intel RealSense colour stream to a
camera added as **Publisher on this device** (see the script header).

On the handheld, run the browser full screen (F11 or kiosk mode) at
1280×800. Touch-target and fit requirements are in
[docs/design/HANDHELD_TOUCH_REQUIREMENTS.md](docs/design/HANDHELD_TOUCH_REQUIREMENTS.md).

## Services

**Open Edge Platform services, reused unmodified** (`release-2026.2.0`
images, pinned in `compose/handheld-multi-modal.yaml`):

| Service | Image | Role here |
|---|---|---|
| [DL Streamer Pipeline Server](https://github.com/open-edge-platform/edge-ai-libraries/tree/release-2026.2.0/microservices/dlstreamer-pipeline-server) | `intel/dlstreamer-pipeline-server:2026.2.0-ubuntu24` | YOLO26s INT8 detection (NPU) and tracking per camera, annotated WebRTC, MQTT observations, evidence crops |
| [Chat Question-and-Answer Core](https://github.com/open-edge-platform/edge-ai-libraries/tree/release-2026.2.0/sample-applications/chat-question-and-answer-core) | `intel/chatqna:core_gpu_2026.2.0` | Mission documents and general answers: `Intel/neural-chat-7b-v3-3` (GPU), `granite-embedding-125m-english`, `ms-marco-electra-base` reranker |
| [Audio Analyzer](https://github.com/open-edge-platform/edge-ai-libraries/tree/release-2026.2.0/microservices/audio-analyzer) | `intel/audio-analyzer:2026.2.0` | Speech-to-text, Whisper-medium FP16 on the GPU |
| [Text To Speech](https://github.com/open-edge-platform/edge-ai-libraries/tree/release-2026.2.0/microservices/text-to-speech) | `intel/text-to-speech:2026.2.0` | Spoken alerts and answers, SpeechT5 (CPU default, GPU option) |
| [Alert Agent](https://github.com/open-edge-platform/edge-ai-libraries/tree/release-2026.2.0/microservices/alert-agent-service) | `intel/alert-agent-service:2026.2.0` | Alert dispatch in rule mode (no LLM in the alert path) |
| [Metrics Manager](https://github.com/open-edge-platform/edge-ai-libraries/tree/release-2026.2.0/microservices/metrics-manager) | `intel/metrics-manager:2026.2.0` | CPU, memory, GPU, NPU and power telemetry |
| [Model Download](https://github.com/open-edge-platform/edge-ai-libraries/tree/release-2026.2.0/microservices/model-download) | `intel/model-download:2026.2.0` | Prepares the YOLO26s INT8 OpenVINO IR (first start only) |

**Third-party open-source services** (pinned upstream images):

| Service | Image | Role here |
|---|---|---|
| MediaMTX | `bluenviron/mediamtx:1.11.3` | RTSP camera input, WHIP/WHEP WebRTC relay to the browser |
| Coturn | `coturn/coturn:4.18.0` | TURN relay for WebRTC only |
| Eclipse Mosquitto | `eclipse-mosquitto:2.0.22` | MQTT broker between DLSPS and the Backend |
| nginx | `nginx:1.27-alpine` | TLS reverse proxy (`https://<device>:8443`) |
| FFmpeg | `jrottenberg/ffmpeg:6.1-ubuntu2404` | Demo only: loops the sample videos as RTSP cameras |

**Built for this application** (`backend/`): the application Backend
(`handheld/backend:dev`, FastAPI) and the browser UI it serves. They own
cameras, rules, alert incidents, evidence history, speech arbitration and
telemetry aggregation, and call the services above only through their stock
APIs. Details and versions: [COMPONENTS.md](COMPONENTS.md).

## Architecture

```text
Cameras ─RTSP─> MediaMTX ─> DLSPS (YOLO26s, NPU) ─WHIP─> MediaMTX ─WebRTC─> UI
                              ├─ MQTT observations ─> Backend
                              └─ evidence crops ───> Backend
Chat Q&A · Whisper · SpeechT5 · Alert Agent · Metrics ─> Backend ─HTTPS/SSE─> UI
```

The Backend never sits in the video path and never edits pipelines at runtime.

## Documentation

| Topic | Where |
|---|---|
| Using the UI (operator guide) | [docs/OPERATOR_GUIDE.md](docs/OPERATOR_GUIDE.md) |
| Deployment and service reference | [docs/application/application-operations.md](docs/application/application-operations.md) |
| Current status | [STATUS.md](STATUS.md) |
| Components and pinned versions | [COMPONENTS.md](COMPONENTS.md), [MANIFEST.yaml](MANIFEST.yaml) |
| Model provenance | [MODEL_PROVENANCE.md](MODEL_PROVENANCE.md) |
| Design | [docs/design/APPLICATION_DESIGN.md](docs/design/APPLICATION_DESIGN.md) |
| Contributor and agent rules | [AGENTS.md](AGENTS.md) |

Demo videos: intel-iot-devkit sample (CC BY 4.0) and Pexels video 34622113
(Pexels License). The mission packet in `examples/` is synthetic.
