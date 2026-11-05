<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Multi-UAV Operations in the Ground Station

`uav-ground-station` can connect to more than one `uav-vision-analytics`
host at once from the same browser session — useful when operating a small
fleet, or comparing two configurations side by side. This tutorial covers
connecting/disconnecting additional UAVs, what keeps running in the
background, and how the per-host **Flight Summary** report works. This is a
`uav-ground-station` feature; no change is needed on the
`uav-vision-analytics` side beyond each host running its own stack normally.

## Prerequisites

- Two or more `uav-vision-analytics` hosts reachable from the ground-station
  machine, each with at least one RTSP pipeline running (see
  [Get Started](../get-started/get-started-standalone.md)).
- `uav-ground-station` running (`make up` or `make up-vss` — see
  [Searching Live Video with VSS](./vss-live-search.md) if you
  also want cross-UAV search).

## Steps

### 1. Connect the first UAV

Enter its IP in the dashboard's connect field and press **Connect**. Camera
cards, metrics, and (if captioning is on) scene captions populate for that
host.

### 2. Connect additional UAVs

Enter a second host's IP and press **Connect** again. A tab bar appears
above the connect field once a second host is connected; switching tabs
swaps which host's camera cards and metrics are displayed. The dashboard
keeps the full list of connected hosts server-side (`GET/POST/DELETE
/api/sessions`), so reloading the page restores all open tabs — only a
backend restart (new `boot_id`) resets the list to none connected.

### 3. Understand what keeps running while a tab is inactive

Switching tabs does **not** stop anything in the background: a host keeps
streaming, captioning, and (if the VSS overlay is enabled) being indexed for
search for as long as its tab stays open, even while another tab is active.
Only closing a tab's **×** (disconnect) stops that host's VSS live-sync
indexing.

### 4. Avoid camera-name collisions

Camera names are operator-chosen per host
(`configs/cameras-pymavlink.yaml` / `configs/cameras-uavsdk.yaml` — see
[Configure the Cameras](../how-to-customize/camera-management/configure-cameras.md)), so two UAVs can legitimately both
have a camera named `front_camera`. The ground station disambiguates this
automatically:

- The mediamtx relay path is `relay-<host>-<camera>`.
- VSS stream registrations (if the search overlay is enabled) use
  `uav-<host>--<camera>`.

You don't need to rename cameras to avoid collisions across a fleet — just
be aware result/relay names include the host for this reason.

### 5. Generate a Flight Summary for a specific UAV

**Flight Summary** (top bar) defaults to the currently active tab's host, but
accepts any connected (or previously connected, while its caption buffer is
still warm) host explicitly via `POST /api/summary {"host": ...}`. Pick one
or more cameras that are currently captioning on that host and a time
window, then **Generate** — the report is built entirely from that host's
buffered MQTT captions (`caption_log.py`), independent of any other
connected UAV's data.

## Verify

```bash
curl -s http://${VIZ_HOST_IP:-127.0.0.1}:${VIZ_PORT:-8080}/api/sessions
```

Confirms every host you expect to be connected is listed. For a given host,

```bash
curl -s http://${VIZ_HOST_IP:-127.0.0.1}:${VIZ_PORT:-8080}/api/pipeline-info?host=<uav-ip>
```

confirms its cameras/pipelines are visible to the dashboard.

## Troubleshooting

See [Troubleshooting — Multi-UAV Ground Station](./troubleshooting.md#multi-uav-ground-station)
for common issues (one UAV's disconnect appearing to affect another,
Flight Summary still generating after disconnect, lost tabs after a backend
restart). For the UAV/Camera search filter, see
[Searching Live Video with VSS — Scope results to one UAV or camera](./vss-live-search.md#4-scope-results-to-one-uav-or-camera-multi-uav-setups).

See also the ground station's own
[README — Multiple UAVs at once](../../../../uav-ground-station/README.md#multiple-uavs-at-once)
for implementation detail.
