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
- **Mission** — upload PDF/DOCX orders (text only; size and page limits set in
  `config/backend/backend.yaml`); a Mission Brief and document-grounded
  answers, including short follow-ups, come from stock Chat Q&A
  (`Intel/neural-chat-7b-v3-3` on the GPU).
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
starts the previous one. To set up devices without internet, `make bundle`
copies a prepared device; see
[Copy to another device](docs/application/application-operations.md#copy-to-another-device-offline).

```sh
make demo-up        # demo: pinned sample videos as looping cameras
make up             # or: no demo cameras; add your own in the console under Cameras
```

Open `https://localhost:8443/` on the device after trusting
`config/tls/server.crt`. The application listens on `127.0.0.1` only; to use
it from another machine, see [Remote access](#remote-access-ssh-tunnel).
Run `make` for the full command list (status, stop, clean-up, tests); run
`make demo-up` (or `make up`) again to restart.
`make demo-mission` loads the example mission packet from `examples/`.
`examples/realsense-rtsp.sh` publishes an Intel RealSense colour stream to a
camera added as **Publisher on this device** (see the script header).

On the handheld, run the browser full screen (F11 or kiosk mode) at
1280×800. Touch-target and fit requirements are in
[docs/design/HANDHELD_TOUCH_REQUIREMENTS.md](docs/design/HANDHELD_TOUCH_REQUIREMENTS.md).

### Remote access (SSH tunnel)

From a laptop, forward the UI port and the loopback TURN port over SSH:

```sh
ssh -N -L 8443:127.0.0.1:8443 -L 3478:127.0.0.1:3478 <user>@<device-address>
```

Then open `https://localhost:8443/` on the laptop and accept the device
certificate (or trust `config/tls/server.crt`). Keep local port 8443: the
proxy accepts only `localhost:8443`/`127.0.0.1:8443` origins. The 3478 forward
carries live video: SSH does not carry UDP, so the browser relays WebRTC media
over TURN/TCP through the device's loopback-only Coturn. Without it the page
loads but camera tiles stay black. The tunnel gives full, unauthenticated UI
access: do not share it (`ssh -g`, `GatewayPorts`). Details:
[application-operations.md](docs/application/application-operations.md).

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
| Coturn | `coturn/coturn:4.18.0` | TURN relay for WebRTC only, loopback-only (SSH-tunnelled browsers) |
| Eclipse Mosquitto | `eclipse-mosquitto:2.0.22` | MQTT broker between DLSPS and the Backend |
| nginx | `nginx:1.27-alpine` | TLS reverse proxy, localhost only (`https://localhost:8443`) |
| FFmpeg | `jrottenberg/ffmpeg:6.1-ubuntu2404` | Demo only: loops the sample videos as RTSP cameras |

**Built for this application** (`backend/`): the application Backend
(`handheld/backend:dev`, FastAPI) and the browser UI it serves. They own
cameras, rules, alert incidents, evidence history, speech arbitration and
telemetry aggregation, and call the services above only through their stock
APIs. Details and versions: [COMPONENTS.md](COMPONENTS.md).

## Architecture

[![Architecture: Intel Open Edge Platform microservices (blue), open-source infrastructure and the application Backend](docs/images/architecture.svg)](docs/images/architecture.svg)

Blue boxes are stock Intel Open Edge Platform microservices, run unmodified;
each lists the endpoints the Backend calls. Select the diagram to open it full
size. The running app has an interactive version of this page (see below).

| Open Edge Platform component | Endpoints called | Source |
| --- | --- | --- |
| DL Streamer Pipeline Server | `POST /pipelines/user_defined_pipelines/{name}`, `GET /pipelines/status`, `GET /pipelines/{id}/status`, `DELETE /pipelines/{id}` | [microservices/dlstreamer-pipeline-server](https://github.com/open-edge-platform/edge-ai-libraries/tree/c2cdea0ef9b137986b14e6bbeebcb80dbde4c0e9/microservices/dlstreamer-pipeline-server) |
| Chat Q&A Core | `POST /chat`, `GET`/`POST`/`DELETE /documents`, `GET /model`, `GET /health` | [sample-applications/chat-question-and-answer-core](https://github.com/open-edge-platform/edge-ai-libraries/tree/c2cdea0ef9b137986b14e6bbeebcb80dbde4c0e9/sample-applications/chat-question-and-answer-core) |
| Audio Analyzer | `POST /v1/audio/transcriptions`, `GET /health` | [microservices/audio-analyzer](https://github.com/open-edge-platform/edge-ai-libraries/tree/c2cdea0ef9b137986b14e6bbeebcb80dbde4c0e9/microservices/audio-analyzer) |
| Text To Speech | `POST /v1/audio/speech`, `GET /v1/model-info` | [microservices/text-to-speech](https://github.com/open-edge-platform/edge-ai-libraries/tree/c2cdea0ef9b137986b14e6bbeebcb80dbde4c0e9/microservices/text-to-speech) |
| Alert Agent | `POST /api/v1/actions/execute`, `GET /api/v1/events` (SSE), `GET /api/v1/health` | [microservices/alert-agent-service](https://github.com/open-edge-platform/edge-ai-libraries/tree/c2cdea0ef9b137986b14e6bbeebcb80dbde4c0e9/microservices/alert-agent-service) |
| Metrics Manager | `GET /api/v1/metrics/latest` | [microservices/metrics-manager](https://github.com/open-edge-platform/edge-ai-libraries/tree/c2cdea0ef9b137986b14e6bbeebcb80dbde4c0e9/microservices/metrics-manager) |
| Model Download (setup only) | `POST /api/v1/models/download`, `GET /api/v1/jobs/{id}` | [microservices/model-download](https://github.com/open-edge-platform/edge-ai-libraries/tree/c2cdea0ef9b137986b14e6bbeebcb80dbde4c0e9/microservices/model-download) |

Open-source infrastructure: MediaMTX (video relay; the Backend also uses its
`/v3` control API for camera paths and sessions), Coturn (loopback-only TURN),
Mosquitto and nginx.

The application is localhost-only: every published port binds to `127.0.0.1`
(Coturn too, so a TURN relay is available only to local clients). Open it in
a browser on the device itself, or from another machine through an
[SSH tunnel](#remote-access-ssh-tunnel). See
`docs/design/THREAT_MODEL.md` for the residual local risks.

The Backend never sits in the video path and never edits pipelines at runtime.
The running app serves an **Architecture** page (`/architecture.html`, linked
from the Models & devices strip): an interactive diagram that tags each
service as Open Edge Platform, open source or application, shows the Open Edge
Platform API endpoints the Backend calls, live status, step-by-step flows (live
video, detection to alert, voice and mission questions), and the models,
devices, images and interfaces used.

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
