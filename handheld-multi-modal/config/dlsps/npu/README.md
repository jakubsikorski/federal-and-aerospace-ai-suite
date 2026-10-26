# NPU default variant (shipped/default, ~30fps on NPU)

**This is the shipped/default variant per application spec.** Real,
smoke-tested pipeline. `config.json` pipeline name: `handheld_npu_detect`.
Detection device `NPU` (`pre-process-backend=va`), hardware decode
(`vah264dec`), `gvatrack`, MQTT publish, and raw VA-memory output feeding the
stock WebRTC frame destination.

**Snip (per-object crop) generation is wired into this pipeline** via a
`gvapython` callback (`scripts/dlsps/crop_candidates.py`), validated
end-to-end at ~30fps producing matched crop+metadata pairs; see
"Known gotchas" below and `scripts/dlsps/README.md`.

## Run

```sh
docker compose -f compose/handheld-multi-modal.yaml up -d   # mounts this config by default
bash scripts/smoke/04_dlsps_npu.sh
bash scripts/smoke/05_mqtt.sh
```

Start request. One pipeline instance per camera; the Backend
(`backend/app/pipelines.py`) issues exactly this call, so start it by hand only
when debugging without the Backend:

```sh
curl -X POST http://127.0.0.1:8080/pipelines/user_defined_pipelines/handheld_npu_detect \
  -H 'Content-Type: application/json' -d '{
    "source": {"uri": "rtsp://mediamtx:8554/cam1", "type": "uri"},
    "destination": {
      "frame": {
        "type": "webrtc",
        "peer-id": "annotated1",
        "overlay": true,
        "cache-length": 1,
        "sync-with-source": false,
        "sync-with-destination": false
      }
    },
    "parameters": {
      "crop-config": "{\"config\": \"/opt/handheld-crops-config/template.json\", \"stream_id\": \"cam1\"}",
      "model_instance": "inst_cam1_0a1b2c3d"
    }
  }'
```

Annotated video uses DLSPS's documented frame destination. `peer-id` becomes
the MediaMTX path; `WEBRTC_SIGNALING_SERVER=http://mediamtx:8889` makes DLSPS's
internal WebRTC manager publish to `/annotated1/whip`. The pipeline string does
not contain `whipclientsink`. MQTT remains independent and always enabled by
the static `mqtt_publisher` block. Both parameters are mandatory:

- `crop-config` must be **JSON text**, not a path. The `gvapython` element parses
  its `kwarg` as JSON, so a bare path raises a decode error. Passing a real JSON
  object instead of a string fails DLSPS's own `Invalid Parameters` check.
- `model_instance` must be **unique per start**, not per camera. See "Known
  gotchas".

MQTT publish uses static `mqtt_publisher` plus `MQTT_HOST`/`MQTT_PORT`. WebRTC
uses the REST `destination.frame` request. Both consume raw VA frames from
`appsink name=destination`; neither replaces the other.

## Expected behavior

The YOLO26s NPU path runs with the NPU device requested and no
NPU compile/plugin error in logs (IR provenance in
`MODEL_PROVENANCE.md`). Confirmed running simultaneously w/ real MQTT
publish (`detections/live`, ~30 msg/s), no fps degradation on either leg.

## Known gotchas

- Requires `/dev/accel/accel0` (NPU) and `/dev/dri/renderD128` (decode)
  device mappings, render GID (e.g. 992), and
  `--tmpfs /var/cache/pipeline_root:uid=1999,gid=1999`.
- **WHEP codec is H264 here, not VP9.** Raw VAMemory reaches the stock WebRTC
  manager, which selects its `vah264enc ! h264parse ! whipclientsink` path.
  The CPU variant still produces VP9, so a client must offer both.
- **`model-instance-id` must be unique per pipeline start.** Sharing one id
  across cameras makes OpenVINO share a single inference instance: deleting any
  one pipeline disposes it and **aborts every other camera**. A per-*camera*
  constant id is also not enough — a leaked predecessor still holds the id and
  the replacement then hangs in preroll (`QUEUED`, 0 fps) with no error logged.
  The Backend generates `inst_<camera>_<random>` on every start.
- **`gvapython` has no teardown hook.** The plugin exposes only `process_frame`
  (confirmed with `strings` on `libgvapython.so`), so an aborted pipeline never
  releases the crop store's `flock`. `crop_candidates.py` works around this
  itself: the producer heartbeats `status.json` each report tick, and a store
  whose heartbeat is older than `takeover_seconds` is taken over by unlinking
  the lock file and reopening it.
- **Deleting a pipeline leaks its MediaMTX sessions.** The zombie keeps
  publishing to `annotatedN` and keeps its RTSP reader on `camN` (5 stale
  decoding readers observed on one camera). The Backend kicks both through the
  MediaMTX control API before each start; it never kicks the source publisher.
- **Only `DELETE /pipelines/{instance_id}` works.** The documented
  `DELETE /pipelines/{name}/{version}/{instance_id}` form returns a hardcoded
  400 `NOT_IMPLEMENTED` in this image.
- **DLSPS RSS grows with leaked pipelines** (1.2 GiB observed after repeated
  restart cycles). Known ceiling, not yet addressed.
- Same VA-memory→JPEG (`vapostproc`) and MQTT-mechanism gotchas as GPU/CPU
  variants apply here too.
- **No stock per-object crop destination exists in DLSPS.** `destination.frame`
  supports `rtsp`, `webrtc`, `s3_write`, `application` — all whole-frame only.
  An earlier iteration used `s3_write` to self-hosted
  SeaweedFS S3 store as whole-frame "frame-saver"; **this was rejected
  and fully backed out** (no SeaweedFS container, no
  `s3_write` requested anywhere in this repo). Chosen
  approach: custom `gvapython` element inserted into pipeline (no
  `ADDITIONAL_GST_PLUGIN_PATH` needed — `gvapython` is stock) that reads
  `GstVideoRegionOfInterestMeta` per tracked object and crops before any
  encode step. **Implemented and wired in**: `scripts/dlsps/crop_candidates.py`
  runs as a `gvapython` element in this config (see the pipeline string
  above), its config path passed via the `crop-config`→`kwarg` pipeline
  `parameter`, not embedded in the pipeline string — see
  `scripts/dlsps/README.md` and `scripts/smoke/06_snip.sh` (which checks this
  shipped config).
