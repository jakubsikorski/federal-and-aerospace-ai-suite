<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Troubleshoot UAV Vision Analytics

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
    apt install python3-venv
Failing command: .../resources/venv/bin/python3
make: *** [Makefile:28: model] Error 1
```

**Resolution:** Install the `python3-venv` package and re-run:

```bash
sudo apt install python3-venv
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

### `make model` / `make vlm-model` fail behind a corporate proxy

**Symptom:** `make model` hangs or fails downloading `yolo11s.pt` from
Ultralytics' GitHub releases (inside the `resources/venv` it creates), or
`make vlm-model` fails fetching `download_models.sh` from
`raw.githubusercontent.com` with a TLS/certificate error or a timeout, even
though `docker pull` works fine.

**Cause:** Unlike the Docker-build-time `pip install pymavlink` issue above,
`make model` and `make vlm-model` run `yolo export`, `curl`, and (for a
custom VLM export) `optimum-cli`/`huggingface_hub` directly **on the host**,
in the calling shell — they need `http_proxy`/`https_proxy`/`no_proxy` set in
*that* shell (not just `/etc/environment`), and if the proxy does TLS
inspection, its root CA must be trusted by the host's CA store for
`curl`/`pip` to accept it.

**Resolution:**

```bash
export http_proxy=http://proxy.example.com:911
export https_proxy=http://proxy.example.com:911
export no_proxy=localhost,127.0.0.0/8
make model
make vlm-model
```

If the proxy does TLS inspection, install its root CA into the host's trust
store (e.g., `sudo update-ca-certificates` on Ubuntu, after copying the CA
into `/usr/local/share/ca-certificates/`) before retrying. Never use `curl
-k`/`--insecure` for these downloads — that flag is only safe against this
application's own self-signed nginx certificate (see
[REST API calls fail or get a proxy error](#rest-api-calls-to-httpshost_ip-fail-or-get-a-proxy-error)
below), never against an external download host.

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
ffplay -rtsp_transport tcp rtsp://<HOST_IP>:8555/uav-front-camera
Command 'ffplay' not found, but can be installed with:
sudo apt install ffmpeg
```

**Resolution:** `ffplay` is part of the `ffmpeg` package:

```bash
sudo apt install ffmpeg

# Then verify RTSP stream
ffplay -rtsp_transport tcp rtsp://<HOST_IP>:8555/uav-front-camera
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

**Symptom:** The `px4` service fails to start or behaves unexpectedly with the pinned PX4 SITL tag.

