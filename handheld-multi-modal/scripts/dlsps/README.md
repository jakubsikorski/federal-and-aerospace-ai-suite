# DLSPS contextual crop callbacks

Requested: “Make the first script for measurements and write the full version. Put them both under scripts/dlsps”.

- `crop_probe.py`: measurement callback. Saves at most 20 contextual JPEGs per invocation and reports frame mapping/conversion, crop copy, JPEG, write, and callback timings. Use `max_samples=0` for the metadata-only baseline.
- `crop_candidates.py`: bounded producer. Prioritizes first snapshots, then meaningful improvements; writes contextual images plus durable JSON events and optionally publishes `detections/snips` over MQTT QoS 1.
- `test_crops.py`: runnable geometry, overload, persistence and lifecycle checks; `--live` exercises actual YOLO26s/NPU/VA frames and independently receives/correlates MQTT crop events.

These are application-owned callbacks for the stock `gvapython` element. They do not implement a new GStreamer plugin and do not need `ADDITIONAL_GST_PLUGIN_PATH`. They introduce no model downloads or new service. Both scripts must be present together because the probe reuses the producer's geometry and mapping functions.

## Placement and dependencies

Use the pinned `intel/dlstreamer-pipeline-server:2026.2.0-ubuntu24` image and existing YOLO26s IR. Python, gstgva, NumPy, OpenCV and Paho are present in that image. No pip installation is required.

Mount this directory read-only at `/opt/handheld-crops`, and give the producer a dedicated writable output directory. Add the script directory to Python's existing search path; **preserve the image's existing PYTHONPATH**, including its GStreamer overrides. Replacing it can break gstgva/GstAnalytics imports.

Callback pipeline fragment (choose one callback per run):

```text
... ! gvatrack tracking-type=zero-term-imageless config=tracking_per_class=false
    ! gvametaconvert add-empty-results=true
    ! gvapython module=/opt/handheld-crops/crop_probe.py class=CropProbe
    ! ... existing output chain ...
```

Full producer fragment:

```text
... ! gvatrack tracking-type=zero-term-imageless config=tracking_per_class=false
    ! gvametaconvert add-empty-results=true
    ! gvapython name=cropcandidates module=/opt/handheld-crops/crop_candidates.py class=CropCandidates
    ! ... existing output chain ...
```

**Do not put `kwarg="{...}"` directly in the config.json `pipeline` string.** DLSPS runs that string through `str.format()` (for `{auto_source}`) and then a Jinja2 template render before handing it to `gst_parse_launch`. Both layers, plus `gst_parse_launch`'s own quote handling, mangle embedded JSON braces/quotes — every escaping combination tried (`\"`, `{{ }}`, `arg=` instead of `kwarg=`) either throws `KeyError`/`TemplateSyntaxError` at request time or silently strips the quotes so gvapython's own JSON parser rejects the bare value.

Instead, leave `gvapython` bare (name only, no `arg=`/`kwarg=`) in the pipeline string, and declare the config path as a DLSPS pipeline `parameter` mapped to that element's `kwarg` property — the same mechanism used for `detection-properties`/`model`:

```json
"parameters": {
    "type": "object",
    "properties": {
        "crop-config": {
            "element": { "name": "cropcandidates", "property": "kwarg" },
            "type": "string"
        }
    }
}
```

Then pass the value in the `POST /pipelines/.../{name}` launch payload, where it goes through normal JSON decoding (no gst-launch text parsing involved) straight to `element.set_property()`:

```json
"parameters": {
    "detection-properties": { "model": "/home/pipeline-server/yolo_models/yolo26s/INT8/yolo26s.xml", "device": "NPU" },
    "crop-config": "{\"config\": \"/opt/handheld-crops-config.json\"}"
}
```

