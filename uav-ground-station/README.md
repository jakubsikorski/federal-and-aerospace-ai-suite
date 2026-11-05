<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# UAV Ground Station

A minimal, single-container remote viewer for the
[`uav-vision-analytics`](../uav-vision-analytics) stack. Run it on a
**separate machine** — e.g. a ground-station laptop — from the one running
`docker-compose-pymavlink.yml`/`docker-compose-uavsdk.yml`. It never talks to
that host until you enter its IP in the dashboard and press **Connect**.

## What it shows

- The live video stream, via **H.264 Relay**: the local uav-gs-backend
  configures a mediamtx sidecar (`docker-compose-mediamtx.yml`, always
  brought up alongside the base stack -- see `make up`; `configs/mediamtx.yml` keeps on-demand sources alive for 60 s) to pull the remote
  UAV host's already-H.264 RTSP stream on demand and re-serve it to the
  browser as WebRTC (WHEP) -- pure remux, no transcoding. Requires the
  remote pipeline's RTSP output to be H.264, as uav-vision-analytics publishes;
  if the remote stream is not H.264, the dashboard shows a placeholder message ("No video
  stream — start this camera's pipeline on the remote host.").
  Each card's expand button (or the player's own full-screen control)
  shows that feed full screen; Esc or "Exit full screen" returns. If a feed
  drops partway through viewing -- for example the remote RTSP publisher
  restarts -- the card notices and recovers on its own within a few
  seconds, without needing a manual page refresh.
- Scene captions (`uav/caption/<camera>` over MQTT, bridged to the browser via
  SSE), shown under the video of cameras whose pipeline has captioning on.
