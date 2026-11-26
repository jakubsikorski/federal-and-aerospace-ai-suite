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
| `VSS_MAX_RESULTS` | `50` | The most results the search popup lists. It is also VSS's `AGGREGATION_MAX_RESULTS` (the top non-overlapping time windows VSS keeps per query), so the ground station can fetch up to this many per UAV camera and always has enough to fill the list after you pick one camera. |

VSS aggregates across all indexed streams, so a camera or UAV with far more
indexed footage could take every slot. The ground station therefore also asks
VSS for each UAV's and each camera's own best matches, merges the answers and
interleaves them by (UAV, camera), so every camera with a match is
represented in the list.
The UAV and Camera filters narrow that merged set without searching again, and
the list then shows the top `VSS_MAX_RESULTS` of what remains. Raising the
value shows more results, at the cost of slower queries (more windows are
scored).

## Which results are shown

VSS's `relevance_score` is normalised per query (the best hit is always
`1.0`), so it ranks hits but cannot say whether the best one is a real
match. The ground station filters on the raw frame similarity instead:

| Setting | Default | Effect |
| --- | --- | --- |
| `VSS_SEARCH_MIN_PEAK_SCORE` | `0.23` | Hits whose best frame scores below this are dropped. Turns "an elephant" over road footage into no results. Tuned for CLIP ViT-B/32 (on aerial footage an absent object peaks at 0.21-0.22 and a present one at 0.25 or more; single words such as "person" score lower than full descriptions); another embedding model needs a different value. |
| `VSS_SEARCH_RELATIVE_CUTOFF` | `0.90` | Of the remaining hits, keep those within this fraction of the best hit's frame score for the same UAV, trimming the weak tail behind a strong match. |

Set both to `0` to show everything VSS returns. Lower `MIN_PEAK_SCORE` (for
example `0.22`) if genuine matches are being hidden; raise it if unrelated
clips appear for nonsense queries. The search popup's `filtered_out` count
(`/api/search`) shows how many hits the filters removed.

### Queries that name an object

VSS also indexes crops of the objects its detector finds, each with a label.
When a query names a detectable object (person, car, truck, bus, ...), crops
labelled as another object are dropped and the label of a matching crop is shown
as **Matched object** on the result. `VSS_SEARCH_OBJECT_MATCH` selects the
behaviour:

| Value | Effect |
| --- | --- |
| `crops` (default) | Drop crops labelled as another object; whole-frame hits stay. |
| `strict` | Return only crops labelled as the searched object. Every result shows the object, but nothing is returned when the detector misses it (small or low-resolution frames). |
| `off` | Rank by image-text similarity alone. |

## How footage is indexed

| Setting | Default | Effect |
| --- | --- | --- |
| `VSS_FRAME_INTERVAL` | `15` | Embed one frame every N frames of each live stream. Lower = finer time resolution and more embeddings (more CPU/GPU, more storage, slower queries); higher = cheaper indexing. |
| `VSS_LIVE_RETENTION_HOURS` | `24` | How long live footage and embeddings are kept (`0` = forever). |
| `VSS_LIVE_STREAM_MAX_CONCURRENT` | `8` | Live streams indexed at once, one per UAV camera. Feeds beyond it show as `queued` in the live-index panel, the tab being viewed first. Raise it to index more UAVs together (more CPU/GPU). |
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
[Troubleshooting — VSS Live Search](../../troubleshooting.md#vss-live-search).
If a matching query returns nothing, `VSS_SEARCH_MIN_PEAK_SCORE` is too high
or the footage is not indexed yet (allow `VSS_LIVE_SYNC_INTERVAL_SECONDS`
plus a few frame intervals); if search is slow, lower `VSS_MAX_RESULTS`,
raise `VSS_FRAME_INTERVAL` or move embedding to the GPU.
