# CPU variant: historical PASS (fixed alternative; current YOLO26s config not rerun)

Historical YOLO11s pipeline was smoke-tested. The current `config.json` uses YOLO26s and parses, but has not been rerun end-to-end. `config.json` pipeline name:
`handheld_cpu_detect`. Detection device `CPU`, no VA/hardware decode
(`decodebin`/`videoconvert`), `gvatrack`, MQTT publish, JPEG-capable output.


## Run

```sh
DLSPS_CONFIG_VARIANT=cpu docker compose -f compose/handheld-multi-modal.yaml up -d --force-recreate dlsps
bash scripts/smoke/02_dlsps_cpu.sh
# restore shipped default afterward:
docker compose -f compose/handheld-multi-modal.yaml up -d --force-recreate dlsps
```

Copying a file into an unmounted `/tmp` path then `docker restart` does
**not** work: the container's `config.json` is a direct bind mount from
`config/dlsps/<variant>/config.json` on the host (see
`compose/handheld-multi-modal.yaml`), and a plain restart re-reads the same
mount, not a new one. Changing the mounted source requires recreating the
container (`--force-recreate`, or removing and re-`up`-ing it), not restarting it.

Start request shape (the Backend sends this per camera, `backend/app/pipelines.py`;
model and device are fixed in config.json; stop the Backend before starting
one by hand, or its reconciler deletes it):

```sh
curl -X POST http://127.0.0.1:8080/pipelines/user_defined_pipelines/handheld_cpu_detect \
  -H 'Content-Type: application/json' -d '{
    "source": {"uri": "rtsp://mediamtx:8554/cam1", "type": "uri"},
    "destination": {"frame": {"type": "webrtc", "peer-id": "annotated1", "overlay": true}},
    "parameters": {"model_instance": "inst_cam1_1a2b3c4d",
                   "crop-config": "{\"config\": \"/opt/handheld-crops-config/template.json\", \"stream_id\": \"cam1\"}"}
  }'
```

## Evidence

Historical YOLO11s result: ~23fps sustained via `gvafpscounter` in container logs during 15s+ run. Current YOLO26s CPU config needs a fresh run before claiming runtime acceptance.

## Known gotchas (shared across all three variants)

- MQTT: use documented `appsink name=destination` + top-level
  `mqtt_publisher` config block. Raw `gvametapublish` element + REST
  `destination.metadata.host` field is broken in this 2026.2.0 image.
- Historical YOLO11s only: `Model quantization runtime (2025.0.0)
  != current runtime (2026.2.0)` was cosmetic. No equivalent YOLO26s warning is currently recorded.
- WebRTC (`destination.frame`) requested dynamically via REST at
  pipeline start, not baked into `config.json`.