This is wired into all three shipped `config/dlsps/{cpu,gpu,npu}/config.json` variants and `compose/handheld-multi-modal.yaml` (crop script dir, crop config, and a writable crop-output volume are mounted into the `dlsps` service). Validated end-to-end on the NPU variant: pipeline reached `RUNNING` at ~30 FPS and produced matched `.jpg`+`.json` crop pairs in the mounted output volume for a 3-minute manual-validation run.

If the `dlsps` process previously failed to construct `CropCandidates` for any reason (e.g. bad config path) while holding the crop-output directory's advisory lock file (`.lock`), the lock is never released until the container restarts — a stuck `Resource temporarily unavailable` on `Store.__init__` means restart the `dlsps` container (and remove any stale `.lock`) before relaunching.

Placement is deliberately **after gvametaconvert, before annotation or encoding**. The producer preserves its stock observation JSON and adds `crop_context`; a missing observation JSON produces an explicit callback error and no crops. Keep the existing DLSPS appsink/mqtt_publisher path for observations. Do not switch to raw gvametapublish MQTT or put Backend in the video path.

BGR, BGRA/BGRx, NV12 and I420 are supported through the pinned frame mapping API. YUV conversion allocates a full BGR frame only when candidates have passed admission. Direct VA/NV12 access must be measured on the target hardware. If caps/mapping are incompatible, negotiate system-memory BGR before the callback using the appropriate postprocessor; that conversion's cost then occurs outside callback timings and may affect every frame. No zero-copy or hardware JPEG claim is made.

## Probe usage

`CropProbe` constructor kwargs:

| Setting | Default | Meaning |
|---|---:|---|
| output_dir | /tmp/dlsps-crop-probe | Creates a unique run directory |
| sample_every_frames | 30 | Sample one least-attempted tracked ROI on selected frames |
| max_samples | 20 | Maximum attempts, including failed mappings; 0 disables image work |
| max_tracks | 2048 | Bounded recent-ID table |
| track_ttl_seconds | 10 | Expire absent entries |
| max_frame_pixels | 8294400 | Reject oversized frame mappings |
| report_seconds | 5 | Print stats and overwrite summary.json |

The probe is intentionally synchronous so it measures the individual operations, including fsync. Its timings do not represent the asynchronous producer's latency. It prefers tracks not yet sampled, then the least-attempted currently visible track. It skips untracked ROIs and can revisit tracks once all visible tracks have been sampled. This improves track coverage but does not establish physical identity or guarantee class diversity. ID-entry rate can count the same ID again after expiry/eviction and is not a distinct-object count. Timing distributions retain the latest 2048 measurements. `callback_fps` includes startup and is not an independent detector throughput measurement. Record throughput from the pipeline too.

Each run writes `sample-NNNN.jpg`, a JSON sidecar containing original/crop rectangles and original frame messages, and `summary.json`. Per-run output is bounded by sample count and frame size; remove old measurement run directories when no longer needed.

## Producer behavior and configuration

Generate the complete deployment configuration:

```bash
python3 scripts/dlsps/crop_candidates.py --print-defaults > /tmp/handheld-crops-config.json
```

Unknown keys, invalid numeric ranges, and unsafe stream IDs fail at startup. `mqtt_host` is empty by default: file-only operation is useful for measurements. Set it to `mqtt` for this Compose broker. No credential fields are provided; never place tokens in this file.

Important defaults (starting values to calibrate, not measured optimal settings):

