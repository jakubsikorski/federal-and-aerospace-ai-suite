<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Configuration

All configuration is via `.env` (see `.env.example`). See
[Get Started](get-started.md) for the deployment walkthrough these variables
support and the [Release Notes](release-notes.md) for what changed per version.

| Variable | Default | Purpose |
|---|---|---|
| `VIZ_HOST_IP` | `127.0.0.1` (`make init` sets the host's LAN IP) | Bind address for this machine's published port |
| `VIZ_PORT` | `8080` | Dashboard port; `make up` moves it to the next free port when this machine already uses it (for example next to the UAV Mission Compute SDK, which publishes `8080`) |
| `VIZ_COMPANION_HOST` | `127.0.0.1` | UAV Companion Compute IP field |
| `VIZ_CAMERA_ORDER` | `front,bottom,back` | Camera card order, by a word in the stream name; unlisted cameras follow alphabetically |
| `VIZ_REMOTE_HTTPS_PORT` | `443` | UAV host's nginx HTTPS port |
| `VIZ_REMOTE_TLS_VERIFY` | `false` | Set `true` to verify the UAV host's TLS certificate (it is self-signed by default, so off keeps it working out of the box; a warning is logged) |
| `VIZ_REMOTE_TLS_CA_BUNDLE` | empty (system CAs) | Used when verification is on: PEM file (under `configs/certs/`, as `/certs/<file>.pem`) with the UAV hosts' certificates |
| `VIZ_REMOTE_MQTT_PORT` | `1883` | UAV host's nginx MQTT passthrough port |
| `VIZ_REMOTE_RTSP_PORT` | `8555` | UAV host's raw RTSP port, pulled by the mediamtx relay |
| `VIZ_PUBLIC_HOST` | `127.0.0.1` (`make init` sets the host's LAN IP) | Address browsers use to reach this machine; advertised as the WebRTC ICE candidate. Set it when `VIZ_HOST_IP` is `0.0.0.0` |
| `MEDIAMTX_WEBRTC_PORT` / `MEDIAMTX_WEBRTC_ICE_UDP_PORT` / `MEDIAMTX_WEBRTC_ICE_TCP_PORT` | `8889` / `8189` / `8189` | Published WebRTC (WHEP) port and ICE UDP/TCP ports of the relay; `make up` moves them to the next free port when taken; the SDK publishes `8888` and `8889`, so the ground station avoids both when the SDK is installed on the same machine |
| `GPU_DEVICE` / `GPU_RENDER_DEVICE` / `NPU_DEVICE` | `/dev/null` (`make init` auto-detects) | Devices passed to `gs-metrics-manager` for this machine's utilization panel |
| `VSS_MINIO_USER` / `VSS_MINIO_PASSWORD` / `VSS_POSTGRES_USER` / `VSS_POSTGRES_PASSWORD` | _(`make init` auto-generates: a fixed username + password)_ | Credentials for the VSS overlay's MinIO and Postgres — internal to this stack only, edit `.env` yourself before `make up-vss` for different values |
| `VSS_REGISTRY` / `VSS_TAG` | `intel/` / `2026.3.0-rc1` | Registry and tag of the VSS images used by `make up-vss` |
| `VSS_ENABLED` / `VSS_MANAGER_URL` / `VSS_DATASTORE_URL` | `false` / `http://host.docker.internal:12345/manager` / `.../datastore` | Only for a VSS deployed separately; `make up-vss` sets them itself |
| `VSS_MANAGER_PORT` | `3001` | Host port (bound to `VIZ_HOST_IP`) for the VSS pipeline-manager API; moved to the next free port by `make up-vss` when taken |
| `VIDEO_GROUP_ID` / `RENDER_GROUP_ID` | `44` / `992` | Host's `video`/`render` group IDs (`getent group video render`; `make init` does not set them), needed for GPU/NPU VSS devices |
| `VSS_EMBEDDING_MODEL` | `CLIP/clip-vit-b-32` (`make init` pre-fills this only after confirming the download prompt; set `ACCEPT_VSS_MODELS=y` to skip it) | Embedding model VSS uses for frames and queries; edit `.env` before `make up-vss` for a different model |
| `DATAPREP_DETECTION_MODEL` | `OpenVINO/yolox_s-int8-ov` (`make init` pre-fills this only after confirming the download prompt; set `ACCEPT_VSS_MODELS=y` to skip it) | Object detection model VSS dataprep uses for crop extraction. Must be a Hugging Face OpenVINO™ Model API repository in the `OpenVINO/` namespace |
| `VSS_ENABLE_OBJECT_DETECTION` | `true` | Run object-detection crops alongside embedding during ingestion |
| `VSS_INGEST_EMBEDDING_DEVICE` / `VSS_DETECTION_DEVICE` / `VSS_QUERY_EMBEDDING_DEVICE` | `CPU` | Devices for VSS indexing, object detection, and query embedding. Set all three at once at deploy time with `make up-vss DEVICE=CPU\|GPU\|NPU\|all` instead of hand-editing `.env` — see [Get Started](get-started.md#vss-search) |
| `VSS_FRAME_INTERVAL` | `15` | Live ingest embeds one frame every N frames |
| `VSS_LIVE_RETENTION_HOURS` | `24` | How long indexed live footage/embeddings are kept before being purged |
| `VSS_LIVE_BATCH_MAX_AGE_SECONDS` | `2` | Longest a new live feed waits for its first embeddings: a partial batch of frames is embedded once its oldest frame is this many seconds old (VSS's own default is `20`) |
| `VSS_LIVE_SYNC_INTERVAL_SECONDS` | `15` | How often running UAV pipelines are synced to VSS live streams |
| `VSS_DATASTORE_BUCKET` | `video-summary` | VSS MinIO bucket for uploaded videos |
| `VSS_SEARCH_TIMEOUT_SECONDS` | `60` | Timeout for one search request to VSS |
| `VSS_SEARCH_MIN_PEAK_SCORE` | `0.23` | Hides hits whose best frame is less similar to the query than this; tuned for CLIP ViT-B/32, retune for other embedding models (`0` disables) |
| `VSS_SEARCH_RELATIVE_CUTOFF` | `0.90` | Keeps hits whose peak frame similarity is at least this fraction of the best hit's of the same UAV (`0` disables) |
| `VSS_SEARCH_OBJECT_MATCH` | `crops` | For a query that names a detectable object (person, car, truck, bus, ...): `crops` drops object crops that VSS's detector labeled as another object; `strict` returns only crops labeled as that object (nothing if the detector misses it); `off` ranks by image-text similarity alone |
| `VSS_PREVIEW_ENABLED` | `true` | Offers a cached low-bitrate SD preview of each result alongside the full-resolution HD clip; `false` always streams originals |
| `VSS_PLAYBACK_DEFAULT` | `sd` | Quality the player starts in (`sd` or `hd`); users switch with the SD/HD toggle and their choice is remembered per browser |
| `VSS_PREVIEW_HEIGHT` | `360` | Preview height in pixels (never upscaled) |
| `VSS_PREVIEW_MAX_KBPS` | `800` | Preview video bitrate cap |
| `VSS_PREVIEW_CACHE_MB` | `1024` | Disk budget for cached previews in `uav-gs-backend` |
| `VSS_PREVIEW_PREWARM` | `3` | Top results transcoded as soon as a search returns |
| `CAPTION_BUFFER_SIZE` | `1000` | Maximum scene captions kept per (host, camera) for the Flight Summary report |
| `CAPTION_LOG_IDLE_TTL_SECONDS` | `120` | Drop a (host, camera) caption subscriber after this many seconds of not being observed captioning |
| `VSS_MAX_RESULTS` | `50` | Most results the search popup lists. Also `video-search`'s `AGGREGATION_MAX_RESULTS` (top non-overlapping time windows VSS keeps **per query**): an unscoped search issues one query per UAV host and per camera (see [VSS Search](get-started.md#vss-search)) and merges them, so the API response can hold up to this many per camera. The dashboard lists the top `VSS_MAX_RESULTS` after its UAV/Camera filters are applied, and the response's `max_results` field reports the value |
| `VSS_LIVE_STREAM_MAX_CONCURRENT` | `8` | Live streams VSS indexes at once (one per UAV camera); feeds beyond it show as `queued` in the live-index panel |

The remote-port defaults assume the standard nginx-published ports from
`uav-vision-analytics/docker-compose-pymavlink.yml`; only change them if that
stack's nginx is configured differently.

## Backend Routes

`uav-gs-backend` proxies everything so the browser only talks to this dashboard.

### VSS Search

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

### Flight Summary

| Route | Purpose |
|---|---|
| `POST /api/summary` `{"host", "cameras": [...], "window_minutes", "max_highlights"}` | JSON report, rendered inline in the modal |
| `GET /api/summary.pdf?host=...&cameras=a,b&window_minutes=15&max_highlights=20` | Same report as a downloadable PDF |

### Other Routes

| Route | Purpose |
|---|---|
| `GET /api/runtime-config` | Default host, remote HTTPS port, and the relay's WebRTC host/port for the browser |
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
