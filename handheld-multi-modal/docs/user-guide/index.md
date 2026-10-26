# Handheld (Soldier System) Blueprint

<!--hide_directive
<div class="component_card_widget">
  <a class="icon_github" href="https://github.com/open-edge-platform/federal-and-aerospace-ai-suite/tree/main/handheld-multi-modal">
     GitHub
  </a>
  <a class="icon_document" href="https://github.com/open-edge-platform/federal-and-aerospace-ai-suite/blob/main/handheld-multi-modal/README.md">
     Readme
  </a>
  <a class="icon_download" href="https://github.com/open-edge-platform/federal-and-aerospace-ai-suite/releases/download/fedaero-latest/handheld-multi-modal.zip">
     Download Package
  </a>
</div>
hide_directive-->

The Handheld Blueprint is a multi-modal AI operator console for handheld deployment
scenarios, optimized for Intel® edge hardware. It combines live camera analytics,
evidence search, mission documents, rule-based alerts, and voice interaction in a
single browser UI that runs entirely on the device.

The Handheld Multi-Modal application is built from unmodified Open Edge Platform
`release-2026.2.0` microservices, open source media and messaging components, and an
application Backend with a browser UI. Video analytics run on the NPU, the language and
speech-to-text models run on the GPU, and text-to-speech runs on the CPU. Models and
dependencies are downloaded from the Internet on the first start. After that, all
inference runs locally and data does not leave the device.

![Operator console: live camera with detection boxes, object tracks with evidence images, open alerts, model placement, and resource telemetry with per-service CPU and memory use](./_assets/operator-ui.jpg)

The Handheld Multi-Modal application is deployed on top of the Edge Node Infrastructure
software - an edge computing platform, which enables hardware acceleration capabilities.
See [Infrastructure Setup](infrastructure-setup.md) for build and provisioning steps.

## Deployment and Usage

Follow these steps to deploy the Handheld (Soldier System) Blueprint:

1. [Infrastructure Setup](infrastructure-setup.md) — Build the OS image, flash it to a bootable USB, and validate the provisioned platform.
2. [Install OEP SDKs](install-oep-sdks.md) — Verify hardware accelerators and install the OEP Vision AI SDK on the provisioned target.
3. [Install Handheld Multi-Modal Application](deploy-applications.md) — Download and deploy the application stack.
4. [Access Application User Interface](access-application.md) — Open the operator console locally or through an SSH tunnel.
5. [Operator Guide](operator-guide.md) — Use cameras, evidence, the assistant, mission documents, alerts, and voice.
6. [Application Operations](application-operations.md) — Lifecycle commands, offline copy to other devices, and model changes.
7. [Benchmarks](benchmarks.md) — Benchmark AI workloads across the available hardware.

## Features

- **Live cameras** — YOLO26s INT8 object detection (NPU) and tracking, annotated WebRTC
  video, and cropped evidence images per tracked object. Cameras are added, renamed, and
  removed from the console.
- **History** — browse recorded tracks and ask questions such as "how many people on
  camera 2 in the last ten minutes?".
- **Mission** — upload PDF or DOCX orders; the Mission Brief and answers are produced by
  `Intel/neural-chat-7b-v3-3` (GPU) through the Chat Q&A microservice.
- **Alerts** — operator rules ("appears", "stays too long", optional zones) dispatched by
  the Alert Agent and spoken by SpeechT5.
- **Voice** — push-to-talk questions transcribed by Whisper-medium (GPU) and spoken answers.
- **Resources** — CPU, memory, GPU, NPU, power, and per-service telemetry.

## Architecture

![Handheld Multi-Modal architecture](./_assets/architecture.svg)

The Backend is never in the video path: MediaMTX relays camera RTSP to the DL Streamer
Pipeline Server, which publishes annotated video back over WebRTC (WHIP) and detection
events over MQTT. The browser plays video directly from MediaMTX (WHEP) and talks to the
Backend over HTTPS, Server-Sent Events, and WebSocket. The running application also serves
an interactive architecture page at `/architecture.html`.

## Components

| Service | Image | Role |
|---------|-------|------|
| DL Streamer Pipeline Server | `intel/dlstreamer-pipeline-server:2026.2.0-ubuntu24` | Detection, tracking, annotated WebRTC, MQTT events, evidence crops |
| Chat Q&A Core | `intel/chatqna:core_gpu_2026.2.0` | Mission documents and assistant answers |
| Audio Analyzer | `intel/audio-analyzer:2026.2.0` | Speech-to-text (Whisper-medium, GPU) |
| Text To Speech | `intel/text-to-speech:2026.2.0` | Spoken alerts and answers (SpeechT5) |
| Alert Agent | `intel/alert-agent-service:2026.2.0` | Rule-mode alert dispatch, no LLM in the alert path |
| Metrics Manager | `intel/metrics-manager:2026.2.0` | Hardware telemetry |
| Model Download | `intel/model-download:2026.2.0` | Prepares the YOLO26s OpenVINO™ IR on the first start |
| MediaMTX | `bluenviron/mediamtx:1.11.3` | RTSP input, WHIP/WHEP relay |
| Coturn | `coturn/coturn:4.18.0` | Loopback-only TURN relay for tunnelled browsers |
| Mosquitto | `eclipse-mosquitto:2.0.22` | MQTT between the pipeline server and the Backend |
| NGINX | `nginx:1.27-alpine` | TLS reverse proxy on `127.0.0.1:8443` |
| FFmpeg | `bluenviron/mediamtx:1.11.3-ffmpeg` | Demo camera publishers only |
| Backend and UI | `handheld/backend:dev` (built locally) | Application logic; calls stock microservice APIs only |

Model sources and licenses are listed in
[third-party-components.spdx](https://github.com/open-edge-platform/federal-and-aerospace-ai-suite/blob/main/handheld-multi-modal/third-party-components.spdx).

## AI Playground

AI Playground is an optional open source generative AI application suite for offline AI chat,
code assistance, document search, image analysis, and image and video generation, running on
the same Edge Node Infrastructure software platform. See the
[AI Playground — FedAero Setup Guide](ai-playground.md) for installation and setup steps.

## AI Agent SKILLs

The Handheld Blueprint supports AI agent skills for GitHub Copilot and Claude Code, covering
both platform-level automation (image build, provisioning, power and thermal tuning) and
application-level pipeline generation with Intel DL Streamer. See
[AI Agent SKILLs](agents.md) for the available skills and how to use them.

<!--hide_directive
:::{toctree}
:hidden:

Infrastructure Setup <infrastructure-setup.md>
Install OEP SDKs <install-oep-sdks.md>
Install Handheld Multi-Modal Application <deploy-applications.md>
Access Application User Interface <access-application.md>
Operator Guide <operator-guide.md>
Application Operations <application-operations.md>
Benchmarks <benchmarks.md>
AI Playground <ai-playground.md>
AI Agent SKILLs <agents.md>
Release Notes <release-notes.md>

:::
hide_directive-->
