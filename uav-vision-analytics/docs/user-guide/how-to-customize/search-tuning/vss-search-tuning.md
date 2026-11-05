<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Tune VSS Live Search

The ground station's [live search](../../how-to-guides/vss-live-search.md) is
tuned entirely through `uav-ground-station/.env`. The defaults suit one UAV
or a small fleet with the bundled CLIP embedding model; this page lists the
settings worth changing and what each one trades off. After any change:

```bash
cd uav-ground-station
make down-vss && make up-vss      # VSS_SEARCH_* only need: docker compose ... up -d --build viz-backend
```

## How many candidates VSS keeps

| Setting | Default | Effect |
| --- | --- | --- |
| `VSS_MAX_RESULTS` | `50` | VSS's `AGGREGATION_MAX_RESULTS`: the top non-overlapping time windows kept **across all indexed streams together** before the ground station sees them. |

Aggregation is global, so a camera or UAV with far more indexed footage can
take most of the slots for a query that matches it well. Raising the value
gives other streams a chance to appear, at the cost of slower queries (more
windows are scored). The ground station interleaves the kept results by
(UAV, camera) for display, but cannot show a stream VSS dropped.

## Which results are shown

VSS's `relevance_score` is normalised per query (the best hit is always
`1.0`), so it ranks hits but cannot say whether the best one is a real
match. The ground station filters on the raw frame similarity instead:

| Setting | Default | Effect |
| --- | --- | --- |
| `VSS_SEARCH_MIN_PEAK_SCORE` | `0.25` | Hits whose best frame scores below this are dropped. Turns "an elephant" over road footage into no results. Tuned for CLIP ViT-B/32; another embedding model needs a different value. |
| `VSS_SEARCH_RELATIVE_CUTOFF` | `0.90` | Of the remaining hits, keep those within this fraction of the best hit's frame score, trimming the weak tail behind a strong match. |

Set both to `0` to show everything VSS returns. Lower `MIN_PEAK_SCORE` (for
example `0.22`) if genuine matches are being hidden; raise it if unrelated
clips appear for nonsense queries. The search popup's `filtered_out` count
(`/api/search`) shows how many hits the filters removed.

## How footage is indexed

| Setting | Default | Effect |
| --- | --- | --- |
| `VSS_FRAME_INTERVAL` | `15` | Embed one frame every N frames of each live stream. Lower = finer time resolution and more embeddings (more CPU/GPU, more storage, slower queries); higher = cheaper indexing. |
| `VSS_LIVE_RETENTION_HOURS` | `24` | How long live footage and embeddings are kept (`0` = forever). |
| `VSS_INGEST_EMBEDDING_DEVICE`, `VSS_DETECTION_DEVICE`, `VSS_QUERY_EMBEDDING_DEVICE` | `CPU` | Devices for frame embedding, object crops and query embedding; `GPU`/`NPU` need the device entries in `.env`. |
| `VSS_ENABLE_OBJECT_DETECTION` | `true` | Also embed detected-object crops, which improves queries for small objects at extra indexing cost. |
| `VSS_EMBEDDING_MODEL` | `CLIP/clip-vit-b-32` | Embedding model for both indexing and queries; changing it invalidates the existing index (`make clean-vss-index`) and the `MIN_PEAK_SCORE` calibration. |

## Playback

| Setting | Default | Effect |
| --- | --- | --- |
| `VSS_PREVIEW_ENABLED` / `VSS_PLAYBACK_DEFAULT` | `true` / `sd` | Serve a cached low-bitrate preview (`sd`) or the original clip (`hd`) first. |
| `VSS_PREVIEW_HEIGHT` / `VSS_PREVIEW_MAX_KBPS` | `360` / `800` | Preview resolution and bitrate for slow links. |
| `VSS_PREVIEW_CACHE_MB` / `VSS_PREVIEW_PREWARM` | `1024` / `3` | Preview cache size, and how many top results are transcoded as soon as a search returns. |

## Troubleshooting

A query that returns the full `VSS_MAX_RESULTS` every time, one stream
dominating the results, and footage that never becomes searchable are
covered in
[Troubleshooting — VSS Live Search](../../how-to-guides/troubleshooting.md#vss-live-search).
If a matching query returns nothing, `VSS_SEARCH_MIN_PEAK_SCORE` is too high
or the footage is not indexed yet (allow `VSS_LIVE_SYNC_INTERVAL_SECONDS`
plus a few frame intervals); if search is slow, lower `VSS_MAX_RESULTS`,
raise `VSS_FRAME_INTERVAL` or move embedding to the GPU.
