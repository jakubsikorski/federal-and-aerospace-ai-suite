<!-- SPDX-FileCopyrightText: (C) 2026 Intel Corporation -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Telemetry Reference — UAV Vision Analytics

## Overview

Telemetry data (GPS position, altitude, ground speed, heading, satellite count)
is overlaid directly on every video frame using a `gvapython` element in the
DL Streamer pipeline. The overlay class runs a background thread that receives
MAVLink messages and stores the latest telemetry values, which are then rendered
as ROI labels onto each frame via `gvawatermark`.

Two overlay implementations exist — one per deployment mode:

| File | Mode | Telemetry source |
|------|------|-----------------|
| `gvapython/telemetry-overlay-pymavlink.py` | pymavlink | MAVLink UDP :14541 (via mavlink-router) |
| `gvapython/telemetry-overlay-uavsdk.py` | UAVSDK | MQTT `uav/{id}/telemetry/status` |

---

## pymavlink Telemetry Overlay

### How it works

`MavlinkReceiver` (a daemon thread) connects to `udp:0.0.0.0:14541` and parses
three MAVLink message types:

| Message | Fields extracted |
|---------|----------------|
| `GLOBAL_POSITION_INT` | `relative_alt` (÷1000 → m), `hdg` (÷100 → °) |
| `VFR_HUD` | `groundspeed` (m/s) |
| `GPS_RAW_INT` | `lat`, `lon` (÷1e7 → °), `alt`, `fix_type`, `satellites_visible` |

`DrawDynamicText.process_frame()` is called per frame by `gvapython`. It reads
the latest values under a threading lock and adds one ROI per text line using
`frame.add_region(x, y, 1, 1, label)`. `gvawatermark` renders these ROIs as
text in the upper-left corner of the frame.

### Overlay fields rendered (per frame)

```
Name  : {{OVERLAY_NAME}}
Frame : {frame_number}
ALT   : {altitude:.1f} m
SPD   : {speed:.1f} m/s
HDG   : {heading:.1f}
LAT   : {latitude:.7f}
LON   : {longitude:.7f}
SATS  : {satellites}
```

### GStreamer pipeline element

```
gvapython module=/home/pipeline-server/gvapython/telemetry-overlay-pymavlink.py
           class=DrawDynamicText
           function=process_frame
           arg=["{{OVERLAY_NAME}}"]
```

The `arg` list is passed to `DrawDynamicText.__init__` as the `name` parameter.

### MAVLink routing (pymavlink mode)

```
PX4 SITL → mavlink-router (server :14550) → broadcast UDP :14541 → pymavlink in DL Streamer Pipeline Server
```

`mavlink-router/main.conf`:
```ini
[General]
TcpServerPort=5760

[UdpEndpoint px4]
Mode = Server
Address = 0.0.0.0
Port = 14550

[UdpEndpoint gcs]
Mode = Normal
Address = 0.0.0.0
Port = 14541
```

### Volume mount in docker-compose

```yaml
volumes:
  - "./gvapython/telemetry-overlay-pymavlink.py:/home/pipeline-server/gvapython/telemetry-overlay-pymavlink.py"
  - "./scripts/mavlink_pipeline_manager.py:/home/pipeline-server/scripts/pipeline_manager.py"
```

---

## UAVSDK Telemetry Overlay

The UAVSDK overlay reads telemetry from MQTT rather than MAVLink directly.
It subscribes to `uav/{id}/telemetry` topics published by the `uav-mission-compute-sdk`
companion bridge.

### Volume mount in docker-compose

```yaml
volumes:
  - "./gvapython/telemetry-overlay-uavsdk.py:/home/pipeline-server/gvapython/telemetry-overlay-uavsdk.py"
  - "./scripts/mavlink_pipeline_manager.py:/home/pipeline-server/scripts/pipeline_manager.py"
```

---

## Pipeline Manager Scripts

### mavlink_pipeline_manager.py (pymavlink mode)

Monitors the MAVLink HEARTBEAT `base_mode` flag for `MAV_MODE_FLAG_SAFETY_ARMED`.
Only RTSP output is supported (there is no `--sink` option).

**Cameras:** the pipelines are not hard-coded. `camera_config.py` (next to the manager) loads and validates
`configs/cameras-pymavlink.yaml` (mounted at `/home/pipeline-server/configs/cameras-pymavlink.yaml`) and returns one plan per enabled camera: the
pipeline (`uav_object_detection_<det>` or `uav_object_detection_<det>_<vlm>`), its start request and the RTSP path
`uav-<camera>`. The manager re-reads the file at every arming and keeps the last good configuration when the file is broken.

**Key constants:**
```python
CONNECTION_STRING   = "udpin:0.0.0.0:14541"
PIPELINE_SERVER_URL = "http://localhost:8081"          # PIPELINE_SERVER_URL overrides it
```

**Invocation inside container:**
```bash
python3 /home/pipeline-server/scripts/pipeline_manager.py              # wait for ARM, start the cameras
python3 /home/pipeline-server/scripts/pipeline_manager.py --start-now  # start them now, no vehicle
python3 /home/pipeline-server/scripts/pipeline_manager.py --status     # file versus running pipelines
```

**Makefile target:**
```bash
make start-rtsp      # docker exec -it uav-va-dlstreamer-pipeline-server bash -c "python3 ..."
```

### mavlink_pipeline_manager.py with ARM_SOURCE=mqtt (uavsdk mode)

The same manager as the pymavlink stack; `docker-compose-uavsdk.yml` sets `ARM_SOURCE=mqtt` and
`CAMERAS_CONFIG=/home/pipeline-server/configs/cameras-uavsdk.yaml`. It subscribes to the SDK's MQTT broker
(`MQTT_BROKER_HOST:MQTT_BROKER_PORT`, default `host.docker.internal:1883`) on `uav/<UAV_ID>/telemetry/status` and parses the
`armed` boolean of the JSON payload.

**On ARMED:** re-reads the cameras file and, for each camera whose source is an RTSP stream, probes it with `ffprobe`
(3 tries, 2 s apart) before starting its pipeline; unavailable cameras are skipped. The source URL
`rtsp://host.docker.internal:8554/{uav_id}/<camera>` takes `{uav_id}` from `UAV_ID` (default `uav-1`).

**On DISARMED:** DELETEs all running pipeline instances.

**Only RTSP sink is supported** in UAVSDK mode.

---

## MQTT Topics (pymavlink mode)

DL Streamer Pipeline Server publishes detection metadata to MQTT when configured with:
```yaml
environment:
  - MQTT_HOST=broker
  - MQTT_PORT=1883
  - APPEND_PIPELINE_NAME_TO_PUBLISHER_TOPIC=true
```

Detection topic pattern: `{pipeline_name}` (populated by DL Streamer Pipeline Server from pipeline name).

---

## Environment Variables for Telemetry

| Variable | Value | Purpose |
|----------|-------|---------|
| `ENABLE_RTSP` | `true` | Enable DL Streamer Pipeline Server RTSP server |
| `RTSP_PORT` | `8555` | RTSP output port |
| `MQTT_HOST` | `broker` | Mosquitto broker hostname |
| `MQTT_PORT` | `1883` | Mosquitto broker port |
| `ZE_ENABLE_ALT_DRIVERS` | `libze_intel_npu.so` | Required for NPU inference |
