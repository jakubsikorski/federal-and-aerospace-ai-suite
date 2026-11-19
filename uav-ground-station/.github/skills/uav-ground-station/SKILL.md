---
name: uav-ground-station
description: >-
  Deploy and configure the uav-ground-station remote viewer — a single
  dashboard that connects to one or more already-running uav-vision-analytics
  (or UAVSDK-mode) hosts over the network, shows their live RTSP video (via a
  local mediamtx H.264-relay sidecar), scene captions, per-camera/UAV
  metrics, a Flight Summary report built from captions, and optionally a
  Video Search and Summarization (VSS) "Live Natural Language Search" panel.
  USE FOR: "stand up the ground station / dashboard", "I want to view my
  drone's video from another machine", "add natural-language video search to
  the dashboard", "connect the viewer to a different UAV host", "the
  dashboard doesn't show video / captions / search". DO NOT USE FOR:
  deploying the UAV-side stack itself (use `uav-vision-analytics` and its
  customization skills), or tuning an already-deployed VSS search's result
  count/relevance/indexing settings (use `uav-tune-vss-search` instead).
license: Apache-2.0
compatibility: >-
  Requires Docker + Docker Compose v2 and `make` on a machine that can reach
  the target UAV host's nginx reverse proxy (443, 8555) and MQTT (1883) over
  the network — typically a separate ground-station machine, not the UAV
  host itself. The optional VSS overlay (`make up-vss`) additionally needs
  ~1.6 GB of disk for downloaded embedding/detection models on first start,
  and does its own model download the first time it runs.
---

<!-- SPDX-FileCopyrightText: (C) 2026 Intel Corporation -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# UAV Ground Station Skill

Deploy the `uav-ground-station` remote viewer: a single container
(`viz-backend`) that a user points at one or more `uav-vision-analytics` (or
UAVSDK-mode) hosts by entering each host's IP in the dashboard UI — nothing
here talks to a UAV host until a user does that. Video always arrives via
**H.264 Relay** (a local `mediamtx` sidecar pulls the remote RTSP stream
on-demand and re-serves it as WebRTC, no transcoding), always brought up
together (`make up`). An optional overlay (`make up-vss`) adds eight more
containers implementing **Live Natural Language Search** over indexed
footage. This skill deploys/reconfigures the ground station itself; it does
**not** build the UAV-side stack (see `uav-vision-analytics` for that) and
is not a substitute for `uav-tune-vss-search` once VSS is already running.

## Relationship to Other Skills

```
uav-vision-analytics (separate machine, or the same one)
   produces: uav-<camera> RTSP streams, uav/caption/<camera> MQTT, nginx REST API
         │
         ▼ (user enters this host's IP in the dashboard UI — no compose link)
uav-ground-station  (THIS skill)
   viz-backend  — dashboard, relay config, Flight Summary, VSS proxy
   mediamtx     — H.264 relay sidecar (always on, `make up`)
   gs-metrics-manager — this machine's own CPU/GPU/NPU utilization panel
   [optional, `make up-vss`] 8 VSS containers — Live Natural Language Search
         │
         ▼ (once VSS is deployed and running)
uav-tune-vss-search  — tune result count/relevance/indexing/device settings
```

If a user asks to both build a UAV stack and view it remotely in one
request, run `uav-vision-analytics` first, then this skill, passing along
the UAV host's reachable IP as `{{VIZ_COMPANION_HOST}}` — never duplicate
either skill's steps inside the other.

## Canonical Documentation (read before acting — do not rely on memorized snippets)

| Task | Read this file in the target repo first |
| --- | --- |
| What the dashboard shows, architecture, Quick Start | `uav-ground-station/README.md` |
| Full deployment walkthrough: both sides, VSS overlay, multi-UAV, Flight Summary | `uav-ground-station/docs/user-guide/get-started.md` |
| Every `.env` variable and the backend route reference | `uav-ground-station/docs/user-guide/configuration.md` and `uav-ground-station/.env.example` |
| VSS search tuning once deployed (separate skill) | `uav-vision-analytics/docs/user-guide/how-to-customize/search-tuning/vss-search-tuning.md` |

