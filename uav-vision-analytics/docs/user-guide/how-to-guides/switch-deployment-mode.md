<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Switching Between pymavlink and UAV Mission Compute SDK Deployment Modes

UAV Vision Analytics supports two independent deployment modes. This tutorial
explains the architecture difference, how to switch between them cleanly, and
the `HOST_IP` binding requirement that trips up first-time UAVSDK mode setup.

## The two modes

| | Standalone (pymavlink) | UAV Mission Compute SDK |
| --- | --- | --- |
| Compose file | `docker-compose-pymavlink.yml` | `docker-compose-uavsdk.yml` |
| `make` targets | `make pymav-up` / `make pymav-down` | `make uavsdk-up` / `make uavsdk-down` |
| Telemetry source | PX4 SITL → `mavlink-router` → `pymavlink` (UDP) | `uav-mission-compute-sdk` project → MQTT |
| Video source | Looped local file or RealSense camera | 3 RTSP camera streams from the SDK (nadir/forward/rear) |
| Self-contained? | Yes — PX4, router, broker all included | No — requires the separate `uav-mission-compute-sdk` project running first |
| Config file | `configs/config-pymavlink.json` | `configs/config-uavsdk.json` |

Standalone mode is self-contained because it starts its own PX4 SITL and
MAVLink routing. UAV Mission Compute SDK mode instead **attaches to** a
separately-running SDK project that owns the flight simulator, camera feeds,
and MQTT telemetry — `uav-vision-analytics` only runs the DLSPS inference
container in that mode.

## Prerequisites

- Only one mode's stack should be running at a time — they are independent
  Docker Compose projects but both publish the same host ports through
  `nginx` (`80`/`443`/`8555`/`1883`).
- For UAVSDK mode: the `uav-mission-compute-sdk` project cloned alongside
  this repo (see
  [Get Started — UAV Mission Compute SDK Mode](../get-started/get-started-uavsdk.md#1-start-the-uav-mission-compute-sdk)).

## Steps

### Switch from standalone to UAV Mission Compute SDK mode

```bash
# 1. Stop the standalone stack if running
make pymav-down

# 2. Start the SDK project's core infrastructure (PX4, MQTT, RTSP server)
cd ../uav-mission-compute-sdk
```

The SDK's `.env` defaults to `HOST_IP=127.0.0.1`, which binds MQTT, the
MediaMTX RTSP server, and every other published port to **loopback only**.
Because `uav-vision-analytics` runs in a separate Docker container/network,
it cannot reach loopback-bound ports on the host — so this bind address must
be widened *before* starting the SDK:

```bash
sed -i 's|^HOST_IP=.*|HOST_IP=0.0.0.0|' .env
make up-sim-camera
```

```bash
# 3. Start uav-vision-analytics in UAVSDK mode
cd ../uav-vision-analytics
make uavsdk-up
make start-rtsp
```

### Switch from UAV Mission Compute SDK mode back to standalone

```bash
make uavsdk-down
cd ../uav-mission-compute-sdk && make down
cd ../uav-vision-analytics
make pymav-up
make start-rtsp
```

## Verify

Before starting `uav-vision-analytics`, confirm the SDK is actually
reachable from outside its own network (this proves the `HOST_IP` fix took
effect, rather than only discovering the failure later):

```bash
docker network ls | grep uav-mission-compute-sdk
curl -sf http://<HOST_IP>:8080/action/arm -X POST   # any reachable SDK REST endpoint
```

A `Connection refused` here means `HOST_IP` is still loopback-bound — repeat
the `sed` step above and restart the SDK (`make down && make up-sim-camera`).

## Troubleshooting

`ConnectionRefusedError` / RTSP `404` in UAVSDK mode (the `HOST_IP` symptom
above), both stacks starting but only one streaming, and an overlay that
shows zeros right after switching are covered in
[Troubleshooting — Pipelines](./troubleshooting.md#pipelines).
