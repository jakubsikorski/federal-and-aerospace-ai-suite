# GPU variant: historical PASS (fixed alternative; current YOLO26s config not rerun)

Historical YOLO11s pipeline was smoke-tested. The current `config.json` uses YOLO26s and parses, but has not been rerun end-to-end. `config.json` pipeline name:
`handheld_gpu_detect`. Detection device `GPU`
(`pre-process-backend=va-surface-sharing`), hardware decode (`vah264dec`),
`gvatrack`, MQTT publish, JPEG-capable output.


## Run

```sh
DLSPS_CONFIG_VARIANT=gpu docker compose -f compose/handheld-multi-modal.yaml up -d --force-recreate dlsps
bash scripts/smoke/03_dlsps_gpu.sh
# restore shipped default afterward:
docker compose -f compose/handheld-multi-modal.yaml up -d --force-recreate dlsps
```

Copying a file into an unmounted `/tmp` path then `docker restart` does
**not** work — `config.json` is a direct bind mount from
`config/dlsps/<variant>/config.json`; changing the mounted source requires
recreating the container, not restarting it.

Start request shape (the Backend sends this per camera, `backend/app/pipelines.py`;
model and device are fixed in config.json; stop the Backend before starting
one by hand, or its reconciler deletes it):

```sh
curl -X POST http://127.0.0.1:8080/pipelines/user_defined_pipelines/handheld_gpu_detect \
  -H 'Content-Type: application/json' -d '{
    "source": {"uri": "rtsp://mediamtx:8554/cam1", "type": "uri"},
    "destination": {"frame": {"type": "webrtc", "peer-id": "annotated1", "overlay": true}},
    "parameters": {"model_instance": "inst_cam1_1a2b3c4d",
                   "crop-config": "{\"config\": \"/opt/handheld-crops-config/template.json\", \"stream_id\": \"cam1\"}"}
  }'
```

## Evidence

Historical YOLO11s result: ~29fps sustained via `gvafpscounter`, confirmed concurrently w/ growing MQTT message counts. Current YOLO26s GPU config needs a fresh run before claiming runtime acceptance.

## Known gotchas

- Requires `/dev/dri/renderD128` device mapping and host render group
  (GID 992 observed this host — confirm per-device).
- **VA-memory to system-memory for JPEG encode requires `vapostproc`, not
  plain `videoconvert`.** `vapostproc ! video/x-raw ! jpegenc` is the
  documented, working pattern (see
  `configs/sample_gpu_decode_and_inference/config.json` upstream and
  `docs/application/video-contracts.md`). Plain `videoconvert` fails linking
  against `video/x-raw(memory:VAMemory)` even w/ explicit caps.
- See also shared MQTT-mechanism and quantization-warning gotchas in
  `config/dlsps/cpu/README.md`.