| Setting | Default | Behavior |
|---|---:|---|
| padding | 0.25 | Add 25% of box size to each side |
| aspect_ratio | 1.3333 | Expand to 4:3; shift/clamp at edges without trimming the visible object |
| confirm_seconds / min_observations | 0.25 / 3 | Require both before first crop |
| min_confidence / min_object_pixels | 0.4 / 16 | Cheap metadata admission checks |
| rate / burst | 2 per second / 3 | Global token bucket across all tracker IDs |
| interval_seconds | 2 | Minimum interval between attempts for a track |
| max_interval_seconds / backoff_multiplier | 16 / 2 | Repeated `not_improved` attempts for a track back off up to this interval |
| geometry_reset_position_pixels / geometry_reset_size_ratio | 12 / 0.20 | Meaningful object movement or size change resets that track to `interval_seconds` |
| candidates_per_track | 3 | First accepted image plus up to two improvements while state is retained |
| improvement | 0.15 | Later score must exceed best score by 15% |
| sharpness_max_dimension | 256 | Longest grayscale side used for Laplacian sharpness; size scoring keeps original object dimensions |
| max_tracks / track_ttl_seconds | 2048 / 10 | Bound and expire selector state; reject new entries at capacity |
| queue_jobs / queue_bytes | 8 / 32 MiB | Include reservations and worker's in-flight original crop |
| max_frame_pixels | 8294400 | Bound source mapping size, independently of queue bytes |
| max_dimension / jpeg_quality | 640 / 85 | Preserve aspect ratio; never upscale |
| store_bytes / store_candidates | 256 MiB / 1500 | Evict oldest candidates to admit new evidence |
| free_disk_bytes | 64 MiB | Keep a free-space reserve |
| retention_seconds | 3600 | Images and events expire together after one hour |

Every ROI remains in the observation JSON. Untracked, unstable, low-confidence, tiny, duplicate-ID or over-budget candidates do not force image work. First snapshots take precedence over improvements; least recently attempted candidates go first within each group. One pending job per track; an already queued job is kept rather than replaced, avoiding repeated copies during slow writes. No heuristic identity merging, re-identification or location-based suppression occurs.

The quality heuristic uses the original object's area, detector metadata confidence, and sharpness measured inside the object on a bounded grayscale sample, with an edge penalty. The contextual background area is not rewarded. Labels are recorded per candidate and are never stabilized by rewriting history. The pinned tracker's confidence freshness is not proven; events explicitly label it `tracker_metadata_unverified`.

The global bucket bounds pixel-copy attempts even during ID churn. The queue owns copied pixels and never retains borrowed views after unmapping. For NV12/I420 the callback copies only the admitted chroma-aligned YUV region and the worker converts/trims it to the exact crop; BGR/BGRA/BGRx callbacks copy only the admitted region and defer any colour conversion to the worker. Queue bytes account for those owned bytes, including YUV alignment padding. Source mapping, worker conversion buffers and encoder scratch allocations are additional to queue_bytes; this is not a total process RSS limit. Object size/frame limits bound those allocations. The producer reuses parsed gvametaconvert objects (pixel x/y/w/h, tracker id, region_id, confidence and roi_type). It preserves the full observation JSON, including classifier fields, and does not enumerate native ROIs. The measurement probe retains native ROI inspection.

At most three candidates are retained per active selector entry. If an ID reappears after the selector TTL, it can become eligible again; the global rate and disk caps still apply. Restart the callback with the tracker so its new session namespaces reset/reused IDs. Silent tracker resets within one callback lifetime are not automatically detectable; do not reuse this callback across such resets.

Worker failure/backpressure never intentionally drops the video buffer. Disk failures pause admissions until the next maintenance pass (five seconds). Worker-written periodic `crop_stats` reports saved/published counts, rate/queue/store rejection counts, errors, tracked entries and pending bytes. These logs are an integration surface for future health reporting, not a new dashboard.

## Files, events and history boundary

One producer exclusively locks one output directory. Use a separate directory per stream. Do not share it with unrelated files or another cleanup process. Backend may read images; the producer owns their expiry.

- `generation`: persistent clear-history generation, carried in observations and events.
- `<candidate_id>.jpg`: immutable contextual JPEG.
- `<candidate_id>.pending`: durable crop-ready JSON awaiting MQTT acknowledgement.
- `<candidate_id>.json`: acknowledged event, or file-only event when MQTT is disabled.

Files are fsynced and atomically renamed before announcement. A crash between image and sidecar creation leaves an unannounced orphan; startup removes it. A crash after broker acknowledgement can cause duplicate publication after restart. MQTT consumers **must deduplicate candidate_id**. MQTT QoS 1 proves broker acceptance, not that Backend committed history. Sidecars provide reconciliation for a consumer that was offline; the future Backend must scan/import them or use an appropriate durable subscription.

