<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Makefile Reference

The `Makefile` at the root of `uav-vision-analytics/` provides shorthand targets
for the most common development and deployment tasks.

Run `make help` (or just `make`) to list all targets with descriptions.

## Quick Reference

| Target | Description |
| --- | --- |
| `make init` | Create `.env` from template and auto-detect the host IP, Intel GPU and NPU device paths, and USB cameras |
| `make model` | Download YOLO11s checkpoint and export to OpenVINO™ FP16 |
| `make vlm-model` | Download and convert the VLM captioning model (OpenVINO™ IR) for CPU and GPU |
| `make pymav-build` | Rebuild the locally built Pymavlink images (requires network access) |
| `make pymav-up` | Start the standalone Pymavlink stack (requires model — errors if missing); builds images only if they do not exist yet |
| `make pymav-down` | Stop and remove the Pymavlink stack (includes volumes) |
| `make uavsdk-up` | Start the uav-mission-compute-sdk stack |
| `make uavsdk-down` | Stop and remove the uav-mission-compute-sdk stack (includes volumes) |
| `make start-rtsp` | Start the cameras of `configs/cameras-pymavlink.yaml` (RTSP output) when the vehicle is armed |
| `make check-cameras` | Validate `configs/cameras-pymavlink.yaml` and show what would run |
| `make cameras-status` | Compare `configs/cameras-pymavlink.yaml` with the running pipelines |
| `make build` | Alias for `pymav-up` |

## Target Details

### `make init`

Creates `.env` from `.env.example` (skipped if `.env` already exists) and
auto-detects Intel GPU and NPU device paths, writing them into `.env` so
`docker compose` picks them up automatically.

- **GPU:** scans `/dev/dri/` for `card*` and `renderD*` entries → sets `GPU_DEVICE` and `GPU_RENDER_DEVICE`
- **Host IP:** takes the first address of `hostname -I` → sets `HOST_IP` (falls back to `127.0.0.1`)
- **USB / RealSense camera:** sets `CAMERA_DEVICE_1` (`/dev/null` when none is found)
- **NPU:** scans `/dev/accel/` for `accel*` entries → sets `NPU_DEVICE` (defaults to `/dev/null` if not found, disabling NPU pipelines)

```bash
make init
# .env created from .env.example
# ✅ GPU detected:
#    GPU_DEVICE=/dev/dri/card1
#    GPU_RENDER_DEVICE=/dev/dri/renderD128
# ✅ NPU detected:
#    NPU_DEVICE=/dev/accel/accel0
```

Run this once before the first `make pymav-up`. On machines where the Intel
iGPU is assigned `card1` instead of `card0` (common on multi-GPU desktops),
this avoids the manual `.env` edit.

### `make model`

Both `make model` and `make vlm-model` show the model license and ask for confirmation first (`[y/N]`). Set `ACCEPT_LICENSE=y` to accept without the prompt.

Creates a Python virtual environment under `resources/venv/`, installs
dependencies from `resources/requirements.txt`, downloads the `yolo11s.pt`
checkpoint from Ultralytics, and exports it to OpenVINO™ FP16 IR format.

> [!NOTE]
> `make pymav-up` **checks for the model** before starting containers.
> If `resources/models/yolo11s/yolo11s_openvino_model/yolo11s.xml` is
> missing it prints an error and exits — run `make model` first.

```text
resources/
├── requirements.txt
├── venv/                          ← created by this target
└── models/
    └── yolo11s/
        ├── yolo11s.pt                ← downloaded checkpoint
        └── yolo11s_openvino_model/      ← exported IR (yolo11s.xml + yolo11s.bin)
```

> [!IMPORTANT]
> `ultralytics` is pinned to `8.4.67`. Do not upgrade without
> re-verifying GPU/NPU compatibility — newer versions use a `CumSum`-based
> detection head that fails to compile on Intel GPU and NPU OpenVINO™ plugins.

### `make vlm-model`

Downloads `OpenGVLab/InternVL2-1B` and converts it to OpenVINO™ IR under `resources/ov_models/<device>/`. Needed for camera
captions. The conversion script is fetched from the live-video-captioning app at a pinned commit and cached in `.cache/`
(`FORCE_REFRESH=true` fetches it again). Overrides: `VLM_MODEL_ID`, `VLM_DEVICE` (`cpu,gpu` by default; the VLM does not run on the NPU) and
`VLM_WEIGHT_FORMAT` (`int8` by default).

### `make pymav-up` / `make pymav-down`

