<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Installation on Ubuntu

This guide installs the UAV Ground Station on an Ubuntu 24.04 machine using the
[Open Edge Platform (OEP) CLI Installer](https://github.com/open-edge-platform/edge-ai-libraries/tree/release-2026.3.0/frameworks/oep-installer-cli)
(`openedge-cli`). It is a single self-contained shell script that installs the required system
packages, downloads the application, and builds the images, including the optional VSS search overlay.

For the manual `make` and Docker Compose path instead, see [Get Started](./get-started.md).
To build a customized OS image first, see [Customized Image](../infrastructure/customized-image.md).

## Prerequisites

- Ubuntu 24.04 LTS machine with Internet access (or a configured proxy).
- Sudo access. The installer prompts for the sudo password unless `SUDO_PASSWORD` or `SUDO_ASKPASS`
  is set, or passwordless sudo is granted.
- A UAV host running [UAV Vision Analytics](../uav-vision-analytics/installation-on-ubuntu.md) for the dashboard to connect to.
  It can run on a different machine (the usual case) or on this one.

## Step 1: Install the Ground Station

```bash
curl -fsSL https://raw.githubusercontent.com/open-edge-platform/edge-ai-libraries/refs/heads/release-2026.3.0/frameworks/oep-installer-cli/rendered/openedge-cli-federal-aerospace | bash -s -- install uncrewed_aerial_vehicle_ground_station
```

The installer:

- installs Docker, Git, Make, and the video utilities if missing;
- clones the `release-2026.3.0` source into
  `~/.local/open-edge-platform/uav-mission-compute-sdk/ground-station/uav-ground-station`;
- generates `.env`, sets the default VSS models (`CLIP/clip-vit-b-32` and `OpenVINO/yolox_s-int8-ov`),
  and sets `VIZ_PUBLIC_HOST` to the detected address of the machine;
- pulls and builds the container images.

After the first run, the installer saves itself as `~/.local/bin/openedge-cli`. Open a new shell (or add
`~/.local/bin` to `PATH`) and call `openedge-cli` directly for the remaining steps.

To install the whole UAV profile (blueprint, ground station, and vision analytics) instead:

```bash
openedge-cli install federal_and_aerospace_ai_suite_uncrewed_aerial_vehicle
```

## Step 2: Start the Ground Station

Profiles cannot be started as a group; start the individual component:

```bash
openedge-cli start uncrewed_aerial_vehicle_ground_station
```

The command starts the dashboard with the VSS search overlay, waits until the services are ready, and
prints the dashboard address, for example `http://<HOST_IP>:8080`.

## Step 3: Connect a UAV Host

1. Open the dashboard address in a browser.
2. Select **Add UAV**, enter the address of the host running UAV Vision Analytics, and give it a label.
3. The live video, captions, and utilization charts appear. Live indexing starts for the cameras, so
   **Search Videos** and **Flight Summary** become available as footage is indexed.

See [Get Started](./get-started.md) for search, multiple UAVs, and Flight Summary.

> [!NOTE]
> When both applications run on the same machine, connect the dashboard to the machine's LAN address
> (not `127.0.0.1`), because UAV Vision Analytics publishes its ports on `HOST_IP` only.

## Stop the Ground Station

```bash
openedge-cli stop uncrewed_aerial_vehicle_ground_station
```

> [!NOTE]
> Stopping also removes the application's containers and volumes, including the VSS index of the
> indexed footage. The next start builds a fresh index.

## Removing Components

Remove the application and its workspace. This does not remove dependencies such as Docker; pass
`--remove-all` to force removing everything:

```bash
openedge-cli remove uncrewed_aerial_vehicle_ground_station
```

## Related Guides

- [OEP CLI Installer README](https://github.com/open-edge-platform/edge-ai-libraries/blob/release-2026.3.0/frameworks/oep-installer-cli/README.md): full command reference, options, and credentials handling.
- [Get Started](./get-started.md): manual deployment, VSS search, multiple UAVs, and Flight Summary.
- [Configuration](./configuration.md): every `.env` variable and the backend route reference.
