# Federal and Aerospace AI Suite

AI-enabled applications and supporting components for aerospace and defense edge deployments.

## Applications

### Handheld Multi-Modal

The Handheld Multi-Modal application is an operator console for Intel edge devices that fuses
live camera analytics, evidence search, mission documents, alerts, and speech. It is built from
Open Edge Platform microservices — DL Streamer Pipeline Server with YOLO26s detection on the NPU,
Chat Q&A with a local LLM on the GPU, Audio Analyzer (Whisper) for speech-to-text, and Text To
Speech (SpeechT5) — and runs entirely on the device. It can be deployed on top of the
Edge Node Infrastructure software - an edge computing platform, which enables hardware
acceleration capabilities. See [Infrastructure Setup](handheld-multi-modal/docs/user-guide/infrastructure-setup.md) for build and provisioning steps.

See [`handheld-multi-modal/`](handheld-multi-modal/README.md).

### Deterministic Threat Detection (Preview)

[Deterministic Threat Detection](deterministic-threat-detection) : A sample application that showcases Time-Sensitive Networking (TSN) to enable deterministic, low-latency transmission of AI-processed video and sensor data alongside best-effort traffic on a shared network. This application is currently in preview. [User Docs](deterministic-threat-detection/docs/user-guide/index.md)



### UAV Vision Analytics

[UAV Vision Analytics](uav-vision-analytics) : An AI-powered UAV object detection application with live telemetry overlay, built on Intel DL Streamer Pipeline Server. It processes video from a UAV-mounted camera (or simulated video file), runs YOLO11s inference across 80 object classes, and overlays correlated MAVLink telemetry (GPS, altitude, speed, heading) on the output RTSP stream. Supports standalone (pymavlink + PX4 SITL) and UAV Mission Compute SDK deployment modes.

See [`/uav-vision-analytics/docs/user-guide`](./uav-vision-analytics/docs/user-guide/index.md).


## Components

| Directory                               | Description                                |
|-----------------------------------------|--------------------------------------------|
| `handheld-multi-modal/`            | Handheld multi-modal operator console application |
| `deterministic-threat-detection/`  | Deterministic threat detection application (Preview) |
| `uav-vision-analytics/`            | UAV vision analytics application           |
| `docs/`                                 | Documentation                              |

## Contribute

Read the [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on submitting issues and pull requests.

## Community and Support

For support, submit your bug report and feature request to [Github Issues](https://github.com/open-edge-platform/federal-and-aerospace-ai-suite/issues).

## License

The **Federal and Aerospace AI Suite** project is licensed under the [APACHE 2.0](LICENSE).  

## Intended Use

Applications developed in this repository, unless stated otherwise, are intended for reference
and demonstration purposes, not for production environments.
Certain features, such as authentication, TLS termination, and external access controls are
assumed to be covered at the infrastructure level.

For more information, refer to the
[Notes on Usage](https://docs.openedgeplatform.intel.com/dev/OEP-articles/notes-on-usage.html)
document.
