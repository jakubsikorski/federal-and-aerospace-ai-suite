<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Installation on Ubuntu

This guide installs the UAV Vision Analytics application on an Ubuntu 24.04 target using the
[Open Edge Platform (OEP) CLI Installer](https://github.com/open-edge-platform/edge-ai-libraries/tree/release-2026.3.0/frameworks/oep-installer-cli)
(`openedge-cli`). It is a single self-contained shell script that installs the required system
packages, downloads the application, fetches the models, and builds the images. It deploys the
[Standalone Mode (Pymavlink)](../uav-vision-analytics/deploy/deploy-standalone.md) stack, which runs PX4 SITL and
simulated cameras on the same host.

For the manual path instead, see [Install UAV Vision Analytics Application](../uav-vision-analytics/install-uav-app.md).
To build a customized OS image first, see [Customized Image](../infrastructure/customized-image.md).
To add the browser dashboard on top, see the
[UAV Ground Station installation](../uav-ground-station/installation-on-ubuntu.md).

## Prerequisites

- Ubuntu 24.04 LTS target with Internet access (or a configured proxy).
- Sudo access on the target. The installer prompts for the sudo password unless `SUDO_PASSWORD` or
  `SUDO_ASKPASS` is set, or passwordless sudo is granted.
- An Intel GPU and NPU for the default camera configuration (NPU object detection, GPU captioning).
  See the [camera configuration](../uav-vision-analytics/how-to-customize/camera-management/configure-cameras.md) to change devices.

## Step 1: (Optional) Install the Edge Pack Components

Edge Pack provides platform-level services (EP-IPU, EP-Manageability, and EP-TSN). Install the UAV
blueprint with `--edgepack` to add them:

```bash
curl -fsSL https://raw.githubusercontent.com/open-edge-platform/edge-ai-libraries/refs/heads/release-2026.3.0/frameworks/oep-installer-cli/rendered/openedge-cli-federal-aerospace | bash -s -- install uncrewed_aerial_vehicle_blueprint --edgepack
```

During installation the system might reboot. After it is back online, follow the instructions from
the installer to resume. If the system did not reboot, go to the next step.

## Step 2: Install UAV Vision Analytics

```bash
curl -fsSL https://raw.githubusercontent.com/open-edge-platform/edge-ai-libraries/refs/heads/release-2026.3.0/frameworks/oep-installer-cli/rendered/openedge-cli-federal-aerospace | bash -s -- install uncrewed_aerial_vehicle_vision_analytics
```

The installer:

- installs Docker, Git, Make, and the GPU and NPU runtime packages if missing;
- clones the `release-2026.3.0` source into
  `~/.local/open-edge-platform/uav-mission-compute-sdk/vision-analytics/uav-vision-analytics`;
- generates `.env` and sets `HOST_IP` to the detected address of the machine;
- downloads the YOLO11s detection model and the InternVL2-1B captioning model;
- pulls and builds the container images.

> [!NOTE]
> This step downloads and converts models, so it takes a while.

After the first run, the installer saves itself as `~/.local/bin/openedge-cli`. Open a new shell (or add
`~/.local/bin` to `PATH`) and call `openedge-cli` directly for the remaining steps.

To install the whole UAV profile (blueprint, ground station, and vision analytics) instead:

```bash
openedge-cli install federal_and_aerospace_ai_suite_uncrewed_aerial_vehicle
```

## Step 3: Start the Application

Profiles cannot be started as a group; start the individual component:

```bash
openedge-cli start uncrewed_aerial_vehicle_vision_analytics
```

The command starts the standalone stack, waits until the services are healthy, and starts the
cameras immediately. You do not need to arm the vehicle.

## Step 4: Verify the Streams

```bash
cd ~/.local/open-edge-platform/uav-mission-compute-sdk/vision-analytics/uav-vision-analytics
make cameras-status
```

All three cameras should report `RUNNING` at about 30 fps. The annotated streams are published at:

```text
rtsp://<HOST_IP>:8555/uav-front-camera
rtsp://<HOST_IP>:8555/uav-bottom-camera
rtsp://<HOST_IP>:8555/uav-back-camera
```

Continue with [How-To Guides](../uav-vision-analytics/how-to-guides.md) to view them, connect QGroundControl, or add the ground station.

## Stop the Application

```bash
openedge-cli stop uncrewed_aerial_vehicle_vision_analytics
```

## Removing Components

Remove the application and its workspace. This does not remove dependencies such as the GPU and NPU
drivers or Docker; pass `--remove-all` to force removing everything:

```bash
openedge-cli remove uncrewed_aerial_vehicle_vision_analytics
```

## Related Guides

- [OEP CLI Installer README](https://github.com/open-edge-platform/edge-ai-libraries/blob/release-2026.3.0/frameworks/oep-installer-cli/README.md): full command reference, options, and credentials handling.
- [Install UAV Vision Analytics Application](../uav-vision-analytics/install-uav-app.md): manual deployment of the Standalone and UAV Mission Compute SDK modes.
- [Customized Image](../infrastructure/customized-image.md): build a customized Ubuntu image and install it from a bootable USB stick.
