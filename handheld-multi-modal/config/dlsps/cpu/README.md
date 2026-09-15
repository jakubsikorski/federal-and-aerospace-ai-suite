# CPU variant: PASS (bootstrap alternative, real ~23-30fps)

Real, smoke-tested pipeline. `config.json` pipeline name:
`handheld_cpu_detect`. Detection device `CPU`, no VA/hardware decode
(`decodebin`/`videoconvert`), `gvatrack`, MQTT publish, JPEG-capable output.


## Run

```sh
DLSPS_CONFIG_VARIANT=cpu docker compose -f compose/bootstrap-compose.yaml up -d --force-recreate dlsps
bash scripts/smoke/02_dlsps_cpu.sh
# restore shipped default afterward:
docker compose -f compose/bootstrap-compose.yaml up -d --force-recreate dlsps
```

Copying a file into an unmounted `/tmp` path then `docker restart` does
**not** work: the container's `config.json` is a direct bind mount from
`config/dlsps/<variant>/config.json` on the host (see
`compose/bootstrap-compose.yaml`), and a plain restart re-reads the same
mount, not a new one. Changing the mounted source requires recreating the
container (`--force-recreate`, or removing and re-`up`-ing it), not restarting it.

Start request (also see `scripts/smoke/_dlsps_lib.sh`):

```sh
curl -X POST http://127.0.0.1:8080/pipelines/user_defined_pipelines/handheld_cpu_detect \
  -H 'Content-Type: application/json' -d '{
    "source": {"uri": "file:///home/pipeline-server/resources/videos/classroom.avi", "type": "uri"},
    "destination": {"frame": {"type": "webrtc", "peer-id": "annotated", "overlay": true}},
    "parameters": {"detection-properties": {"model": "/home/pipeline-server/yolo_models/yolo11s/INT8/yolo11s.xml", "device": "CPU"}}
  }'
```

## Evidence

Real ~23fps sustained via `gvafpscounter` in container logs during 15s+ run.
Slower than GPU/NPU as expected for software-only decode+inference.

## Known gotchas (shared across all three variants)

- MQTT: use documented `appsink name=destination` + top-level
  `mqtt_publisher` config block. Raw `gvametapublish` element + REST
  `destination.metadata.host` field is broken in this rc3 image (see
  `docs/bootstrap/video-contracts.md`).
- Cosmetic-only warning every run: `Model quantization runtime (2025.0.0)
  != current runtime (2026.2.0)`. No observed functional effect.
- WebRTC (`destination.frame`) requested dynamically via REST at
  pipeline start, not baked into `config.json`.
