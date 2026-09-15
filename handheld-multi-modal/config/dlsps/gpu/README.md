# GPU variant: PASS (bootstrap alternative, real ~29fps)

Real, smoke-tested pipeline. `config.json` pipeline name:
`handheld_gpu_detect`. Detection device `GPU`
(`pre-process-backend=va-surface-sharing`), hardware decode (`vah264dec`),
`gvatrack`, MQTT publish, JPEG-capable output.


## Run

```sh
DLSPS_CONFIG_VARIANT=gpu docker compose -f compose/bootstrap-compose.yaml up -d --force-recreate dlsps
bash scripts/smoke/03_dlsps_gpu.sh
# restore shipped default afterward:
docker compose -f compose/bootstrap-compose.yaml up -d --force-recreate dlsps
```

Copying a file into an unmounted `/tmp` path then `docker restart` does
**not** work — `config.json` is a direct bind mount from
`config/dlsps/<variant>/config.json`; changing the mounted source requires
recreating the container, not restarting it.

Start request:

```sh
curl -X POST http://127.0.0.1:8080/pipelines/user_defined_pipelines/handheld_gpu_detect \
  -H 'Content-Type: application/json' -d '{
    "source": {"uri": "file:///home/pipeline-server/resources/videos/classroom.avi", "type": "uri"},
    "destination": {"frame": {"type": "webrtc", "peer-id": "annotated", "overlay": true}},
    "parameters": {"detection-properties": {"model": "/home/pipeline-server/yolo_models/yolo11s/INT8/yolo11s.xml", "device": "GPU"}}
  }'
```

## Evidence

Real ~29fps sustained via `gvafpscounter`, confirmed concurrently w/
growing MQTT message counts.

## Known gotchas

- Requires `/dev/dri/renderD128` device mapping and host render group
  (GID 992 observed this host — confirm per-device).
- **VA-memory to system-memory for JPEG encode requires `vapostproc`, not
  plain `videoconvert`.** `vapostproc ! video/x-raw ! jpegenc` is the
  documented, working pattern (see
  `configs/sample_gpu_decode_and_inference/config.json` upstream and
  `docs/bootstrap/video-contracts.md`). Plain `videoconvert` fails linking
  against `video/x-raw(memory:VAMemory)` even w/ explicit caps.
- See also shared MQTT-mechanism and quantization-warning gotchas in
  `config/dlsps/cpu/README.md`.
