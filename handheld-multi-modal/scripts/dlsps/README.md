<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# DL Streamer Pipeline Server Crop Callbacks

These scripts are `gvapython` callbacks for the stock DL Streamer Pipeline Server image. They
add no GStreamer plugin, model or service.

- `crop_candidates.py` — the producer. It runs in every shipped pipeline, saves contextual
  JPEG crops of tracked objects, and publishes crop events on the `detections/snips` MQTT
  topic.
- `crop_probe.py` — a measurement callback for crop timings. It is not used at runtime.
- `test_crops.py` — geometry, persistence and lifecycle checks for both callbacks.

## Deployment

Compose mounts this directory read-only at `/opt/handheld-crops`, the templates from
`config/dlsps/crops` at `/opt/handheld-crops-config`, and a writable output directory from
`data/dlsps/crops` at `/opt/handheld-crops-output`. Place the callback after `gvametaconvert`
and before annotation or encoding:

```text
... ! gvatrack tracking-type=zero-term-imageless config=tracking_per_class=false
    ! gvametaconvert add-empty-results=true
    ! gvapython name=cropcandidates module=/opt/handheld-crops/crop_candidates.py class=CropCandidates
    ! ...
```

> [!IMPORTANT]
> Do not put `kwarg="{...}"` in the `pipeline` string of `config.json`. The DL Streamer
> Pipeline Server template rendering corrupts the embedded JSON. Map a pipeline parameter to
> the element's `kwarg` property instead:

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

The Backend passes the value when it starts a pipeline (`backend/app/pipelines.py`):

```json
"parameters": {
    "model_instance": "inst_cam1_1a2b3c4d",
    "crop-config": "{\"config\": \"/opt/handheld-crops-config/template.json\", \"stream_id\": \"cam1\"}"
}
```

When you modify the scripts, apply the changes to running pipelines with `make demo-up`.

## Configuration

To print the full configuration with defaults, run:

```bash
python3 scripts/dlsps/crop_candidates.py --print-defaults
```

Unknown keys, out-of-range values and unsafe stream IDs fail at startup. Leave `mqtt_host`
empty for file-only output, or set it to `mqtt` for the Compose broker. Do not put credentials
in this file. The defaults that control crop rate and storage are:

| Setting | Default | Behavior |
|---|---:|---|
| `rate` / `burst` | 2 per second / 3 | Global crop budget across all tracks |
| `candidates_per_track` / `improvement` | 3 / 0.15 | First crop plus up to two crops that score at least 15% better |
| `max_dimension` / `jpeg_quality` | 640 / 85 | Output size limit and JPEG quality |
| `store_bytes` / `store_candidates` | 256 MiB / 1500 | Storage cap; the oldest crops are removed first |
| `retention_seconds` | 3600 | Crops and their events expire after one hour |

## Output and Events

One producer locks one output directory, so use a separate directory for each stream. The
directory contains:

- `generation` — the clear-history generation, included in observations and events.
- `<candidate_id>.jpg` — the crop image.
- `<candidate_id>.pending` — an event waiting for MQTT acknowledgement.
- `<candidate_id>.json` — an acknowledged event, or a file-only event when MQTT is disabled.

Each event carries the stream, session, track and candidate IDs, the generation, the frame
sequence, label and confidence, detection and crop rectangles, quality scores, `expires_at`
and a relative `storage_key`. The observation JSON gains a matching `crop_context`.

Consumers must:

- Deduplicate events by `candidate_id`; an event can be published again after a restart.
- Join crops to observations by stream, session, generation and frame sequence, because
  either can arrive first.
- Treat an image past `expires_at` as unavailable.
- Serve images through the Backend by `storage_key`, never by raw file path.

To clear crop history, stop the owning pipeline, clear the Backend history, and run:

```bash
python3 scripts/dlsps/crop_candidates.py --clear-store /path/to/crops --confirm-clear
```

The command rotates the generation. Consumers must reject messages from the old generation.

## Troubleshooting

- **`Resource temporarily unavailable` from `Store.__init__`** — a failed producer still holds
  the output directory lock. Restart the `dlsps` container, remove any stale `.lock` file, and
  start the pipeline again.
- **No crops for a stream** — check the DL Streamer Pipeline Server logs for callback errors
  and periodic `crop_stats` lines. Writer failures disable crops, but observations continue.
  The `scripts/smoke/06_snip.sh` script checks crops end to end.

## Tests

Run the checks inside the running container:

```bash
docker exec handheld-multi-modal-dlsps-1 python3 /opt/handheld-crops/test_crops.py
```

The `--live` option runs a short pipeline on the NPU with the demo video and publishes real
events on `detections/snips` with the `crop_validation` stream ID.