- One card per camera (a running pipeline with an RTSP stream, shown as `uav-<name>`),
  with the devices of its pipeline as badges (detection device, VLM device, `LIVE` for
  a live camera such as a RealSense, overlay off), and the models, prompt and video or
  camera device in the top-right information icon's tooltip, along with the
  pipeline state and name (from the DL Streamer
  Pipeline Server's REST API and the start request of each pipeline).
- CPU/GPU/NPU utilization of the UAV host (proxied from its `metrics-manager`) and of this ground-station machine (the local `uav-gs-metrics-manager` container).
- **Live Natural Language Search**: the **Search Videos** button (top bar or side
  panel, or press `/`) opens a popup where you type a natural-language prompt
  (e.g. "a red car on the road") and get a ranked result list with thumbnails
  next to a clip player (arrow keys move between results, `Esc` closes), from a
  separately deployed
  [Video Search and Summarization (VSS)](https://github.com/open-edge-platform/edge-ai-libraries/tree/main/sample-applications/video-search-and-summarization)
  search stack -- see [VSS search](#vss-search) below.
- An interactive **Architecture** popup: the services on the UAV host, the ground station and the optional VSS stack, color-coded by origin (Open Edge Platform, open source, application). Pick a flow such as live video, captions, live indexing or search to highlight its path, click a service for its role, image and APIs, and see which models run on the NPU, GPU and CPU.
- A **Flight Summary** report (top bar's **Flight Summary** button): pick one or more cameras that are currently captioning and a time window, and get an extractive summary -- an overview paragraph, recurring subjects, and a timeline of de-duplicated scene highlights grouped camera-by-camera (not interleaved by arrival time, so each camera's own story reads start-to-finish) -- built entirely from the MQTT caption stream already flowing for captioning-enabled pipelines. No VSS deployment or LLM call required; see [Flight Summary](#flight-summary) below.

## Project Structure

```text
uav-ground-station/
├── docker-compose.yml   # viz-backend (FastAPI UI + proxy/bridge APIs)
├── docker-compose-mediamtx.yml  # mediamtx sidecar for H.264 Relay video (always used, see `make up`)
├── configs/mediamtx.yml  # mediamtx settings (WebRTC only, 60 s on-demand timeouts)
├── docker-compose-vss.yml  # optional VSS search containers (make up-vss)
├── .env.example         # Copy to .env; all vars have loopback-safe defaults
└── backend/             # FastAPI app: static UI + proxy/bridge APIs
    ├── main.py
    ├── vss_search.py    # /api/search* proxy to the separate VSS stack
    ├── vss_live_sync.py # keeps VSS live-stream indexing in step with the running pipelines
    ├── uav_naming.py    # host-qualified stream names and the host/camera registry (multi-UAV)
    ├── caption_log.py   # MQTT caption ring buffer, feeds vss_summary.py
    ├── vss_summary.py   # /api/summary* -- Flight Summary report (no VSS needed)
    ├── pdf_writer.py    # dependency-free PDF renderer used by vss_summary.py
    ├── requirements.txt
    ├── Dockerfile
    └── static/           # index.html, css/, js/ (app.js, search.js, summary.js, arch.js), img/
```

Container names use the prefix `uav-gs-` (ground station): `uav-gs-backend`, `uav-gs-mediamtx` and `uav-gs-metrics-manager`. The compose service names (`viz-backend`, `mediamtx`, `gs-metrics-manager`) are unchanged.

## Quick Start

Prerequisites: Docker with the Compose plugin and `make` on this machine, Recommended to use
a machine other than the UAV host running `uav-vision-analytics` or the UAV SDK stack with its pipelines
started.

There are two options available to get the application source:

#### Option A — Download the ZIP (recommended)

Download the compressed file and get into the directory:

```bash
curl -OjL https://github.com/open-edge-platform/federal-and-aerospace-ai-suite/releases/download/fedaero-latest/uav-mission-apps.zip
```

Decompress the downloaded file:

```bash
unzip uav-mission-apps.zip
cd uav-ground-station
```

#### Option B — Clone the whole repository

Clone the repo and get into the directory:

```bash
git clone https://github.com/open-edge-platform/federal-and-aerospace-ai-suite.git --branch main
cd federal-and-aerospace-ai-suite/uav-ground-station

Then:

```bash
make up      # creates .env from template (host IP and devices auto-detected), builds, and starts the container
```

`make up` runs `make init`, which sets `VIZ_HOST_IP` and `VIZ_PUBLIC_HOST` to this
machine's LAN IP and detects the GPU/NPU devices; edit `.env` only to override them
(for example `127.0.0.1` for loopback-only access).

Then open `http://<VIZ_HOST_IP>:8080` (`http://localhost:8080` when `VIZ_HOST_IP` is `127.0.0.1`), enter the
UAV host's IP (defaults to `VIZ_COMPANION_HOST`, `127.0.0.1`), and press **Connect**. Device cards
(`cpu`/`gpu`/`npu`) are detected automatically from whichever pipelines are
currently running on the remote host.

```bash
make down    # stop and remove the stack
make logs    # tail the container's logs
```


## Configuration

All configuration is via `.env` (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `VIZ_HOST_IP` | `127.0.0.1` (`make init` sets the host's LAN IP) | Bind address for this machine's published port |
| `VIZ_PORT` | `8080` | Dashboard port |
| `VIZ_COMPANION_HOST` | `127.0.0.1` | UAV Companion Compute IP field |
| `VIZ_CAMERA_ORDER` | `front,bottom,back` | Camera card order, by a word in the stream name; unlisted cameras follow alphabetically |
| `VIZ_REMOTE_HTTPS_PORT` | `443` | UAV host's nginx HTTPS port |
| `VIZ_REMOTE_MQTT_PORT` | `1883` | UAV host's nginx MQTT passthrough port |
| `VIZ_REMOTE_RTSP_PORT` | `8555` | UAV host's raw RTSP port, pulled by the mediamtx relay |
| `VIZ_PUBLIC_HOST` | `127.0.0.1` (`make init` sets the host's LAN IP) | Address browsers use to reach this machine; advertised as the WebRTC ICE candidate. Set it when `VIZ_HOST_IP` is `0.0.0.0` |
| `MEDIAMTX_WEBRTC_PORT` / `MEDIAMTX_WEBRTC_ICE_UDP_PORT` / `MEDIAMTX_WEBRTC_ICE_TCP_PORT` | `8889` / `8189` / `8189` | Published WebRTC (WHEP) port and ICE UDP/TCP ports of the relay |
| `GPU_DEVICE` / `GPU_RENDER_DEVICE` / `NPU_DEVICE` | `/dev/null` (`make init` auto-detects) | Devices passed to `gs-metrics-manager` for this machine's utilization panel |
| `VSS_MINIO_USER` / `VSS_MINIO_PASSWORD` / `VSS_POSTGRES_USER` / `VSS_POSTGRES_PASSWORD` | _(required with `make up-vss`)_ | Credentials for the VSS overlay's MinIO and Postgres |
| `VSS_REGISTRY` / `VSS_TAG` | `intel/` / `2026.3.0-rc1` | Registry and tag of the VSS images used by `make up-vss` |
| `VSS_ENABLED` / `VSS_MANAGER_URL` / `VSS_DATASTORE_URL` | `false` / `http://host.docker.internal:12345/manager` / `.../datastore` | Only for a VSS deployed separately; `make up-vss` sets them itself |
| `VSS_MANAGER_PORT` | `3001` | Host port (bound to `VIZ_HOST_IP`) for the VSS pipeline-manager API |
| `VIDEO_GROUP_ID` / `RENDER_GROUP_ID` | `44` / `992` | Host's `video`/`render` group ids (`getent group video render`; `make init` does not set them), needed for GPU/NPU VSS devices |
| `VSS_EMBEDDING_MODEL` | _(required with `make up-vss`)_ | Embedding model VSS uses for frames and queries, for example `CLIP/clip-vit-b-32` |
| `DATAPREP_DETECTION_MODEL` | _(required with `make up-vss`)_ | Object detection model VSS dataprep uses for crop extraction, for example `yolox_s` |
| `VSS_ENABLE_OBJECT_DETECTION` | `true` | Run object-detection crops alongside embedding during ingestion |
| `VSS_INGEST_EMBEDDING_DEVICE` / `VSS_DETECTION_DEVICE` / `VSS_QUERY_EMBEDDING_DEVICE` | `CPU` | Devices for VSS indexing, object detection and query embedding |
| `VSS_FRAME_INTERVAL` | `15` | Live ingest embeds one frame every N frames |
| `VSS_LIVE_RETENTION_HOURS` | `24` | How long indexed live footage/embeddings are kept before being purged |
| `VSS_LIVE_SYNC_INTERVAL_SECONDS` | `15` | How often running UAV pipelines are synced to VSS live streams |
| `VSS_DATASTORE_BUCKET` | `video-summary` | VSS MinIO bucket for uploaded videos |
| `VSS_SEARCH_TIMEOUT_SECONDS` | `60` | Timeout for one search request to VSS |
| `VSS_SEARCH_MIN_PEAK_SCORE` | `0.25` | Hides hits whose best frame is less similar to the query than this; tuned for CLIP ViT-B/32, retune for other embedding models (`0` disables) |
| `VSS_SEARCH_RELATIVE_CUTOFF` | `0.90` | Keeps hits whose peak frame similarity is at least this fraction of the best hit's (`0` disables) |
| `VSS_PREVIEW_ENABLED` | `true` | Offers a cached low-bitrate SD preview of each result alongside the full-resolution HD clip; `false` always streams originals |
| `VSS_PLAYBACK_DEFAULT` | `sd` | Quality the player starts in (`sd` or `hd`); users switch with the SD/HD toggle and their choice is remembered per browser |
| `VSS_PREVIEW_HEIGHT` | `360` | Preview height in pixels (never upscaled) |
| `VSS_PREVIEW_MAX_KBPS` | `800` | Preview video bitrate cap |
| `VSS_PREVIEW_CACHE_MB` | `1024` | Disk budget for cached previews in `uav-gs-backend` |
| `VSS_PREVIEW_PREWARM` | `3` | Top results transcoded as soon as a search returns |
| `CAPTION_BUFFER_SIZE` | `1000` | Max scene captions kept per (host, camera) for the Flight Summary report |
| `CAPTION_LOG_IDLE_TTL_SECONDS` | `120` | Drop a (host, camera) caption subscriber after this many seconds of not being observed captioning |
| `VSS_MAX_RESULTS` | `50` | `video-search`'s `AGGREGATION_MAX_RESULTS` -- how many top non-overlapping time windows VSS keeps, across *all* indexed streams |

The remote-port defaults assume the standard nginx-published ports from
`uav-vision-analytics/docker-compose-pymavlink.yml`; only change them if that
stack's nginx is configured differently.

## VSS search

The **Live Natural Language Search** panel and **Search Videos** popup appear
only when the VSS overlay is used. It is a separate compose file,
`docker-compose-vss.yml`, that adds just the 8 containers VSS search needs
(pipeline-manager, video-search, multimodal-dataprep,
multimodal-embedding-serving, vector-retriever, vdms-vector-db, postgres,
minio; container names `uav-gs-vss-*`). With the default `make up` the search UI is hidden
and the search routes return 404.

1. Set the required `VSS_MINIO_*` / `VSS_POSTGRES_*` credentials, plus
   `VSS_EMBEDDING_MODEL` and `DATAPREP_DETECTION_MODEL` (no defaults), and,
   optionally, devices in `.env` -- see `.env.example`.
2. `make up-vss` (base + mediamtx + VSS overlay). The first start downloads
   the CLIP embedding model (and YOLOX, for object crops) into Docker volumes.
3. Open the dashboard and **Connect** to the UAV. Every RUNNING pipeline
   (`uav-<camera>` on `VIZ_REMOTE_RTSP_PORT`) is registered with VSS
   as a live stream and indexed continuously. A backend loop
   (`VSS_LIVE_SYNC_INTERVAL_SECONDS`) adds pipelines that start later and
   pauses ones that stop; **Disconnect** pauses all of them. Indexed footage
   is kept for `VSS_LIVE_RETENTION_HOURS`.

`make down-vss` stops everything but keeps the index; `make clean-vss-index`
also deletes the indexed footage, embeddings and stream registrations but
keeps the downloaded models; `make delete-vss-volumes` (asks for confirmation;
`FORCE=1` skips it) deletes all VSS volumes, including the models
(re-downloaded on the next start). The pipeline-manager API is published on
`VIZ_HOST_IP:VSS_MANAGER_PORT` (default 3001) for uploading videos
(`POST /videos`, then `POST /videos/search-embeddings/<videoId>`).

Live RTSP ingestion needs the `intel/*:2026.3.0-rc1` (or newer) VSS images
(`VSS_REGISTRY` / `VSS_TAG`). To use a VSS
deployed separately with its own `setup.sh --search` instead, set
`VSS_ENABLED=true` and `VSS_MANAGER_URL` / `VSS_DATASTORE_URL` in `.env`.

`uav-gs-backend` proxies everything so the browser only talks to this dashboard:

| Route | Upstream VSS call | Purpose |
|---|---|---|
| `GET /api/search/status` | `GET /manager/health`, `GET /manager/app/features` | Reachability + search feature flag (popup status pill) |
| `POST /api/search` `{"query": "..."}` | `POST /manager/search/query`, `GET /manager/videos`, `GET /manager/streams` | Ranked hits, joined with filenames / live stream names |
| `GET /api/search-video?path=<bucket>/<object>` | `GET /datastore/<bucket>/<object>` | Range-aware clip streaming (seekable `<video>`) |
| `GET /api/search-frame?video_id=...&timestamp=...` | `GET /manager/frames` | Best-matching frame (thumbnail / fallback) |
| `GET /api/search/sources` | _(local registry)_ | UAV hosts and cameras currently live-indexed, for the search popup's UAV / Camera filters |
| `GET/POST/DELETE /api/search/live-sync` | `GET/POST/PATCH/DELETE /manager/streams` | Show / start (`{"host": ...}`) / stop live indexing of the UAV feeds |

Quick check:

```bash
curl -s http://${VIZ_HOST_IP:-127.0.0.1}:${VIZ_PORT:-8080}/api/search/status
curl -s -X POST http://${VIZ_HOST_IP:-127.0.0.1}:${VIZ_PORT:-8080}/api/search \
  -H 'Content-Type: application/json' -d '{"query": "a car on the road"}'
```

### Multiple UAVs at once

The dashboard supports connecting to more than one UAV companion board in the
same browser session: a tab bar appears above the connect field once a second
host is connected, and switching tabs swaps which host's camera cards and
metrics are shown. Disconnecting (closing a tab's **×**) stops that host's VSS
live-sync indexing; switching tabs does not -- a host keeps being indexed in
the background for as long as its tab stays open, even while another tab is
active.

Because camera names are operator-chosen (`configs/cameras-*.yaml`), two UAVs
could configure the same camera name (e.g. both have a `uav-front-camera`);
each host's streams are registered with VSS under a host-qualified name
(`uav-<host>--<camera>`) so they never collide.

Each search result's `camera`/`host` fields are resolved in order from: the
embedding's own `sensor_id` metadata; failing that, the stream's
registration-time `tags` (`["uav", "<host>", "<camera>"]`, also visible in
each result's own "Tags" detail); and failing that, looking the result's
`video_id` up in VSS's current stream list as a last resort. A result VSS
still has indexed from a UAV that has since disconnected may still show
"Unknown camera" if none of these resolve.

The dashboard's search popup puts two filter controls — UAV and Camera dropdown.
The full, unfiltered result set is fetched once when the search runs, and every
subsequent filter toggle — whether switching UAVs, changing cameras, or resetting
selections — simply reshapes that same in-memory dataset without firing any 
additional network request.

Clips whose codec the browser can't decode (for example some stored
live-stream segments) fall back to VSS's
server-decoded best-matching frame plus a download link. Live segments older
than VSS's `LIVE_RETENTION_HOURS` may no longer be available.

## Flight Summary

The **Flight Summary** button (top bar) opens a report built from the MQTT
scene-caption stream (`uav/caption/<camera>`) -- the same captions shown live
under each camera's video. It works independently of VSS: no extra containers,
no LLM call, always available once a camera's pipeline is running with
captioning on.

1. `uav-gs-backend` keeps a small ring buffer of recent captions per
   `(host, camera)` (`caption_log.py`), kept warm only while the dashboard's
   normal pipeline-info polling has observed that camera captioning; an idle
   `(host, camera)` pair is dropped after `CAPTION_LOG_IDLE_TTL_SECONDS`.
2. Open **Flight Summary**, confirm the host (defaults to the connected UAV),
   pick one or more cameras from the list (populated from whichever cameras
   are currently captioning) and a time window, then **Generate**.
3. `vss_summary.py` runs an extractive map-reduce over that camera's buffered
   captions: consecutive near-identical captions are folded into one "scene"
   (with an occurrence count), a keyword tally feeds the overview paragraph,
   and a representative subset of scenes (favoring long-held/repeated ones,
   filled out with an even chronological spread of the rest) is kept per
   camera up to the requested highlight budget. With more than one camera
   selected, the timeline is **grouped by camera** -- all of one camera's
   highlights in order, then the next camera's -- rather than interleaved by
   arrival time, which is hard to follow once several cameras are captioning
   concurrently.
4. **Download PDF** renders the same report via `pdf_writer.py` (a small,
   dependency-free PDF writer -- no extra system packages).

If a search was just run, the report also appends the query and its matched
clips as an "appendix", tying a natural-language search back to the raw
caption evidence around the same time window.

| Route | Purpose |
|---|---|
| `POST /api/summary` `{"host", "cameras": [...], "window_minutes", "max_highlights"}` | JSON report, rendered inline in the modal |
| `GET /api/summary.pdf?host=...&cameras=a,b&window_minutes=15&max_highlights=20` | Same report as a downloadable PDF |

## Other backend routes

| Route | Purpose |
|---|---|
| `GET /api/runtime-config` | Default host, remote HTTPS port and the relay's WebRTC host/port for the browser |
| `GET /api/pipeline-info`, `GET /api/detect-pipelines` | Cameras and pipelines running on the UAV host |
| `GET /api/captions-stream`, `/api/captions-stream-multi` | Scene captions as SSE |
| `GET /api/metrics-stream`, `/api/metrics-stream-multi`, `/api/local-metrics-stream` | UAV host and ground-station utilization as SSE |
| `POST /api/relay-path`, `GET /api/relay-status` | Configure and check the mediamtx path `relay-<host>-<camera>` |
| `GET/POST/DELETE /api/sessions` | Connected UAV hosts, kept server-side so a page reload restores the tabs; a backend restart (new `boot_id`) starts with none connected |

## Notes

- The UAV host's TLS certificate is self-signed; the backend skips
  verification when calling it (trusted-LAN tool posture, consistent with the
  `-k`/`--insecure` curl examples in `uav-vision-analytics`'s own README).
- This project makes outbound, read-only requests to whatever host you enter;
  it never starts/stops pipelines on the remote stack.

## Notice for FFmpeg

FFmpeg is an open source project licensed under LGPL and GPL. See
<https://www.ffmpeg.org/legal.html>. You are solely responsible for determining
if your use of FFmpeg requires any additional licenses. Intel is not
responsible for obtaining any such licenses, nor liable for any licensing fees
due, in connection with your use of FFmpeg.
