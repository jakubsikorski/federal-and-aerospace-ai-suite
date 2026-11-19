---
name: uav-tune-vss-search
description: >-
  Tune the uav-ground-station's Video Search and Summarization (VSS) live
  natural-language search over UAV camera streams, by editing
  uav-ground-station/.env (result count, relevance filters, indexing rate
  and retention, embedding/detection devices and models, playback preview
  settings) and reapplying the VSS overlay. USE FOR: "search returns too
  many/few results", "one camera/UAV dominates every search", "a query that
  should match returns nothing", "footage isn't searchable yet", "search is
  slow", "move VSS embedding to the GPU", "change the VSS embedding or
  detection model". DO NOT USE FOR: anything in the uav-vision-analytics
  stack itself (cameras, detection/captioning models, pipelines, overlay —
  use the corresponding `uav-manage-cameras` / `uav-change-detection-model` /
  `uav-change-vlm-model` / `uav-manage-pipelines` /
  `uav-customize-telemetry-overlay` skills), or standing up VSS for the
  first time from scratch.
license: Apache-2.0
compatibility: >-
  Requires an already-deployed uav-ground-station stack checked out at a
  known path, with the VSS overlay buildable (`make up-vss`). Changing
  `VSS_EMBEDDING_MODEL` invalidates the existing search index.
---

<!-- SPDX-FileCopyrightText: (C) 2026 Intel Corporation -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Tune UAV VSS Live Search Skill

VSS live search is a `uav-ground-station` feature (not part of
`uav-vision-analytics` itself — it indexes the RTSP streams that app
produces). Every setting is a variable in `uav-ground-station/.env`; this
skill edits the right ones for the symptom described and reapplies the
overlay.

## Canonical Documentation (read before acting — do not rely on memorized snippets)

| Task | Read this file in the target repo first |
| --- | --- |
| Every tunable `VSS_*` setting, its default, and its effect/trade-off | `uav-vision-analytics/docs/user-guide/how-to-customize/search-tuning/vss-search-tuning.md` (documents `uav-ground-station/.env`) |
| What live search is and how it's enabled | `uav-vision-analytics/docs/user-guide/how-to-guides/vss-live-search.md` |
| Symptom-specific troubleshooting (one stream dominating, full-result-count every time, footage never searchable) | `uav-vision-analytics/docs/user-guide/how-to-guides/troubleshooting.md#vss-live-search` |
| Current values, required-vs-optional variables | `uav-ground-station/.env.example` (template) and the live `uav-ground-station/.env` |

Read the live copy of the tuning doc and `.env`/`.env.example` in the target
repos together — default values and which variables are required (`make
up-vss` fails fast if a required one is unset) can change between releases;
do not hardcode a remembered default.

## How to Use This Skill

1. Confirm the path to the `uav-ground-station` checkout and whether the VSS
   overlay is already enabled (`VSS_ENABLED`/is `make up-vss` already running).
2. Ask, in one batched question, for the symptom or goal (too many/few
   results, one stream dominating, a query returning nothing, slow search,
   footage not yet searchable, move a device to GPU/NPU, change the
   embedding/detection model) — map it to the relevant setting(s) using the
   tuning doc's tables (result count / relevance / indexing / playback).
3. Read the current `uav-ground-station/.env` to see existing values before
   changing anything; change only the variable(s) the symptom maps to.
4. Apply and verify per the guardrails and completion criteria below.

## Parameter Validation (enforce BEFORE editing `.env`)