**Resolution:** Pin another tag from [Docker Hub](https://hub.docker.com/r/px4io/px4-sitl/tags) (for example the release `v1.18.0-rc1`) in `docker-compose-pymavlink.yml`:

```diff
-image: px4io/px4-sitl:v1.18.0-beta1-660-g61907962c9
+image: px4io/px4-sitl:v1.18.0-rc1
```

### `make pymav-up` / `make uavsdk-up` fails — `address already in use` (port `1883`, `14551`, etc.)

**Symptom:**

```text
Error response from daemon: failed to set up container networking: driver failed
programming external connectivity on endpoint mqtt-broker (...): failed to bind
host port 0.0.0.0:1883/tcp: address already in use
make: *** [Makefile:91: up-sim-camera] Error 1
```

and killing the process holding the port (`sudo lsof -i :1883`, then `sudo kill
-9 <PID>`) only has it come right back under a new PID a moment later.

**Cause:** This is **not** a container cleanup problem — a prior **standalone
deployment** installed a host-level service (for example a `mosquitto`
systemd unit) that is still enabled and bound to the same port the compose
stack wants to publish (`1883` for MQTT, `14551` for mavlink-router, etc.).
A plain `kill`/`kill -9` only kills the current process; `systemd`'s restart
policy immediately respawns it, which looks like "cleanup isn't happening"\
but is actually the host service winning the race against Docker every time.

**Resolution:**

1. Identify the service, not just the PID:

   ```bash
   sudo lsof -i :1883
   systemctl status mosquitto --no-pager   # substitute the actual service name
   systemctl is-enabled mosquitto
   ```

2. If it's a leftover host service from a prior standalone install (not a
   `docker-proxy` entry for one of this stack's own containers), disable and
   mask it so it can't be auto-started again, instead of just killing the
   process:

   ```bash
   sudo systemctl disable --now mosquitto
   sudo systemctl mask mosquitto
   ```

3. Confirm the port is free, then re-run the `make` target:

   ```bash
   sudo lsof -i :1883   # expect no output
   make up-sim-camera
   ```

> [!TIP]
> Before switching a machine from standalone mode to SDK/containerized mode
> (or back), explicitly uninstall/disable any host-level services the
> standalone deployment installed. Otherwise this same class of issue can
> recur for *any* port the two deployment modes both use — not just MQTT, but
> MediaMTX/WebRTC (`8889`/`8189`), the Viz UI (`8080`), or mavlink-router
> (`14551`, see [QGroundControl shows "not connected"](#qgroundcontrol-shows-not-connected--failed-to-bind-udp-socket-to-port-14551)
> below) — whichever one happens to collide on the machine.

## Pipelines

### REST API calls to `https://<HOST_IP>/...` fail or get a proxy error

**Symptom:** `curl -k https://<HOST_IP>/pipelines/status` (or any other REST
call from [Manage Pipelines](./how-to-customize/pipeline-management/manage-pipelines.md#rest-api))
hangs, returns `403`/`502` from an unrelated proxy server, or
`Connection refused`, even though the stack is healthy and `ffplay` can read
the RTSP stream fine.

**Cause:** A shell-level `http_proxy`/`https_proxy` (often set system-wide by
a corporate device policy) routes LAN/host-local traffic through a proxy that
cannot reach `<HOST_IP>` or terminates TLS itself and rejects nginx's
self-signed certificate.

**Resolution:** Exclude local nginx calls from the proxy, the same way
`benchmark/calc_stream_density.sh` does for every REST call it makes:

```bash
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
export no_proxy="*" NO_PROXY="*"
curl -k -s https://<HOST_IP>/pipelines/status
```

Resolve `HOST_IP` from `.env` instead of assuming `localhost` if the request
is issued from a different host/VM than the one running the stack:

```bash
HOST_IP=$(grep -E '^HOST_IP=' .env | cut -d= -f2)
curl -k -s --noproxy '*' "https://${HOST_IP}/pipelines/status"
```

### `/pipelines/status` keeps old pipeline entries after they were stopped

**Symptom:** A benchmark or monitoring script that counts entries in
`GET /pipelines/status` reports more pipelines than are actually running, or
keeps counting a pipeline that was `DELETE`d or disarmed minutes ago.

**Cause:** `/pipelines/status` retains **every** instance's history
(`COMPLETED`, `ERROR`, `ABORTED`, and any instance stopped mid-run) until the
`dlstreamer-pipeline-server` container restarts — it does not shrink back
down after a `DELETE` or a camera disarms.

**Resolution:** Always filter on `state`, never trust the raw array length.
`benchmark/calc_stream_density.sh` does this consistently:

- Counting/stopping active instances: `state in ("RUNNING", "QUEUED", "STARTING")`
- Averaging FPS/throughput: `state == "RUNNING"` only
- Counting failures: `state in ("ERROR", "ABORTED", "COMPLETED")`

```bash
curl -k -s https://<HOST_IP>/pipelines/status | \
  jq '[.[] | select(.state=="RUNNING" or .state=="QUEUED" or .state=="STARTING")] | length'
```

See [Manage Pipelines — stop all running pipelines](./how-to-customize/pipeline-management/manage-pipelines.md#stop-all-running-pipelines).

### `make start-rtsp` silently does nothing from a script / CI / non-interactive shell

**Symptom:**

```text
cannot attach stdin to a TTY-enabled container because stdin is not a terminal
```

...but `make` itself exits `0` and the "manager started" step of an automated
script reports success even though no camera ever starts.

**Cause:** `make start-rtsp`'s recipe runs `docker exec -it ...`, which needs
a real terminal. Its Makefile line is prefixed with `-` (meaning "ignore this
recipe's exit status"), so `make` reports success regardless. Run from an
interactive terminal (a normal SSH session, a local shell) it works fine;
run from `nohup ... &`, a CI job, a cron job, or any other non-tty context,
`docker exec -it` fails immediately and nothing is listening for the
vehicle's armed state.

**Resolution:** For interactive use, run `make start-rtsp` directly in a
terminal you keep open (or a `screen`/`tmux` session). For scripted/CI
validation where there is no vehicle to arm, skip the manager's
wait-for-MAVLink loop entirely and start every enabled camera once, immediately, with:

```bash
docker exec -d <dlstreamer-pipeline-server container name> \
  python3 /home/pipeline-server/scripts/pipeline_manager.py --start-now
```

(`docker exec -d` detaches immediately and needs no tty.) Confirm with
`make cameras-status`, do not rely on the launch command's own exit code.

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
cd federal-and-aerospace-ai-suite/uav-mission-compute-sdk
sed -i 's|^HOST_IP=.*|HOST_IP=0.0.0.0|' .env
make down && make up-sim-camera
```

See [Deploy in UAV Mission Compute SDK Mode — Step 1](./deploy/deploy-uavsdk.md#1-start-the-uav-mission-compute-sdk).

### Pipelines skip their cameras — the RTSP source is not available (UAV Mission Compute SDK mode)

**Symptom:**

```text
[pipeline] Skipping camera 'nadir_camera': its RTSP source rtsp://host.docker.internal:8554/uav-1/nadir is not available.
```

**Cause:** The SDK publishes the camera streams only while the vehicle is armed
(`camera-bridge` logs `UAV ARMED — cameras active` / `UAV DISARMED — cameras
paused`), and the pipeline manager checks the sources at the moment it sees the
armed state. Start the manager before arming, or arm and take off first and then
run `make start-rtsp`; landing disarms the vehicle and removes the streams.

If the vehicle is armed and the streams are still missing, the SDK's `mediamtx`
container may have lost its ports because another service, usually the ground
station, holds one of them (`8554`, `8888`, `8889`). `camera-bridge` then cannot
resolve `mediamtx` and restarts its publisher in a loop. Check:

```bash
docker ps --format '{{.Names}}\t{{.Ports}}' | grep -E 'mediamtx'   # the SDK one must list 8554, 8888 and 8889
docker exec camera-bridge getent hosts mediamtx                     # must print an address
```

**Resolution:** Run `make up-vss` (or `make up`) again in `uav-ground-station`: it
moves the ground station to free ports and avoids the SDK's `8080`, `8554`, `8888`
and `8889`. If the ground station was started before the SDK was installed, set
other ports in `uav-ground-station/.env` (for example `VIZ_PORT=8081`,
`MEDIAMTX_WEBRTC_PORT=8890`) and run its `make up-vss` again. Then recreate the
SDK's MediaMTX:

```bash
cd uav-mission-compute-sdk
docker compose up -d --no-deps --force-recreate mediamtx
```

### No telemetry overlay on stream (all zeros)

**Pymavlink mode:** Confirm `mavlink-router` is running and forwarding MAVLink from PX4:

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
[Manage Pipelines — List the available pipelines](./how-to-customize/pipeline-management/manage-pipelines.md#list-the-available-pipelines).

### REST API returns `400` on POST

A `parameters` value does not match its schema `pattern` in the config file,
for example a `detection_model` outside `/home/pipeline-server/resources/models/`
(use the container path, not a host path), a `video` name with a folder in
it, or a `captioner_prompt` with characters other than letters, digits,
spaces, and `, . ? ' -`. The schemas are listed in
[Modify Pipelines — The parameters schema](./how-to-customize/pipeline-management/modify-pipelines.md#the-parameters-schema).

### `model-instance-id ... errored out on a prior run`

A previous start with the same `detection_instance_id` failed (wrong device,
unsupported model, or device not mounted) and the pipeline server keeps that
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
see [Modify Pipelines — Add a pipeline](./how-to-customize/pipeline-management/modify-pipelines.md#add-a-pipeline).

### Rapid arm/disarm flapping leaves a stream down (SITL)

A re-arm loop whose interval is close to PX4's idle-disarm timeout flaps the
vehicle between armed and disarmed, repeatedly starting and stopping the
pipelines. Use a clearly shorter re-arm interval; if a stream stays down,
stop and start that pipeline by hand
([Manage Pipelines](./how-to-customize/pipeline-management/manage-pipelines.md)).
This pattern is for SITL testing only.

### Both stacks start but only one streams

Pymavlink and UAV Mission Compute SDK mode both bind `80`/`443`/`8555`;
only one `nginx` can hold them. Run `make pymav-down` or `make uavsdk-down`
for the mode not in use.

### Telemetry overlay shows zeros right after switching modes

The two modes use different `gvapython` overlay scripts; values appear once
the new mode's receiver gets its first MAVLink/MQTT message (a few seconds).
If they stay at zero, see
[No telemetry overlay on stream](#no-telemetry-overlay-on-stream-all-zeros).

### Detection log (JSONL) is missing or empty

Only a pipeline started by hand with a `destination.metadata` block writes
one; the camera-file manager does not add it. See [Read and Filter Detection Logs](./how-to-guides/view-detection-logs.md).

## Cameras & Video

### `make check-cameras` reports `video not found`

The file is not in `resources/videos`, or `video:` includes a folder. Only a
file name is accepted.

### Pipeline goes to `ERROR` with a new video file

The pipelines expect H.264 in an `.avi`/`.mp4`/`.mkv`/`.ts` container.
Convert the file with `scripts/convert_video.sh` rather than copying the
original — see [Use Your Own Videos](./how-to-customize/model-and-video-management/use-your-own-video.md).

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
use the stable `/dev/v4l/by-id/` path and the camera's color node (YUYV or
UYVY format). See
[Configure the Cameras — Live cameras](./how-to-customize/camera-management/configure-cameras.md#live-cameras-realsense-usb-webcams).

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
[Manage the Detection Model](./how-to-customize/model-and-video-management/model.md).

### No detections with a new detection model

The `threshold=0.4` in the pipeline string may be too high for the model's
confidence distribution; lower it as shown in
[Modify Pipelines — Change an element's property](./how-to-customize/pipeline-management/modify-pipelines.md#change-an-elements-property).

### Captioning pipeline goes to `ERROR` at start

The model folder is not a complete OpenVINO™ IR export of the VLM, or the
architecture is not supported by the `openvino_genai` version in the
pipeline-server image. `docker logs uav-va-dlstreamer-pipeline-server` shows the
loader error; revert `captioning.model` and re-arm to recover. The required
file set is listed in
[Change the Captioning Model — Required format](./how-to-customize/model-and-video-management/vlm-model.md#required-format).

### `make vlm-model` fails — `Service failed to start within 180s`

**Symptom:**

```text
INFO: Starting container: model-download-ephemeral (pulling image if needed)...
INFO: Service URL: http://localhost:32768
INFO: Waiting for service to become ready (timeout: 180s)...
ERROR: Service failed to start within 180s.
ERROR: Container logs:
 ...
 Using Python 3.12.15 environment at: .venv-openvino
   Updating https://github.com/huggingface/optimum-intel.git (d4dd21a...)
INFO: Stopping container model-download-ephemeral...
ERROR LOG: .../.model_download_logs/model_download_<timestamp>.log
make: *** [Makefile:199: vlm-model] Error 1
```

**Cause:** `make vlm-model` runs `.cache/download_models.sh`, which on every
invocation fetches a separate helper script, `get_model.sh`, fresh from
`edge-ai-libraries` (`main` branch) into a temp file and runs it immediately
— nothing from that helper is cached or vendored locally. That helper starts
the `model-download-ephemeral` container and polls `/health` for up to a
**hardcoded, non-configurable 180 seconds** before giving up. The container
installs a Python venv and builds `optimum-intel` from its GitHub source on
every single run (there is no persisted pip/venv cache across runs — the
container is started with `docker run --rm` and no cache volume), so on a
slow network or corporate proxy/mirror this routinely takes longer than
180s, especially on the first run on a given machine.

**Resolution:** `download_models.sh` already supports pointing at a
different helper script via `MODEL_DOWNLOAD_EPHEMERAL_SCRIPT_URL` (it falls
back to the upstream URL only if this is unset) — including a local file,
since `curl`/this variable also accepts a `file://` URL. Pre-fetch, patch,
and point at it once:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/open-edge-platform/edge-ai-libraries/main/microservices/model-download/scripts/get_model.sh \
  -o /tmp/get_model_patched.sh

# Raise the health-check timeout from 180s to, e.g., 900s
sed -i 's/^HEALTH_TIMEOUT=180/HEALTH_TIMEOUT=900/' /tmp/get_model_patched.sh

MODEL_DOWNLOAD_EPHEMERAL_SCRIPT_URL="file:///tmp/get_model_patched.sh" make vlm-model
```

Re-run with the same `MODEL_DOWNLOAD_EPHEMERAL_SCRIPT_URL` export set for
any later `make vlm-model` / `make model` call on that machine until the
upstream helper script changes.

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

If you are running the benchmark from a **different machine** than the one hosting the
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

See [QGroundControl](./how-to-guides/qgroundcontrol.md) for the primary installation instructions (Stable v5.1).

**Symptom:** QGroundControl (Stable v5.1) fails to connect to the vehicle, or
the RTSP video stream does not render, even though the RTSP URL and pipeline
configuration are correct.

**Resolution:** Install the QGroundControl **latest daily build** as a fallback —
it contains newer fixes not yet in the stable release:

- [Download and Install QGroundControl — Latest daily build (Ubuntu)](https://docs.qgroundcontrol.com/master/en/qgc-user-guide/getting_started/download_and_install.html#ubuntu)

> [!CAUTION]
> The daily build is pre-release software and may be less stable
> overall. Only use it if the Stable v5.1 release does not work for your setup.

### QGroundControl shows "not connected" — `Failed to bind UDP socket to port 14551`

See [QGroundControl](./how-to-guides/qgroundcontrol.md) for the primary installation instructions.

**Symptom:** QGroundControl never shows a connected vehicle, and its logs show:

```text
Failed to bind UDP socket to port 14551 - Comms.UDPLink - (UDPWorker::connectLink:353)
    40.200 Warning: Communication error: "The bound address is already in use" - Comms.UDPLink - (UDPLink::_onErrorOccurred:582)
```

**Cause:** Port `14551` is already in use by something else on the host, so
QGroundControl's own UDP listener fails to bind to it. This has two common
root causes, and it's worth checking both:

1. **`14551` is the mavlink-router container's published port**, not a port
   QGroundControl should be trying to listen on itself. If QGroundControl's
   comm link is configured with **Listening Port = 14551**, it is colliding
   with the `uav-va-mavlink-router` container, which already binds host
   `14551` (check with `docker ps --format '{{.Names}}\t{{.Ports}}' | grep
   mavlink-router`). QGroundControl's link should instead be a UDP client
   that *sends to* `<HOST_IP>:14551` (mavlink-router's endpoint) while
   listening on a different local port of its own (for example `14552`).
2. **A leftover `mavlink-routerd` process from a previous deployemnt might
   still running on the host** and never got cleaned up — the same class of
   stale-process issue as [`address already in use` on `make pymav-up`/`make uavsdk-up`](#make-pymav-up--make-uavsdk-up-fails--address-already-in-use-port-1883-14551-etc).
   Check for it with:

   ```bash
   sudo ss -ulpn | grep 14551
   # or
   sudo lsof -i :14551
   ```

   If the process is an old `mavlink-routerd` (not a `docker-proxy` entry for
   the container), it's a leftover from a prior standalone install and safe
   to stop:

   ```bash
   sudo kill <PID>
   ```

**Resolution:**

1. Stop QGroundControl.
2. Edit its comm link so the **Listening Port** no longer collides with
   `14551` (used by mavlink-router) — for example set it to `14552` in
   `~/.config/QGroundControl/QGroundControl.ini`:

   ```bash
   sed -i 's/^Link0\\port=14551/Link0\\port=14552/' \
     ~/.config/QGroundControl/QGroundControl.ini
   ```

   (Equivalently, in the UI: **Application Settings → Comm Links → \<your
   link\> → Edit**, change **Listening Port** to `14552`, leaving the
   **Target Host**/`14551` pointed at the UAV host unchanged.)
3. Clean up any leftover standalone-mode `mavlink-routerd` process still
   holding `14551` on the host, per the **Cause** section above.
4. Restart QGroundControl. It should now bind its own listening socket
   without conflict and begin receiving the MAVLink heartbeat/telemetry
   stream from the containerized mavlink-router.

### RTSP video stream does not play on a remote Windows machine (system proxy configured)

See [QGroundControl](./how-to-guides/qgroundcontrol.md) for video stream configuration.

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

See [QGroundControl](./how-to-guides/qgroundcontrol.md) for installation and video stream configuration.

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

See [Search Live Video with Natural Language](./how-to-guides/vss-live-search.md) for the feature
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
query better, can take most of the slots. The ground station therefore also
asks VSS for each UAV's and each camera's own best matches, merges them and
interleaves the results by UAV/camera. The popup's UAV/Camera filters narrow
that merged set without searching again; see
[Tune VSS Live Search](./how-to-customize/search-tuning/vss-search-tuning.md).
If a camera's footage genuinely never matches (check by filtering to that
camera alone), no setting will surface it.

### A search finds nothing in recent footage although the object is visible

VSS applies the UAV/camera and time filters only after it has fetched the 1000
frames most similar to the query across the whole index. When the index holds
a large amount of older footage (for example from a previous pymavlink session
or another scenario), the newest frames can fall outside those 1000 and the
search returns nothing for them. Run `make clean-vss-index` in
`uav-ground-station` and `make up-vss` to start with an empty index. Raising
the candidate count in VSS is not a remedy: 3000 candidates made a query take
about 30 s.

### A short query such as "person" returns few or no results

Single words score lower against CLIP than full descriptions. Describe the
scene ("a person walking on the road") or lower `VSS_SEARCH_MIN_PEAK_SCORE`
(see [Tune VSS Live Search](./how-to-customize/search-tuning/vss-search-tuning.md)).

### A result does not show the object that was searched for

VSS indexes whole frames and also crops of the objects its detector finds.
Image-text similarity alone cannot tell a car crop from a truck crop, and
cannot confirm a small object inside a whole frame. When the query names a
detectable object (for example person, car, truck, bus), the ground station
drops crops whose detector label is another object, and shows the label of a
matching crop as **Matched object**. Whole-frame hits cannot be checked this
way. To return only crops labelled as the searched object, set
`VSS_SEARCH_OBJECT_MATCH=strict` in `uav-ground-station/.env`; this suits
high-resolution footage, but returns nothing when the detector does not find the
object, for example on small or low-resolution frames.

### A newly indexed feed stays "starting" for a few seconds before "running"

Expected: VSS has to open the RTSP source and produce its first embeddings
before it reports `running`, and it briefly re-reports `starting` every time
it reopens a stream afterward (shown as `running` for `RESTART_GRACE_SECONDS`
once a stream has run at least once, so it does not flicker in the UI). While a
feed is starting, the ground station checks VSS every few seconds instead of
every `VSS_LIVE_SYNC_INTERVAL_SECONDS`.

Two settings decide how soon a new feed shows its first embeddings (about
6-13 s after connecting with the defaults, against 30-40 s before):

- A client can only start decoding at a keyframe, so VSS waits for the next
  one when it opens the stream. The cameras ask for a keyframe every
  `H264_GOP_SIZE` frames (`15`, in `scripts/camera_config.py`) through the
  `encode-gop-size` option of the RTSP destination. A pipeline server that does
  not know the option ignores it and uses the encoder's own interval of 90
  frames, which is 6-13 s at this frame rate and delays each camera's first
  embeddings by up to that long. A smaller value starts feeds sooner and raises
  the video bitrate.
- VSS embeds a live feed's frames in batches and sends on a partial batch once
  its oldest frame is `VSS_LIVE_BATCH_MAX_AGE_SECONDS` old (default `2` in
  `uav-ground-station/.env`; VSS's own default is `20`).

### Live-index panel shows feeds as `queued`

VSS indexes at most `VSS_LIVE_STREAM_MAX_CONCURRENT` live streams (default `8`,
one per UAV camera). With more cameras connected, the tab being viewed starts
first and the rest wait for a free slot. Raise the value in `.env` and run
`make up-vss` to index more at once (more CPU/GPU load). Feeds of cameras that
stopped, and of UAVs unreachable for a minute, are paused to free their slots.

### `Object detection failed` floods the `uav-gs-vss-dataprep` log

The detector model could not be written to the `vss-yolox-models` volume, which
earlier releases mounted root-owned. Run `make up-vss` to recreate the
container with the corrected mount; the model downloads on the next start.

### Every matching query returns the full result count

The relevance filters are off or too low for the embedding model; see
`VSS_SEARCH_MIN_PEAK_SCORE` / `VSS_SEARCH_RELATIVE_CUTOFF` in
[Tune VSS Live Search](./how-to-customize/search-tuning/vss-search-tuning.md).

### A result clip will not play in the browser

Some stored live-stream segments use a codec the browser cannot decode; the
player falls back to VSS's server-decoded best-matching frame plus a
download link. If the segment is older than VSS's `VSS_LIVE_RETENTION_HOURS`, it
may no longer be available at all.

### New camera never becomes searchable

Confirm it reached `RUNNING` on the UAV host first (see
[Read and Filter Detection Logs](./how-to-guides/view-detection-logs.md) or
`make cameras-status`); VSS only indexes RTSP streams it can see.

See the Ground Station's own
[Get Started — VSS search](../uav-ground-station/get-started.md#vss-search)
for the container list, and its
[Configuration — VSS search routes](../uav-ground-station/configuration.md#vss-search)
for the full route reference.

## Multi-UAV Ground Station

See [Monitor Multiple UAVs in the Ground Station](./how-to-guides/multi-uav-ground-station.md)
for the feature this section covers.

### Disconnecting one UAV seems to affect another's video/metrics

This should not happen; each host's relay path, metrics stream, and caption buffer are
keyed by host. If this happens, check for a camera-name collision (see
[Monitor Multiple UAVs — Avoid camera-name collisions](./how-to-guides/multi-uav-ground-station.md#4-avoid-camera-name-collisions))
that was not actually disambiguated — confirm the relay/VSS names include the
expected host segment.

### A disconnected host's Flight Summary still generates

This is expected for a short window: `caption_log.py` keeps a host/camera's buffer
warm until it has gone `CAPTION_LOG_IDLE_TTL_SECONDS` without being observed
captioning, not the instant a tab closes.

### Reconnecting after a backend restart loses my tabs

This is expected; a backend restart assigns a new `boot_id` and starts with no
sessions. This is a dashboard-session concept, not a UAV-side state change —
nothing was lost on the UAV hosts themselves.

See the Ground Station's own
[Get Started — Multiple UAVs at once](../uav-ground-station/get-started.md#multiple-uavs-at-once)
for the implementation details.
