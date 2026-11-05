<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Searching Live Video with VSS

`uav-ground-station` can optionally index every running camera's live RTSP
stream into a
[Video Search and Summarization (VSS)](https://github.com/open-edge-platform/edge-ai-libraries/tree/main/sample-applications/video-search-and-summarization)
stack and let an operator search across them with a natural-language query
(e.g. "a red car on the road"). This tutorial covers enabling the overlay,
how a UAV's cameras get indexed, and how to scope a search to one UAV or
camera — this is a `uav-ground-station` feature; nothing changes on the
`uav-vision-analytics` side beyond already running RTSP pipelines (see
[Configure the Cameras](../how-to-customize/camera-management/configure-cameras.md)).

> [!NOTE]
> This covers **video** search only — VSS indexes and searches the raw RTSP
> frames, not the MQTT scene captions. Captioning is a separate feature
> (per-camera VLM captions and the Flight Summary report); see the
> [VLM Model guide](../how-to-customize/model-and-video-management/vlm-model.md) and
> [Multi-UAV Operations — Flight Summary](./multi-uav-ground-station.md#5-generate-a-flight-summary-for-a-specific-uav).

## Prerequisites

- `uav-vision-analytics` running with at least one RTSP pipeline up (any
  device/camera) — see [Get Started](../get-started/get-started-standalone.md)
  and [Configure the Cameras](../how-to-customize/camera-management/configure-cameras.md).
- `uav-ground-station` checked out (see its
  [Quick Start](../../../../uav-ground-station/README.md#quick-start)) and `make init` already run once (creates
  `.env`). Recommended to Run it on a different machine than the UAV Mission Compute SDK
  stack: both publish the mediamtx WebRTC port `8889` on `HOST_IP`.
- `VSS_MINIO_USER` / `VSS_MINIO_PASSWORD` / `VSS_POSTGRES_USER` /
  `VSS_POSTGRES_PASSWORD` set in `uav-ground-station/.env` (see
  `.env.example`) — required by `make up-vss`.

## Steps

### 1. Start the VSS overlay instead of the plain stack

```bash
cd uav-ground-station
make up-vss     # base uav-gs-backend + mediamtx relay + the 8 VSS containers (uav-gs-vss-*)
```

The first start downloads the CLIP embedding model and YOLOX (for object
crops) into Docker volumes — this can take a few minutes. With the default
`make up` (no `-vss`) the search UI stays hidden and `/api/search*` routes
return `404`.

### 2. Connect to the UAV and let indexing start automatically

Open the dashboard (`http://<VIZ_HOST_IP>:<VIZ_PORT>`), enter the UAV host's
IP, and press **Connect**. Every currently `RUNNING` pipeline's RTSP stream
(`uav-<camera>`) is registered with VSS as a live stream and indexed
continuously. A backend loop, every `VSS_LIVE_SYNC_INTERVAL_SECONDS` (default
`15`), adds pipelines that start later and pauses ones that stop —
**Disconnect** pauses all of that host's indexing. Indexed footage is kept
for `VSS_LIVE_RETENTION_HOURS`.

> [!NOTE]
> Starting/enabling a camera *after* connecting (e.g. via
> [Configure the Cameras](../how-to-customize/camera-management/configure-cameras.md) + `make start-rtsp`) is picked up on
> the next sync interval, not instantly — allow up to
> `VSS_LIVE_SYNC_INTERVAL_SECONDS` before it appears as searchable.

### 3. Run a search

Press **Search Videos** (top bar, side panel, or the `/` key), type a
natural-language prompt, and press Enter. Results are ranked with
thumbnails; use the arrow keys to move between them and `Esc` to close.

### 4. Scope results to one UAV or camera (multi-UAV setups)

The search popup's **UAV** and **Camera** dropdowns filter the already
fetched result set in-memory — switching UAVs, changing cameras, or
resetting the selection never re-queries VSS. This works because every
UAV's streams are registered under a host-qualified name
(`uav-<host>--<camera>`), so two UAVs reusing the same camera name (e.g. both
configure a `front_camera`) never collide in the index.

> [!TIP]
> Result count, relevance filtering and indexing rate are all tunable in
> `uav-ground-station/.env`; see
> [Tune VSS Live Search](../how-to-customize/search-tuning/vss-search-tuning.md).

## Verify

```bash
curl -s http://${VIZ_HOST_IP:-127.0.0.1}:${VIZ_PORT:-8080}/api/search/status
curl -s -X POST http://${VIZ_HOST_IP:-127.0.0.1}:${VIZ_PORT:-8080}/api/search \
  -H 'Content-Type: application/json' -d '{"query": "a car on the road"}'
```

`GET /api/search/sources` lists the UAV hosts and cameras currently
live-indexed — if a camera you expect is missing, it hasn't completed a sync
cycle yet, or its pipeline isn't `RUNNING`.

## Troubleshooting

See [Troubleshooting — VSS Live Search](./troubleshooting.md#vss-live-search)
for common issues (search popup 404, "Unknown camera" results, one stream
dominating results, result clips that won't play, a new camera that never
becomes searchable).

See also the ground station's own
[README — VSS search](../../../../uav-ground-station/README.md#vss-search)
for the full route reference and container list.
