<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# System Requirements

This section provides the hardware, software, and platform requirements to help you set up and run the application efficiently.

## Supported Platforms

**Operating Systems**

- Ubuntu 24.04 LTS
- Ubuntu 26.04 LTS

## Minimum Requirements

|                 |                                                              |
| --------------- | ------------------------------------------------------------ |
| Processor       | Intel® Core™ Ultra Processor Series 3                        |
| Memory          | 16 GB                                                        |
| Disk Space      | 64 GB                                                        |
| GPU/Accelerator | Integrated GPU (scene captions in the default configuration) |
| NPU             | Required for object detection in the default configuration   |

> [!NOTE]
> Users can also create apps tailored to their use case using models supported by DL Streamer.
> Check [the list of supported models](https://docs.openedgeplatform.intel.com/dev/edge-ai-libraries/dlstreamer/supported_models.html) for the latest information.

## Software Requirements

**Required Software**:

- Docker 27.3.1 or higher, with Docker Compose v2 (`docker compose`, not `docker-compose`)
- Python 3.10+
- Git
- `ffmpeg` (for RTSP stream playback and recording)
- `python3-venv` (for creating a Python virtual environment)

> [!NOTE]
> `python3-venv` is required by `make model` to create a Python virtual environment.
> `ffmpeg` provides `ffplay` for viewing the RTSP output stream and `ffmpeg` for recording.

<!--
## Compatibility Notes
**Known Limitations**:
- GPU optimizations require Intel® integrated graphics or compatible accelerators.
-->

## Accelerator Requirements

The shipped `configs/cameras-pymavlink.yaml` runs object detection on the NPU for every camera and scene captions on the GPU for the front and bottom cameras. A system without these accelerators cannot run the cameras as shipped. To run on other hardware, choose the CPU or GPU for each camera, see [Configure the Cameras](../uav-vision-analytics/how-to-customize/camera-management/configure-cameras.md).

## Validation

- Ensure all dependencies are installed and configured before proceeding to
  [Deploy in Standalone Mode](../uav-vision-analytics/deploy/deploy-standalone.md) or
  [Deploy in UAV Mission Compute SDK Mode](../uav-vision-analytics/deploy/deploy-uavsdk.md).
