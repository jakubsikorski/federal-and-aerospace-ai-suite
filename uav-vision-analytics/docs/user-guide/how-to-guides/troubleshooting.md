<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Troubleshooting

If you face a problem while working with UAV Vision Analytics, you can reach out to its
maintainers in the [Discussions section](https://github.com/open-edge-platform/federal-and-aerospace-ai-suite/discussions),
but first, see if the following list of tips answers your questions. They are
grouped into the following categories:

**Table of contents:**

- [Setup & Installation](#setup--installation)
- [Stack & Containers](#stack--containers)
- [Pipelines](#pipelines)
- [Cameras & Video](#cameras--video)
- [Models & Captioning](#models--captioning)
- [Benchmark](#benchmark)
- [QGroundControl](#qgroundcontrol)
- [VSS Live Search](#vss-live-search)
- [Multi-UAV Ground Station](#multi-uav-ground-station)

## Setup & Installation

### `make model` fails — `python3-venv` not available

**Symptom:**

```text
The virtual environment was not created successfully because ensurepip is not available.
On Debian/Ubuntu systems, you need to install the python3-venv package using the following command.
    apt install python3.12-venv
Failing command: .../resources/venv/bin/python3
make: *** [Makefile:28: model] Error 1
```

**Resolution:** Install the `python3-venv` package and re-run:

```bash
sudo apt install python3.12-venv
make model
```

### `make pymav-up` fails — pip install cannot reach PyPI

**Symptom:**

```text
WARNING: Retrying after connection broken by 'NewConnectionError([Errno 101] Network is unreachable)': /simple/pymavlink/
ERROR: Could not find a version that satisfies the requirement pymavlink
```

**Cause:** The Docker build container for `dlstreamer-pipeline-server`
(which runs `pip install pymavlink`) does not have proxy environment variables set.
`https_proxy` set in `/etc/environment` on the host is not automatically
inherited by Docker build containers.

**Resolution:** Pass proxy variables as build args in
`docker-compose-pymavlink.yml` for the `dlstreamer-pipeline-server` service:

```yaml
services:
  dlstreamer-pipeline-server:
    build:
      context: .
      args:
        http_proxy:  ${http_proxy:-}
        https_proxy: ${https_proxy:-}
        no_proxy:    ${no_proxy:-localhost,127.0.0.0/8}
      dockerfile_inline: |
        FROM ${DLSTREAMER_PIPELINE_SERVER_IMAGE}
        ARG http_proxy
        ARG https_proxy
        ARG no_proxy
        RUN pip install --no-cache-dir pymavlink
```

### `make pymav-up` fails — `/dev/dri/card0: no such file or directory`

On some machines the Intel iGPU is assigned `card1` instead of `card0` (e.g.,
when another GPU or firmware device claims `card0` first). Run `init`
to auto-detect the correct paths:

```bash
make init          # detects /dev/dri/card* and /dev/dri/renderD* and writes them to .env
make pymav-up
```

To verify the detected device belongs to the Intel iGPU:

```bash
ls -la /sys/class/drm/ | grep card
# card1 -> .../0000:00:02.0/drm/card1  ← Intel iGPU at PCI 00:02.0
```

If `make init` already ran (`.env` exists), edit `.env` manually:

```bash
GPU_DEVICE=/dev/dri/card1
GPU_RENDER_DEVICE=/dev/dri/renderD128
```

### `ffplay: command not found`

**Symptom:**

```text
ffplay rtsp://<HOST_IP>:8555/uav-front-camera
Command 'ffplay' not found, but can be installed with:
sudo apt install ffmpeg
```

**Resolution:** `ffplay` is part of the `ffmpeg` package:

```bash
sudo apt install ffmpeg

# Then verify RTSP stream
ffplay rtsp://<HOST_IP>:8555/uav-front-camera
```

To view the output stream without `ffplay` (e.g., on a headless server),
record it instead:

```bash
ffmpeg -rtsp_transport tcp \
  -i "rtsp://<HOST_IP>:8555/uav-front-camera" \
  -c copy -t 30 output.mkv
```

## Stack & Containers

### DL Streamer container keeps restarting

- Check logs: `docker logs uav-va-dlstreamer-pipeline-server`
- Verify the model files exist:

  ```bash
  docker exec uav-va-dlstreamer-pipeline-server ls \
    /home/pipeline-server/resources/models/yolo11s/yolo11s_openvino_model/
  ```

- Confirm `HOST_IP` is set correctly in `.env`.
- If the model is missing, run `make model`, then restart the stack:

  ```bash
  make pymav-down && make pymav-up
  ```

### PX4 SITL — image pull or runtime issues

**Symptom:** The `px4` service fails to start or behaves unexpectedly with the `latest` tag.

**Resolution:** Pin the PX4 SITL image to a known-good digest in `docker-compose-pymavlink.yml`:

```diff
-image: px4io/px4-sitl:latest
+image: px4io/px4-sitl@sha256:01866d912ac22ca6119a996b830cf628a6d47dfb60fdccc41cd9f44b62935a44
```

## Pipelines

### `make start-rtsp` fails in uav-mission-compute-sdk mode — `ConnectionRefusedError` / RTSP `404 Not Found`

**Symptom:**

```text
paho.mqtt... ConnectionRefusedError: [Errno 111] Connection refused
```

or

```text
[rtsp @ ...] method DESCRIBE failed: 404 Not Found
Error opening input file rtsp://localhost:8555/nadir.
```

**Cause:** The SDK's `.env` defaults to `HOST_IP=127.0.0.1`, which binds MQTT
(port `1883`), MediaMTX RTSP (port `8554`), and all other published ports to
loopback only. `uav-vision-analytics` runs in a separate Docker container and
cannot reach loopback-bound ports on the host.

**Resolution:** In the `uav-mission-compute-sdk` directory, set
`HOST_IP=0.0.0.0` in `.env` **before** starting the stack:

```bash
cd edge-ai-suites/federal-and-aerospace-ai-suite/uav-mission-compute-sdk
sed -i 's|^HOST_IP=.*|HOST_IP=0.0.0.0|' .env
make down && make up-sim-camera
```

See [Get Started (UAV Mission Compute SDK Mode) — Step 1](../get-started/get-started-uavsdk.md#1-start-the-uav-mission-compute-sdk).

### No telemetry overlay on stream (all zeros)

**pymavlink mode:** Confirm `mavlink-router` is running and forwarding MAVLink from PX4:

```bash
docker logs uav-va-mavlink-router
docker logs uav-va-px4 | grep -i mavlink
```

**uav-mission-compute-sdk mode:** Confirm the SDK MQTT broker is reachable and publishing telemetry:

```bash
mosquitto_sub -h localhost -p 1883 -t "uav/uav-1/telemetry/#" -v
```

### Pipelines not starting in uav-mission-compute-sdk mode

- Confirm `pipeline_manager.py` is running inside the container:

  ```bash
  docker exec uav-va-dlstreamer-pipeline-server ps aux | grep pipeline
  ```

- Check that the RTSP sources from the SDK are available:

  ```bash
  ffprobe rtsp://localhost:8554/uav-1/nadir
  ```

- Verify the UAV is armed — pipelines only start on ARMED state.

### NPU inference fails

**Symptom A — pipeline skipped at startup:**

```text
[pipeline] Skipping 'uav_object_detection_npu': NPU_DEVICE not available.
```

**Cause:** `NPU_DEVICE` is not set in `.env` (or set to `/dev/null`). This
happens when `.env.example` lacked a `NPU_DEVICE=` placeholder and `make init`
could not write the detected value.

**Resolution:**

```bash
# Check if NPU device exists on the host
ls /dev/accel/
# If it does, add it to .env:
echo "NPU_DEVICE=/dev/accel/accel0" >> .env
# Then restart the stack so the container gets the updated device:
make pymav-down && make pymav-up
```

**Symptom B — pipeline returns error about `model-instance-id`:**

```text
Cannot start pipeline. gvadetect element uses model-instance-id: instnpu0
that errored out on a prior run due to incorrect parameters.
```

**Cause:** A previous NPU pipeline attempt failed (e.g. device not mounted,
wrong driver), and DLPS keeps the model instance in a poisoned state until the
container is restarted.

**Resolution:** Restart the DLPS container to clear the poisoned instance:

```bash
docker restart uav-va-dlstreamer-pipeline-server
# Wait ~15 s for container to become healthy, then retry
```

**Other checks:**

- Confirm `ZE_ENABLE_ALT_DRIVERS=libze_intel_npu.so` is set (it is by default
  in the compose files).
- Check that the NPU device node is available: `ls /dev/accel*`
- Verify driver version: `dmesg | grep -i npu`

### GPU pipeline falls back to CPU

- Confirm device group IDs are present: `getent group | grep -E '^(video|render)'`
- The compose files add groups `44`, `109`, `110` for video/render device access.
- Check for the render node: `ls /dev/dri/renderD128`

### Pipeline fails with `gst_parse_error: no element "vah264enc"`

Replace `vah264enc` with `vah264lpenc`

```bash
{"levelname": "ERROR", "asctime": "2026-08-15 11:53:30,507", "message": "Error on Pipeline ef2c39be989f11f189d8c9d3068f2a21: gst_parse_error: no element \"vah264enc\" (1)", "module": "gstreamer_pipeline"}
```

### REST API returns `404 Not Found` on POST

The pipeline name in the URL is not in the loaded config file (names are
case-sensitive). List them with the command in
[Manage Pipelines — List the available pipelines](../how-to-customize/pipeline-management/manage-pipelines.md#list-the-available-pipelines).

### REST API returns `400` on POST

A `parameters` value does not match its schema `pattern` in the config file,
for example a `detection_model` outside `/home/pipeline-server/resources/models/`
(use the container path, not a host path), a `video` name with a folder in
it, or a `captioner_prompt` with characters other than letters, digits,
spaces and `, . ? ' -`. The schemas are listed in
[Modify Pipelines — The parameters schema](../how-to-customize/pipeline-management/modify-pipelines.md#the-parameters-schema).

### `model-instance-id ... errored out on a prior run`

A previous start with the same `detection_instance_id` failed (wrong device,
unsupported model, device not mounted) and the pipeline server keeps that
instance poisoned until it restarts:

```bash
docker restart uav-va-dlstreamer-pipeline-server     # wait ~15 s, then start the pipeline again
```

### `RTSP Stream ... already exists` right after stopping a pipeline

The stream path stays registered for a few seconds after a `DELETE`
returns. Wait and retry; the pipeline manager retries automatically.

### Pipeline stays `QUEUED`

It is waiting for a resource another instance holds: the same
`detection_instance_id` (two hand-started pipelines reusing the default
`instcpu0`), or the same `/dev/video*` device. Stop the other instance or
use a different id/device.

### A config-file edit (`configs/config-*.json`) has no effect

The pipeline server reads the file only at container start; recreate it with
`make pymav-down && make pymav-up` (or the `uavsdk-*` targets). If the
container then restarts in a loop, the JSON is malformed — see
[DL Streamer container keeps restarting](#dl-streamer-container-keeps-restarting).

### `make check-cameras` reports `pipeline ... is not in the pipeline server's config.json`

The camera's device/VLM combination has no pipeline of the derived name
(e.g. a live camera on `cpu`, whose pipelines ship in
`configs/config-pymavlink-realsense.json` only). Copy the pipeline into the
loaded config and recreate the container, or change the camera's devices;
see [Modify Pipelines — Add a pipeline](../how-to-customize/pipeline-management/modify-pipelines.md#add-a-pipeline).

### Rapid arm/disarm flapping leaves a stream down (SITL)

A re-arm loop whose interval is close to PX4's idle-disarm timeout flaps the
vehicle between armed and disarmed, repeatedly starting and stopping the
pipelines. Use a clearly shorter re-arm interval; if a stream stays down,
stop and start that pipeline by hand
([Manage Pipelines](../how-to-customize/pipeline-management/manage-pipelines.md)).
This pattern is for SITL testing only.

### Both stacks start but only one streams

pymavlink and UAV Mission Compute SDK mode both bind `80`/`443`/`8555`;
only one `nginx` can hold them. Run `make pymav-down` or `make uavsdk-down`
for the mode not in use.

### Telemetry overlay shows zeros right after switching modes

The two modes use different `gvapython` overlay scripts; values appear once
the new mode's receiver gets its first MAVLink/MQTT message (a few seconds).
If they stay at zero, see
[No telemetry overlay on stream](#no-telemetry-overlay-on-stream-all-zeros).

### Detection log (JSONL) is missing or empty

Only a pipeline started by hand with a `destination.metadata` block writes
one; the camera-file manager does not add it. See [View Detection Logs](./view-detection-logs.md).

## Cameras & Video

### `make check-cameras` reports `video not found`

The file is not in `resources/videos`, or `video:` includes a folder. Only a
file name is accepted.

### Pipeline goes to `ERROR` with a new video file

The pipelines expect H.264 in an `.avi`/`.mp4`/`.mkv`/`.ts` container.
Convert the file with `scripts/convert_video.sh` rather than copying the
original — see [Use Your Own Videos](../how-to-customize/model-and-video-management/use-your-own-video.md).

### `scripts/convert_video.sh` errors

| Message | Fix |
| --- | --- |
| `Output exists` | Add `-f` or choose another `-o` |
| `Could not read a video stream` | The input is not a video file, or its first stream is not video |
| `--frames N is more than the input provides` | Use a smaller N; the clip is shorter than N / 30 s |
| `Neither ffmpeg nor docker is installed` | Install Docker or `sudo apt-get install -y ffmpeg` |

### Replaced a video file but the stream still shows the old footage

A running pipeline keeps the file it opened. Disarm/arm (or stop and start
the pipeline) so it re-opens the file.

### Live camera (`device:`) is skipped or not found

The node must be listed in `.env` (`CAMERA_DEVICE_1` … `CAMERA_DEVICE_4`)
and the container recreated (`make pymav-up`) for it to be visible inside;
use the stable `/dev/v4l/by-id/` path and the camera's colour node (YUYV or
UYVY format). See
[Configure the Cameras — Live cameras](../how-to-customize/camera-management/configure-cameras.md#live-cameras-realsense-usb-webcams).

## Models & Captioning

### `make check-cameras` reports `detection model not found` / `captioning model not found`

The path in `detection.model` is relative to `resources/models` and must
name the `.xml`; `captioning.model` is a folder name under
`resources/ov_models/<device>/` and must exist for the device the camera
uses (`cpu` or `gpu`). Run `make model` / `make vlm-model` if the bundled
files are missing.

### Model loads on CPU but fails on GPU or NPU

The exported IR uses an operation those plugins do not support — typically
a detection model exported with `ultralytics` newer than the pinned `8.4.67`.
Re-export with the pinned tools (`rm -rf resources/venv && make model`) or
keep that camera on `cpu`. See
[Manage the Detection Model](../how-to-customize/model-and-video-management/model.md).

### No detections with a new detection model

The `threshold=0.4` in the pipeline string may be too high for the model's
confidence distribution; lower it as shown in
[Modify Pipelines — Change an element's property](../how-to-customize/pipeline-management/modify-pipelines.md#change-an-elements-property).

### Captioning pipeline goes to `ERROR` at start

The model folder is not a complete OpenVINO IR export of the VLM, or the
architecture is not supported by the `openvino_genai` version in the
pipeline-server image. `docker logs uav-va-dlstreamer-pipeline-server` shows the
loader error; revert `captioning.model` and re-arm to recover. The required
file set is listed in
[Change the Captioning Model — Required format](../how-to-customize/model-and-video-management/vlm-model.md#required-format).

### Captions arrive slowly or stop

The VLM takes longer than the 5 s frame interval per caption. Reduce
`max_new_tokens`, move captioning to the GPU (`captioning.device: gpu`), or
use a smaller model; several captioning cameras share one GPU.

### Overlay script edit has no effect

`gvapython` modules are loaded when the pipeline starts; stop and start the
pipeline (or the stack) after editing `gvapython/telemetry-overlay-*.py`. A
`KeyError` in `process_frame()` means a key used in `lines` was not added to
`latest_data`.

## Benchmark

### `jq: command not found`

`jq` is not installed on the benchmark host. Two options:

```bash
# Option 1: install via apt (requires sudo)
sudo apt-get install -y jq

# Option 2: docker exec wrapper (no root needed, works when DL Streamer Pipeline Server container is running)
mkdir -p ~/.local/bin
cat > ~/.local/bin/jq << 'EOF'
#!/usr/bin/env bash
CONTAINER="uav-va-dlstreamer-pipeline-server"
args=()
for arg in "$@"; do
  if [[ -f "$arg" ]]; then
    cat "$arg" | docker exec -i "$CONTAINER" jq "${args[@]}"
    exit $?
  else
    args+=("$arg")
  fi
done
docker exec -i "$CONTAINER" jq "${args[@]}"
EOF
chmod +x ~/.local/bin/jq
export PATH="$HOME/.local/bin:$PATH"
# To make permanent:
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
```

### `gawk: command not found`

```bash
sudo apt-get install -y gawk
```

### `DL Streamer Pipeline Server is not reachable at https://<HOST_IP>`

`calc_stream_density.sh` finds the nginx address from `docker port nginx`, then `../.env`
(`HOST_IP`), then the first host address. If it reports the wrong address or an unreachable
server, the stack is not running (`make pymav-up`) or nginx is published elsewhere. `nginx`
binds only to `HOST_IP` (not `0.0.0.0`), so `localhost`/`127.0.0.1` never reach it once
`HOST_IP` is set to a real address.

If you're running the benchmark from a **different machine** than the one hosting the
stack, point it at that host explicitly:

```bash
./benchmark/calc_stream_density.sh -u https://<HOST_IP> ...
```

### `throughput min: 0` or `FAILED` while waiting for RUNNING

Possible causes:

- **A stream went to ERROR** — the message printed after `FAILED` is the pipeline server's
  error text. Typical reasons are too many streams for the memory or device (for example
  several captioning streams on a small machine) or a stale pipeline server state:

  ```bash
  docker restart uav-va-dlstreamer-pipeline-server
  ```

- **Video file missing inside the container:**

  ```bash
  docker exec uav-va-dlstreamer-pipeline-server ls \
    /home/pipeline-server/resources/videos/
  ```

### `metrics-manager not reachable`

`calc_stream_density.sh` reaches `metrics-manager` through nginx on the same address as the
pipeline server (`https://<HOST_IP>`). If it is not reachable the benchmark continues with
FPS-only results. Make sure the full stack is up:

```bash
make pymav-up
docker ps | grep uav-va-metrics-manager
```

To use another address: `./benchmark/calc_stream_density.sh --metrics-url https://<HOST_IP> ...`

### `Pipeline '<name>' is not registered in the pipeline server`

The pipeline derived from `-d`/`-c`/`-cd` (or given with `-p`) is not in the server's
config. List the registered pipelines:

```bash
./benchmark/calc_stream_density.sh -l
```

### GPU or NPU shows `N/A` in the summary table

The system does not have an accessible Intel GPU or NPU. Verify hardware availability:

```bash
docker exec uav-va-dlstreamer-pipeline-server python3 -c \
  "from openvino.runtime import Core; print(Core().available_devices)"
```

- GPU requires `/dev/dri/renderD128` accessible inside the container (Intel iGPU or dGPU).
- NPU requires `/dev/accel/accel0` (Intel NPU, Meteor Lake / Lunar Lake / Panther Lake).

### Power reads all zeros or `N/A`

RAPL counters may not be accessible in the container on this hardware. The `metrics-manager` must have access to `/sys/class/powercap/` or Intel qmassa sensors. Check `metrics-manager` logs:

```bash
docker logs uav-va-metrics-manager 2>&1 | grep -iE "power|rapl|error"
```

## QGroundControl

### QGroundControl stable release fails to connect

See [QGroundControl](./qgroundcontrol.md) for the primary installation instructions (Stable v5.1).

**Symptom:** QGroundControl (Stable v5.1) fails to connect to the vehicle, or
the RTSP video stream does not render, even though the RTSP URL and pipeline
configuration are correct.

**Resolution:** Install the QGroundControl **latest daily build** as a fallback —
it contains newer fixes not yet in the stable release:

- [Download and Install QGroundControl — Latest daily build (Ubuntu)](https://docs.qgroundcontrol.com/master/en/qgc-user-guide/getting_started/download_and_install.html#ubuntu)

> [!NOTE]
> The daily build is pre-release software and may be less stable
> overall. Only use it if the Stable v5.1 release does not work for your setup.

### RTSP video stream does not play on a remote Windows machine (system proxy configured)

See [QGroundControl](./qgroundcontrol.md) for video stream configuration.

**Symptom:** QGroundControl connects to the vehicle (telemetry/MAVLink works
fine) but the RTSP video stream never renders, when QGroundControl is running
on a Windows machine remote from the UAV host and that Windows machine has a
system (or corporate) HTTP/HTTPS proxy configured.

**Cause:** With a system proxy set, QGroundControl's RTSP client can attempt
to route the `rtsp://<HOST_IP>:8555/uav-<camera>` connection through the
proxy as well, which fails to reach the UAV host's RTSP port (`8555`) on the
local/trusted network.

**Resolution:** Add the UAV host's IP (or your local network range) to the
`no_proxy` **system environment variable** on the Windows machine, then
restart QGroundControl so it picks up the change:

1. Open **System Properties** → **Environment Variables**.
2. Under **System variables** (or **User variables**), add or edit `no_proxy`
   (or `NO_PROXY`) to include the UAV host's IP, for example:

   ```text
   no_proxy=localhost,127.0.0.1,<HOST_IP>
   ```

3. Fully close and restart QGroundControl for the new environment variable to
   take effect.

### "Network Not Available" warnings

See [QGroundControl](./qgroundcontrol.md) for installation and video stream configuration.

**Symptom:** The following warnings appear in the QGroundControl logs:

```text
16.701 Warning: 1 "Network Not Available" - QtLocationPlugin.QGeoTiledMapReplyQGC - (unknown:0)
```

**Cause:** NetworkManager's connectivity check is failing, which causes it to
report the network as `limited` or `none` even when the host has a valid local connection.

**Resolution:**

1. Confirm the connectivity state:

   ```bash
   nmcli networking connectivity check   # expected: "limited" or "none"
   ```

2. Disable the NetworkManager connectivity check:

   ```bash
   sudo mkdir -p /etc/NetworkManager/conf.d
   sudo tee /etc/NetworkManager/conf.d/20-connectivity.conf <<'EOF'
   [connectivity]
   enabled=false
   EOF
   sudo systemctl restart NetworkManager
   ```

3. Verify the state is now reported as full:

   ```bash
   nmcli networking connectivity check   # expected: "full"
   ```

## VSS Live Search

See [Searching Live Video with VSS](./vss-live-search.md) for the feature
this section covers.

### Search popup never appears / routes 404

The plain `make up` stack is running, not `make up-vss`; stop it
(`make down`) and start the VSS overlay instead (`make up-vss`).

### A result shows "Unknown camera"

Its `camera`/`host` attribution resolves from the embedding's `sensor_id`
metadata, then the stream's registration-time `tags`, then a lookup against
VSS's current stream list; a UAV that has since disconnected (and aged out
of the stream list) can leave old results unresolved this way — this is
expected for stale results, not a bug.

### One stream dominates every search result

VSS keeps its top `VSS_MAX_RESULTS` hits across **all** streams together, so
a camera with far more indexed footage, or whose content simply matches the
query better, takes most of the slots. The ground station interleaves the
kept results by UAV/camera for display but cannot show a stream VSS
dropped. Raise `VSS_MAX_RESULTS`, or scope the query with the popup's
UAV/Camera filters; see
[Tune VSS Live Search](../how-to-customize/search-tuning/vss-search-tuning.md).
If a camera's footage genuinely never matches (check by filtering to that
camera alone), no setting will surface it.

### Every matching query returns the full result count

The relevance filters are off or too low for the embedding model; see
`VSS_SEARCH_MIN_PEAK_SCORE` / `VSS_SEARCH_RELATIVE_CUTOFF` in
[Tune VSS Live Search](../how-to-customize/search-tuning/vss-search-tuning.md).

### A result clip won't play in the browser

Some stored live-stream segments use a codec the browser can't decode; the
player falls back to VSS's server-decoded best-matching frame plus a
download link. If the segment is older than VSS's `VSS_LIVE_RETENTION_HOURS`, it
may no longer be available at all.

### New camera never becomes searchable

Confirm it reached `RUNNING` on the UAV host first (see
[Viewing and Interpreting Detection Logs](./view-detection-logs.md) or
`make cameras-status`); VSS only indexes RTSP streams it can see.

See the ground station's own
[README — VSS search](../../../../uav-ground-station/README.md#vss-search)
for the full route reference and container list.

## Multi-UAV Ground Station

See [Multi-UAV Operations in the Ground Station](./multi-uav-ground-station.md)
for the feature this section covers.

### Disconnecting one UAV seems to affect another's video/metrics

It shouldn't; each host's relay path, metrics stream, and caption buffer are
keyed by host. If this happens, check for a camera-name collision (see
[Multi-UAV Operations — Avoid camera-name collisions](./multi-uav-ground-station.md#4-avoid-camera-name-collisions))
that wasn't actually disambiguated — confirm the relay/VSS names include the
expected host segment.

### A disconnected host's Flight Summary still generates

Expected for a short window: `caption_log.py` keeps a host/camera's buffer
warm until it has gone `CAPTION_LOG_IDLE_TTL_SECONDS` without being observed
captioning, not the instant a tab closes.

### Reconnecting after a backend restart loses my tabs

Expected; a backend restart assigns a new `boot_id` and starts with no
sessions. This is a dashboard-session concept, not a UAV-side state change —
nothing was lost on the UAV hosts themselves.

See the ground station's own
[README — Multiple UAVs at once](../../../../uav-ground-station/README.md#multiple-uavs-at-once)
for implementation detail.
