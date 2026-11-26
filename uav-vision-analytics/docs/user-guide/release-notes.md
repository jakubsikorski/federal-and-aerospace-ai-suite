# Release Notes: Uncrewed Aerial Vehicle (UAV) Blueprint

## Version 2026.3.0

**Release Date**: December 10, 2026

**New**:

- **Camera files** (`configs/cameras-*.yaml`) define the cameras of the application: the video file or live
  camera, the detection model, the CPU, GPU, or Neural Processing Unit (NPU) device, the overlay, and
  optional captioning. `make check-cameras` validates the file and shows the pipeline each camera uses, and
  `make start-rtsp` starts the cameras and re-reads the file every time the vehicle is armed.
- **Scene captions** from a vision-language model (InternVL2-1B, fetched with `make vlm-model`) can run next to
  detection on any camera and are published over Message Queuing Telemetry Transport (MQTT) on
  `uav/caption/<camera>`.
- **NPU inference** for YOLO11s detection, alongside the existing CPU and GPU pipelines.
- **UAV Ground Station** ([`uav-ground-station`](https://github.com/open-edge-platform/federal-and-aerospace-ai-suite/tree/main/uav-ground-station/README.md)),
  a viewer for a UAV host on a separate machine. It relays the H.264 streams to the browser over WebRTC, shows
  captions and host utilization, supports several UAVs in one session, builds Flight Summary reports, and can
  add Live Natural Language Search with the Video Search and Summarization (VSS) stack. See the
  [Ground Station release notes](./uav-ground-station/release-notes.md).
- **Simpler setup**: `make init` detects the host IP from the default route and the GPU, NPU, and camera group
  IDs, and `make start-rtsp` picks up the device configured on a first run.
- **1080p sample videos at 30 frames per second** for the front, bottom, and back cameras.
- **Updated components**: Deep Learning Streamer Pipeline Server `2026.3.0-ubuntu24` and Metrics Manager
  `2026.3.0`. PX4 SITL is pinned to `v1.18.0-beta1-660-g61907962c9`.
- **Documentation and agent skills** cover camera and model customization, captioning, QGroundControl, multi-UAV
  operations, VSS live search, and troubleshooting; the agent skills include captioning, VSS, and model
  customization.

**Known issues and limitations**:

- The application is intended for development and evaluation, not production deployment.
- Camera streams are available only while the UAV is armed; clients may receive an RTSP 404 error when the UAV
  is disarmed.
- Starting the cameras immediately after stopping them can leave a camera without video on the ground station
  until you restart the pipeline server container (`docker restart uav-va-dlstreamer-pipeline-server`). Wait
  about a minute before starting the cameras again to avoid this issue.
- A failed pipeline start poisons its model instance until the pipeline server restarts; see
  [Troubleshooting](./uav-vision-analytics/troubleshooting.md).
- Pymavlink and UAV Mission Compute SDK modes cannot run at the same time, because both use ports `80`, `443`,
  and `8555`.
- The first `make model` and `make vlm-model` runs download models and can take several minutes.

## Version 2026.2.0

**Release Date**: September 10, 2026

**New**:

- **Initial SDK release** for local, single-user UAV edge AI development and evaluation.
  - **Two camera input modes**: simulated 3-camera Gazebo profile and real V4L2 USB profile with Docker Compose profile-based mutual exclusion.
  - **RTSP-first video pipeline** via MediaMTX with H.264 streams for raw and processed feeds.
  - **Companion telemetry and control bridge** exposing MAVLink-to-MQTT telemetry plus REST command APIs.
  - **Intel GPU AI inference pipeline** using OpenVINO™ + DL Streamer (YOLOv2-tiny), with detections published to MQTT topics.
  - **Observability stack included by default** (InfluxDB + Grafana + telemetry extractors), with lean startup targets available for reduced memory usage.
  - **Remote PX4 over Ethernet evaluation workflow** is available for testing companion compute with a separate flight-controller host.
  - **Benchmarking workflows** for passive telemetry, bridge stress sweep, and client scaling sweep, including optional HTML reports.
  - **Agent Commands and MCP Tools** for SDK usability and assistance in application development.
- **UAV Vision Analytics application** is now available as a standalone application and integrates with MAVLink telemetry provided by the UAV Mission Compute SDK.
- **Use Case Implementation**: Includes the necessary code to test both the standalone application and the UAV Mission Compute SDK use case.
- **Documentation**: Comprehensive documentation on how to set up, configure, and run tests for the use case.

**Known issues and limitations**:

- The SDK is intended for development and evaluation, not production deployment.
- In Ethernet mode, MAVLink UDP and MQTT are not authenticated/encrypted by default; additional transport and broker security is required outside trusted networks.
- Camera streams are available only while the UAV is armed; clients may see RTSP 404 when disarmed.
- Simulated and USB camera bridges cannot run simultaneously.
- First-run image builds can take approximately 10-15 minutes.
