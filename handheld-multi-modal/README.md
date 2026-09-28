# Handheld multi-modal application

An operator console for Intel edge devices that combines live camera
analytics, evidence search, mission documents, alerts and speech. It runs
entirely on the device, built from unmodified Open Edge Platform
`release-2026.2.0` components plus an application Backend and browser UI.

![Operator UI: live annotated camera, detected object tracks with evidence images, model placement and resource telemetry](docs/images/operator-ui.jpg)

## What it does

- **Live cameras** — YOLO26s INT8 detection and tracking on the NPU, with an
  annotated WebRTC stream per camera and cropped evidence images per track.
- **Evidence and history** — browse current and past tracks, filter by object
  type, and ask questions such as "how many people on camera 2 in the last
  ten minutes?".
- **Mission** — upload PDF/DOCX orders; a Mission Brief and document-grounded
  answers come from stock Chat Q&A (`Intel/neural-chat-7b-v3-3` on the GPU).
- **Alerts** — operator rules (e.g. "person on Camera 1") dispatched through
  the stock Alert Agent and spoken via SpeechT5 TTS on the CPU.
- **Voice (next phase)** — Whisper-medium transcription runs on the GPU;
  microphone input in the UI is not built yet.
- **Resources** — live CPU, memory, GPU, NPU and package power telemetry.

## Quick start

Requirements: an Intel GPU/NPU host (`/dev/dri/renderD128`,
`/dev/accel/accel0`), Docker Compose 2.24.4+, GNU Make, Python 3.11+, curl
and OpenSSL. The first start downloads several GB of models.

```sh
make demo-up        # demo: pinned sample videos as looping cameras
make up             # or: your own cameras, published to rtsp://127.0.0.1:8554/camN
```

Open `https://<device-address>:8443/` after trusting `config/tls/server.crt`.
Run `make` for the full command list (status, stop, restart, clean-up, tests).
`make demo-mission` loads the example mission packet from `examples/`.

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
| Current status and evidence | [STATUS.md](STATUS.md), [docs/application/evidence/](docs/application/evidence/) |
| Components and pinned versions | [COMPONENTS.md](COMPONENTS.md), [MANIFEST.yaml](MANIFEST.yaml) |
| Model provenance | [MODEL_PROVENANCE.md](MODEL_PROVENANCE.md) |
| Design | [docs/design/APPLICATION_DESIGN.md](docs/design/APPLICATION_DESIGN.md) |
| Contributor and agent rules | [AGENTS.md](AGENTS.md) |

Demo videos: intel-iot-devkit sample (CC BY 4.0) and Pexels video 34622113
(Pexels License). The mission packet in `examples/` is synthetic.