| Setting | Rule | Why |
| --- | --- | --- |
| `VSS_SEARCH_MIN_PEAK_SCORE`, `VSS_SEARCH_RELATIVE_CUTOFF` | numeric, `0`–`1` | calibrated for the embedding model in use; `0` disables that filter |
| `VSS_MAX_RESULTS` | positive integer | most results the popup lists; raising it slows queries (more windows scored) |
| `VSS_LIVE_STREAM_MAX_CONCURRENT` | positive integer | live feeds VSS indexes at once; extra feeds show as `queued`, raising it adds CPU/GPU load |
| `VSS_FRAME_INTERVAL` | positive integer | lower = finer time resolution, more CPU/GPU/storage |
| `VSS_INGEST_EMBEDDING_DEVICE` / `VSS_DETECTION_DEVICE` / `VSS_QUERY_EMBEDDING_DEVICE` | `CPU`/`GPU`/`NPU` | GPU/NPU need the matching `GPU_DEVICE`/`GPU_RENDER_DEVICE`/`NPU_DEVICE` and group IDs already set in `.env` |
| `VSS_EMBEDDING_MODEL` | required, no default | `make up-vss` fails fast if unset; changing it invalidates the existing index and the `MIN_PEAK_SCORE` calibration |
| `DATAPREP_DETECTION_MODEL` | required, no default | `make up-vss` fails fast if unset |
| `VSS_MINIO_USER`/`PASSWORD`, `VSS_POSTGRES_USER`/`PASSWORD` | required, never commit real values | `make up-vss` fails fast with a single message listing everything missing |

## Execution Guardrails

- This skill edits `uav-ground-station/.env`, a different repo/stack from
  `uav-vision-analytics` — confirm the user means the ground station's
  search tuning, not a uav-vision-analytics camera/model/pipeline change.
- Changing `VSS_EMBEDDING_MODEL` invalidates the existing index and the
  `VSS_SEARCH_MIN_PEAK_SCORE` calibration — tell the user before applying,
  and that `make clean-vss-index` (and likely re-tuning `MIN_PEAK_SCORE`) is
  needed afterward.
- Moving a device setting to `GPU`/`NPU` requires the corresponding
  `GPU_DEVICE`/`GPU_RENDER_DEVICE`/`NPU_DEVICE` and group IDs already set in
  `.env` (same auto-detection as `make init`) — verify they're non-empty
  before changing the device variable, or the overlay will fail to start.
  When all three of `VSS_INGEST_EMBEDDING_DEVICE`/`VSS_DETECTION_DEVICE`/
  `VSS_QUERY_EMBEDDING_DEVICE` should move together (the common case), prefer
  `make up-vss DEVICE=GPU|NPU|CPU|all` over hand-editing `.env` — it sets all
  three in one step and is safe to re-run on an already-deployed stack
  (idempotent, only rewrites those three variables then restarts the VSS
  containers). Only hand-edit the individual `.env` variables for a mixed
  setup (e.g. ingest on GPU, query on CPU).
- Most settings only need `docker compose ... up -d --build viz-backend`
  (per the tuning doc's header) to take effect; a few (embedding/detection
  device or model changes affecting the VSS containers themselves) need the
  fuller `make down-vss && make up-vss` — use whichever the doc specifies
  for the setting(s) actually changed, don't default to the heavier one
  without checking.
- Never commit real `VSS_MINIO_PASSWORD`/`VSS_POSTGRES_PASSWORD` values into
  any tracked file; only `.env` (gitignored) holds them.
- Report exactly which variables changed, old → new value, and which
  command is needed to apply them; don't run `make down-vss`/`up-vss`
  without the user's confirmation since it's disruptive to active sessions.

## Completion Criteria

1. The edited `uav-ground-station/.env` has only the intended variable(s)
   changed, each passing its validation rule above.
2. `make up-vss` (or the lighter rebuild for a `VSS_SEARCH_*`-only change)
   starts cleanly with no "set these in .env" error.
3. A test query against `/api/search` reflects the intended effect (fewer/
   more results, previously-hidden match now appears, `filtered_out` count
   changes as expected) once indexing has caught up
   (`VSS_LIVE_SYNC_INTERVAL_SECONDS` plus a few frame intervals).
4. No single stream/UAV still dominates results if that was the reported
   symptom (verify via the ground station's per-camera/per-UAV breakdown).
5. If `VSS_EMBEDDING_MODEL` changed: `make clean-vss-index` was run and the
   user was told `MIN_PEAK_SCORE` likely needs re-tuning for the new model.
