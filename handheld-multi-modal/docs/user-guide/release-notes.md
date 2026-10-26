# Release Notes: Handheld (Soldier System) Blueprint

## Version 2026.3.0

**Release Date**: TBD

The Handheld Multi-Modal Application is rebuilt as a localhost-only operator console that
fuses live video analytics, telemetry, speech, and a local language model on a single device.
All services are Open Edge Platform microservices orchestrated by Docker Compose and a Make
lifecycle.

**New**:

- Operator console on <https://localhost:8443/> with live annotated cameras over WebRTC,
  searchable detection history and evidence images, mission documents, alerts, voice
  questions and spoken answers, and resource monitoring.
- YOLO26s INT8 detection in Deep Learning Streamer Pipeline Server on NPU (default), GPU, or CPU,
  with verified model digests.
- Chat Q&A assistant grounded in detection history, telemetry, and uploaded mission documents.
- Speech input with Audio Analyzer (Whisper) and spoken answers with Text To Speech (SpeechT5).
- Alert rules evaluated by Alert Agent Service and platform metrics from Metrics Manager.
- Cameras added at runtime from the console: network RTSP cameras or local publishers.
- Demo mode (`make demo-up`) with checksum-pinned sample videos and an example mission.
- Readiness check (`make check`) and offline copy to other devices (`make bundle`,
  `make bundle-load`).
- Bundled AI agent skills in `.agents/skills`.
- The Visual Pipeline and Platform Evaluation Tool, OpenVINO Model Server, Open WebUI, and the
  Grafana dashboard used by the previous version are removed.

**Known issues and limitations**:

- All ports bind to `127.0.0.1` and the console has no authentication. Remote access is
  supported only through an SSH tunnel.
- The first start downloads and converts models and can take up to an hour.
- The Backend container runs as root to manage its crop store; Compose drops its capabilities.
- The demo camera publisher uses a third-party FFmpeg container image that is pulled by the
  user and is not redistributed with the application.

## Version 2026.2.1

**Release Date**: September 10, 2026

**New**:

- Added new Single Pane Page on port :443 and moved ViPPET to port :1443.
- Updated ViPPET to latest available version.
- Security hardening.

**Known issues and limitations**:

- When the virtual function is used for GPU, metrics in the
  Visual Pipeline and Platform Evaluation Tool are not available. The metrics are exposed
  correctly when the physical function is used.
- Users may receive pop-up notifications prompting them to upgrade from Ubuntu 24.04 LTS to
  Ubuntu 26.04.1 LTS. Follow the
  [Troubleshooting guide](./infrastructure/troubleshooting.md) to disable this.


## [Edge-Node Infrastructure software](https://github.com/open-edge-platform/edge-node-infrastructure-blueprint/releases)

<!--hide_directive
:::{toctree}
:hidden:

Release Notes 2026.1 <./release-notes/release-2026.1.md>

:::
hide_directive-->