Each event includes stream/session/tracker identity, candidate ID, generation, local frame sequence, arrival observation time, label/confidence, detection/crop rectangles, image dimensions, quality measurements, expiry, and relative storage_key. The observation's `crop_context` carries the same stream/session/generation/frame sequence. Stock frame_id is copied when present; standalone gvametaconvert need not supply it. No camera capture-time or media-PTS value is fabricated.

Serve storage keys through Backend HTTP, confined beneath the shared directory. The UI never reads raw filesystem paths. Crops can arrive before or after observations and must be joined by identity and frame sequence. A crop is evidence for its own observation time, not proof that the object is currently visible.

Expired images and sidecars are removed together, including undelivered events. Backend must honor expires_at and handle an expired image as unavailable, rather than promising indefinite evidence retention. Disk quotas evict oldest candidates, including still-valid evidence, to keep admitting current crops. The writer keeps ordered expiration and pending-publication indexes, recovered at startup and reconciled against disk every 60 seconds. Expiration still runs every five seconds without a full directory scan.

Clear history is deliberately coordinated with producer shutdown, not by deleting files under a live writer:

```bash
python3 scripts/dlsps/crop_candidates.py --clear-store /path/to/crops --confirm-clear
```

Stop the owning pipeline first; the lock rejects a live owner. Clear Backend history and run this command, then restart. It removes only producer-named images/sidecars and rotates generation. Consumers must adopt the new generation and reject late old-generation messages. This supplies the producer part of reset; the crop producer has no reset of its own. `close()` drains queued work on an explicit owner call or callback destruction; after five seconds a still-blocked daemon worker retains the lock until it actually finishes. Hard process termination may lose uncommitted jobs, while completed outbox files remain recoverable.

## Checks in the current running container

Takeover cleanup (2026-09-22): a worker checks ownership while idle and retires
when its lock inode is replaced, even if DLSPS retains the callback object.
Takeover and store operations share a directory lock: displaced workers cannot
write, expire or announce the replacement's files. Retirement closes the crop
lock and MQTT client, discards queued work, and prevents later frame callbacks
from refreshing the old heartbeat. Normal explicit `close()` still drains work.
This fixes the crop takeover leak; it does not claim to fix unrelated stock
DLSPS resource leaks. No custom image is required.

The frame callback queues a single latest status report; the writer performs its
logging and durable heartbeat write. The heartbeat timestamp and file mtime
reflect frame progress, not worker activity. Stale queued reports are discarded,
so idle workers cannot keep a stalled producer alive. Admission capacity is
checked before sorting, and rectangles are calculated only while crop capacity
remains. Image quality, sampling budgets and first-snapshot priority are unchanged.

`test_crops.py` covers repeated retained-callback takeovers with and without
MQTT, and takeover during frame mapping and JPEG encoding. Run the mounted
script with `docker exec handheld-multi-modal-dlsps-1 python3 /opt/handheld-crops/test_crops.py`.
Apply script updates to existing pipelines with `make demo-up`, using the
deployment's existing `ASSET_ROOT`.

From the repo root (temporary container copy, no service config changes):

```bash
docker cp scripts/dlsps handheld-multi-modal-dlsps-1:/tmp/handheld-crop-scripts
docker exec handheld-multi-modal-dlsps-1 python3 /tmp/handheld-crop-scripts/test_crops.py
docker exec handheld-multi-modal-dlsps-1 python3 /tmp/handheld-crop-scripts/test_crops.py --live
```

Use an absent destination for the first docker cp; for subsequent updates copy individual files to avoid nested directories. `--live` uses the already mounted classroom video, pinned YOLO26s model and existing MQTT broker. It runs isolated finite pipelines inside the container, requires NPU and VA access, and does not stop any DLSPS REST instance. It publishes real crop events on detections/snips with stream_id `crop_validation`; run it when that test traffic is acceptable to consumers.