Read the live copy of these files in the target repo — port numbers,
required VSS variables, and container names are maintained there and can
change between releases; do not hardcode a remembered value.

## How to Use This Skill

1. Confirm this is being deployed on the intended machine (recommended:
   separate from the `uav-vision-analytics` host) and get `{{STACK_DIR}}`
   (the `uav-ground-station` checkout path).
2. Ask, in ONE batched question (defaults in brackets):
   - Basic viewer only, or with Live Natural Language Search (VSS)? [basic]
   - The UAV host's IP/hostname to pre-fill as the dashboard's default
     companion IP (`VIZ_COMPANION_HOST`) — optional, can also be typed into
     the UI per session. [none — leave default, enter it in the UI]
   - Bind address: LAN-reachable (`VIZ_HOST_IP`/`VIZ_PUBLIC_HOST` = this
     machine's IP, `make init` auto-detects it) or loopback-only
     (`127.0.0.1`, safer if this host is not on a trusted/isolated network)? [LAN]
   - If VSS requested: embedding model [`CLIP/clip-vit-b-32`, offered by
     `make init`'s download-consent prompt — only ask if they want a
     different model], detection model [`OpenVINO/yolox_s-int8-ov`, same
     prompt — only ask if they want a different model], and the inference
     device [`CPU`]. For one uniform device across ingest/detection/query
     embedding, `make up-vss DEVICE=CPU|GPU|NPU|all` sets all three
     `VSS_*_DEVICE` variables in `.env` in one step (`all` auto-picks GPU >
     NPU > CPU from what `make init` already detected) — only ask about the
     three devices independently if the user explicitly wants a mixed
     setup (e.g. ingest on GPU, query on CPU).
3. Read `README.md`/`docs/user-guide/get-started.md`/`docs/user-guide/configuration.md`/
   `.env.example` for the exact current variable names and defaults before
   writing `.env`.
4. Run `make init` (auto-generates VSS credentials and auto-detects host
   IP/GPU/NPU; for the VSS models it prints the two Hugging Face model names
   plus their license pages and asks `Pre-fill .env with these default models
   now? [y/N]` before writing anything — relay that prompt/its answer to the
   user rather than silently accepting; `ACCEPT_VSS_MODELS=y` answers yes
   non-interactively if they've already said so). Edit `.env` only for
   choices that differ from the defaults above (or if they declined the
   prompt and need `VSS_EMBEDDING_MODEL`/`DATAPREP_DETECTION_MODEL` set by
   hand), then `make up` (or `make up-vss DEVICE=CPU|GPU|NPU|all` if a
   non-default uniform device was requested — this both starts VSS and sets
   all three device variables in one step, no manual `.env` edit needed).
5. Verify per the guardrails/completion criteria below.

## Parameter Validation (enforce BEFORE editing `.env` or starting the stack)

| Param | Rule | Why |
| --- | --- | --- |
| `VIZ_HOST_IP` / `VIZ_PUBLIC_HOST` | valid IP/hostname, or `127.0.0.1` | binds the dashboard/mediamtx ports; `0.0.0.0` needs `VIZ_PUBLIC_HOST` set separately since mediamtx can't advertise `0.0.0.0` as a WebRTC candidate |
| `VIZ_COMPANION_HOST` | optional; IP/hostname, no scheme/port | only pre-fills the UI field — never a hard dependency at stack-start time |
| VSS mode | only requested if the user actually wants natural-language search; never default to it | 8 extra containers + a first-run multi-hundred-MB-to-~1.6GB model download |
| `VSS_MINIO_USER` | length ≥ 3 | MinIO refuses to start otherwise; `make init` auto-generates a valid value, don't ask the user for one unless they want to override it |
| `VSS_MINIO_PASSWORD` | length ≥ 8 | MinIO refuses to start otherwise; `make init` auto-generates a valid value (random, via `openssl rand`), don't ask the user for one unless they want to override it |
| `VSS_POSTGRES_USER` / `VSS_POSTGRES_PASSWORD` | non-empty | `make init` auto-generates these too; `make up-vss` fails fast (one message listing everything missing) only if `.env` was hand-edited/created without running `make init` |
| `VSS_EMBEDDING_MODEL` / `DATAPREP_DETECTION_MODEL` | non-empty | these are downloaded from Hugging Face on first `make up-vss`, so `make init` only pre-fills `CLIP/clip-vit-b-32` / `OpenVINO/yolox_s-int8-ov` (the defaults declared in `.env.example`) after an explicit y/N download-consent prompt (`ACCEPT_VSS_MODELS=y` to skip it); if declined, both are left blank and `make up-vss` fails fast listing them until set manually |
| `VSS_INGEST_EMBEDDING_DEVICE` / `VSS_DETECTION_DEVICE` / `VSS_QUERY_EMBEDDING_DEVICE` | `CPU`/`GPU`/`NPU` | GPU/NPU need `GPU_DEVICE`/`GPU_RENDER_DEVICE`/`NPU_DEVICE` and `VIDEO_GROUP_ID`/`RENDER_GROUP_ID` already set (`make init` auto-detects) |

## Execution Guardrails

- This is a **separate deployment** from `uav-vision-analytics` — never try
  to co-locate it inside that stack's compose files or `make pymav-up`; it
  has its own `Makefile`/`.env`/containers and is started independently.
- Never invent a deployment-mode choice that doesn't exist in this repo
  (e.g. there is no "physical camera mode" toggle here — that concept
  belongs entirely to `uav-vision-analytics`'s `configs/cameras-*.yaml`, out
  of scope for this skill).
- `make up` always includes the mediamtx relay sidecar — there is no
  "video-less" mode; if H.264 Relay isn't wanted, that's a reason to
  question the request, not a flag to pass.
- Keep `sourceOnDemandStartTimeout`/`sourceOnDemandCloseAfter` at `60s`
  (`configs/mediamtx.yml` and the per-path values set in `backend/main.py`):
  the relay starts a UAV stream on first viewer, and mediamtx's 10 s default drops
  the first connection while the remote pipeline is busy under concurrent
  detection + VLM load.
- Single-machine test next to the UAV Mission Compute SDK: the SDK publishes `8080`
  (REST), `8554` (RTSP), `8888` (HLS) and `8889` (WebRTC). `make up`/`make up-vss`
  run `scripts/resolve-ports.sh`, which moves `VIZ_PORT`, `MEDIAMTX_WEBRTC_PORT`, the
  WebRTC ICE ports and `VSS_MANAGER_PORT` in `.env` to the next free port (typically
  `8081`/`8890`), never onto one of those four SDK ports while SDK containers exist,
  and leaves this stack's own ports alone. The dashboard URL it prints uses the
  resulting `VIZ_PORT`, not `8080`. If the ground station was started before the SDK
  existed on the machine, the SDK's `make` fails on the ports it holds (or its
  MediaMTX loses its ports and the cameras have no RTSP source): start the SDK first,
  or change the ground station's ports in `.env` and run `make up` again.
- Never fabricate/accept committed `VSS_MINIO_PASSWORD`/`VSS_POSTGRES_PASSWORD`
  values into any tracked file — these live only in the gitignored `.env`.
  `make init` already auto-generates a strong random value for both
  passwords (plus fixed usernames) on first run and never prints them to the
  console; don't ask the user for these unless they explicitly want to set
  their own instead of the generated default.
- Never let `VSS_EMBEDDING_MODEL`/`DATAPREP_DETECTION_MODEL` get downloaded
  without the user's explicit knowledge — this is why `make init` shows both
  Hugging Face model names and their license pages and requires a `y`/`N`
  answer (`ACCEPT_VSS_MODELS=y` only for a user who has already agreed, e.g.
  scripted/CI deploys) before writing `VSS_EMBEDDING_MODEL=CLIP/clip-vit-b-32`
  / `DATAPREP_DETECTION_MODEL=OpenVINO/yolox_s-int8-ov` into `.env`. Relay
  that prompt to an interactive user instead of answering it yourself; if
  they decline (or already have), tell them how to set their own values by
  hand (`sed -i` one-liners are printed, or edit `.env` directly) before
  `make up-vss`.
- Before `make up-vss`, confirm all six `VSS_REQUIRED_VARS`
  (`VSS_MINIO_USER`, `VSS_MINIO_PASSWORD`, `VSS_POSTGRES_USER`,
  `VSS_POSTGRES_PASSWORD`, `VSS_EMBEDDING_MODEL`, `DATAPREP_DETECTION_MODEL`)
  are set — normally true automatically once `make init` has run.
  `make up-vss`'s own `_check_vss_env` target fails fast with a single
  message if not (e.g. `.env` was hand-created without `make init`), but
  tell the user proactively so they aren't guessing from a Makefile error.
- `make up-vss`'s first run downloads the embedding/detection models into
  the `vss-ov-models`/`vss-yolox-models` volumes — this can take several
  minutes and needs network/proxy access exactly like `make vlm-model` in
  `uav-vision-analytics` (shell-level `http_proxy`/`https_proxy`/`no_proxy`,
  not a container build arg); warn the user before starting it the first time.
- `make down-vss` keeps indexed footage/embeddings (only stops containers);
  `make clean-vss-index` additionally deletes the index/footage volumes but
  keeps downloaded models; `make delete-vss-volumes` deletes everything
  including the ~1.6 GB of downloaded models and prompts for confirmation
  unless `FORCE=1` — never run the last two without the user's explicit
  confirmation.
- Video only appears once the remote `uav-vision-analytics` pipeline is
  actually `RUNNING` and the dashboard is connected to that host via the UI
  — "no video stream" on a card is expected, not a bug, if no camera is
  armed/streaming on the remote host yet.
- Live Natural Language Search only appears once `VSS_ENABLED=true` (set
  automatically by the `up-vss` overlay) — a `basic` deployment correctly
  hides the Search button and returns `404` from `/api/search*`; this is not
  a misconfiguration to "fix".

## Completion Criteria

1. `make init` creates `.env` (from `.env.example`) with `VIZ_HOST_IP`/
   `VIZ_PUBLIC_HOST` auto-detected (or left `127.0.0.1` if requested).
2. `make up` (or `make up-vss`) starts cleanly; `docker ps` shows
   `uav-gs-backend`, `uav-gs-mediamtx`, `uav-gs-metrics-manager` all
   `running` (plus `uav-gs-vss-pipeline-manager` and the other 7 VSS
   containers if VSS was requested).
3. `http://<VIZ_HOST_IP>:<VIZ_PORT>/` (default `8080`) returns HTTP 200 and
   serves the dashboard UI.
4. Entering a reachable `uav-vision-analytics` host's IP and pressing
   Connect shows its running camera(s) as cards, with video once a pipeline
   is armed/streaming, and captions under any camera with captioning on.
5. If VSS was requested: the Search button is visible, `make up-vss`'s
   model download completed (volumes `vss-ov-models`/`vss-yolox-models`
   populated), and a test query against the Search popup returns a ranked
   result list once at least one connected camera has been indexing for a
   few `VSS_LIVE_SYNC_INTERVAL_SECONDS` cycles.
6. `make down` stops the base stack (`uav-gs-backend`, `uav-gs-mediamtx`,
   `uav-gs-metrics-manager`) cleanly. If VSS was started with `make up-vss`,
   `make down` alone does **not** stop the 8 VSS containers — `make down-vss`
   must be run as well (or instead, since it also stops the base stack) —
   without having been asked to delete indexed data unless the user
   explicitly requested `clean-vss-index`/`delete-vss-volumes`.