Manages the **standalone Pymavlink stack** (`docker-compose-pymavlink.yml`), which includes:

- `dlstreamer-pipeline-server` — AI inference, REST API (`:8081`), RTSP output (`:8555`) — reached via `nginx`
- `broker` — Eclipse Mosquitto MQTT broker (`:1883`)
- `px4` — PX4 SITL flight controller simulator
- `mavlink-router` — MAVLink routing sidecar (receives from PX4 on `:14550`, broadcasts to `:14541`, serves a remote QGroundControl on UDP `:14551` published on `HOST_IP`)
- `metrics-manager` — system metrics endpoint (`:9090`) — reached via `nginx`
- `nginx` — reverse proxy; the only service publishing ports to the host (`:80`, `:443`, `:8555`, `:1883`)

`down` passes `-v` to also remove named volumes (pipeline cache).

Before starting, `pymav-up` (and `uavsdk-up`) runs `scripts/update_device_gids.sh`, which writes the group IDs of the
GPU, NPU, and camera device nodes (`GPU_CARD_GID`, `GPU_RENDER_GID`, `NPU_GID`, `CAMERA_GID`) into `.env`; the compose
files pass them as `group_add`, so the non-root pipeline user can open the devices on any host. Nothing to do by hand.

Containers are named `uav-va-<service>` (for example `uav-va-dlstreamer-pipeline-server`), use those names with `docker exec` / `docker logs`.
The two stacks cannot run together: stop one (`make pymav-down` / `make uavsdk-down`) before starting the other.

Two images are built locally (the DL Streamer Pipeline Server with
`pymavlink`, and `mavlink-router`). `make pymav-up` builds them only the
first time, then starts the stack without rebuilding, so it works without
Internet access once the images and the base images exist locally. Run
`make pymav-build` to rebuild them deliberately, for example after changing
`DLSTREAMER_PIPELINE_SERVER_IMAGE`.

### `make uavsdk-up` / `make uavsdk-down`

Manages the **uav-mission-compute-sdk stack** (`docker-compose-uavsdk.yml`),
which requires the `federal-and-aerospace-ai-suite/uav-mission-compute-sdk`
project to already be running.

Start order:

```bash
# 1. Start the SDK project (provides PX4, MQTT telemetry)
cd ../uav-mission-compute-sdk && make up-sim-camera

# 2. Start this application
cd ../uav-vision-analytics && make uavsdk-up
```

`down` passes `-v` to also remove named volumes.

### `make start-rtsp`

Executes `pipeline_manager.py` inside the running `uav-va-dlstreamer-pipeline-server` container. It monitors the MAVLink
ARMED/DISARMED state and starts/stops one inference pipeline per camera of `configs/cameras-pymavlink.yaml`, each with
**RTSP frame output** on port `:8555` (`rtsp://<HOST_IP>:8555/uav-<camera>`). The file is read when the manager starts
and again at every arming, so after an edit you either run `make start-rtsp` again or disarm and arm. `CAMERAS=<file>`
uses another configuration file.

```bash
make check-cameras     # validate the file and show what would run (nothing is started)
make start-rtsp        # start the cameras on arming
make cameras-status    # file versus running pipelines
```

See [Configure the Cameras](../how-to-customize/camera-management/configure-cameras.md) for
the file format. Requires the DL Streamer Pipeline Server container to already be running
(`make pymav-up` first). Also used in UAV Mission Compute SDK mode: the container's
`CAMERAS_CONFIG` env var (set to `configs/cameras-uavsdk.yaml` by `docker-compose-uavsdk.yml`)
is the default file read there; `CAMERAS=<file>` overrides it in either mode.

### `make build`

Convenience alias for `make pymav-up`. Starts the default standalone stack.

## Common Workflows

### First-time setup

```bash
# 0. Install system prerequisites
sudo apt install python3.12-venv ffmpeg

# 1. Create .env and auto-detect the host IP, GPU, NPU, and camera devices
make init

# 2. Download and export the detection model, and download the VLM used for captions
make model
make vlm-model

# 3. Start the stack
make pymav-up

# 4. Start inference pipelines
make start-rtsp
```

### Stop everything and clean up

```bash
make pymav-down
```

### Switch to uav-mission-compute-sdk mode

```bash
make pymav-down                       # stop standalone stack if running
cd ../uav-mission-compute-sdk && make up-sim-camera   # start SDK project
cd ../uav-vision-analytics && make uavsdk-up          # start uav-vision-analytics against the SDK
make start-rtsp
```