Copy the printed evidence directory and logs to `docs/application/evidence/dlsps-crops/`. Inspect representative crops visually as well as decoding them. Hardware identity in evidence is the actual Lunar Lake / Intel Core Ultra 9 288V host; do not relabel it Panther Lake.

Corrected 2026-09-14 (S3): these scripts **are** enabled in Compose and in the fixed production configs — `crop_candidates.py` is in the shipped `pipeline` string of all three variants and runs on every launch — and `06_snip.sh` is implemented and PASSes (see `scripts/smoke/README.md`). The earlier claim here that they were not enabled and that `06_snip.sh` stayed blocked contradicted the rest of this same file. The live check proves actual crop/metadata/MQTT behavior; it is not a whole-app or sustained WHEP/telemetry benchmark. CPU/GPU variants and difficult-angle/occlusion footage require separate measurements.

## Recorded validation (2026-09-11)

On the accepted Lunar Lake host, the pinned image passed the deterministic checks.
A real NPU/VA run saved three probe images and independently received 13 crop MQTT
messages correlated against 161 observation frames. Images decoded with the
expected dimensions. Evidence: `docs/application/evidence/dlsps-crops/npu-va/` and
`live-check.log` (local, gitignored). The existing exporter/runtime version warning
was present; no model was changed. This is a short compatibility check, not a
sustained-load or camera-angle acceptance claim.

### Probe sampling correction

The original `npu-va/probe/` evidence sampled tracker ID 1 three times because
the probe always chose the first ROI. That demonstrated mapping, not track
coverage. The producer's separate `npu-va/crops/` directory contained different
track IDs. The probe now rotates across least-attempted tracked ROIs, with the
same image/attempt limits. Regression checks require IDs 1,2,3,1,2,3 for a stable
three-track fixture; the live classroom check requires three distinct probe IDs
and at least three producer IDs. New evidence is saved separately under
`docs/application/evidence/dlsps-crops/diversity/`; the original run is preserved.

The corrected real NPU/VA run passed with probe IDs **1, 2, 3**, producer IDs
**1 through 12**, 12 correlated MQTT events and 160 observation frames.
The three probe JPEGs were also inspected visually and show different subjects
in different parts of the source frame. These IDs still represent tracks, not
re-identified individuals. See `diversity/result.json` and `diversity-live.log`.

### Region-only worker conversion (2026-09-25)

The producer maps each admitted frame once and copies only selected crop
regions. NV12/I420 regions expand to complete 2×2 chroma blocks on the callback
thread, then the worker converts and trims back to the exact crop rectangle.
BGRA/BGRx conversion also happens in the worker; BGR crops are copied directly.
Queued pixels own their allocations after unmapping and queue bytes include YUV
alignment padding. Sampling, geometry, metadata and JPEG settings are preserved.
GPU buffer mapping/synchronization still occurs, but the producer reads
validated plane offsets/strides directly and avoids gstgva full-frame repacking.
The measurement probe retains its original full-frame conversion. Correctness
tests compare regional output against that conversion, including odd coordinates
and frame edges. No FPS gain is claimed without controlled measurement.

### Stride and failure handling (2026-09-23)

The producer uses bounds-checked read-only plane views from the raw mapped
GStreamer buffer. GstVideoMeta supplies offsets and row strides; negotiated
VideoInfo supplies them when the buffer has no video meta. NV12 and I420 crops
copy only aligned regional pixels, including correctly spaced I420 U/V rows.
The pinned gstgva has no public buffer accessor, so private buffer access is
isolated in `mapped_planes`; revalidate this adapter on a library upgrade.

A candidate larger than the store byte quota is rejected before eviction. An
unexpected writer failure disables crops, logs an error, and leaves observation
identity/context flowing. The failed worker only watches for shutdown/takeover;
it does not refresh the heartbeat. Takeover or explicit shutdown retires it and
closes its resources.
