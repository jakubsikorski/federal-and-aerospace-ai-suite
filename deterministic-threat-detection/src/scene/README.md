# SceneScape stack (use case 2)

SceneScape `hota-scene` fed by two RTSP "cameras" published over the TSN VLAN
(`rtsp://192.168.127.12:8554/Cam_x1_0`, `rtsp://192.168.127.13:8554/Cam_x2_0`).
Part of the repository: it uses `../camera-node` (streams) and `../data` (models) and the shared dashboard in `../dashboard`.
Run it on its own with `make up` in this folder, or start everything with `make up` in the repository root
(see [Get started](../../docs/user-guide/get-started.md); do the [first-time setup](../../docs/user-guide/first-time-setup.md) first).

## Services
ntpserver, broker, pgserver, web, scene-loader (one-shot appdata import), scene (controller),
analytics (publishes the `regulated` topic the UI reads), dlstreamer-pipeline-server
(pipelines `hota-cam1`/`hota-cam2`, CPU) and nginx (HTTPS on 443).

All SceneScape images use the tag set by `SCENESCAPE_VERSION` in `.env`; the pipeline server image
is set by `DLSTREAMER_PIPELINE_SERVER_IMAGE` (defaults in `.env.example`).

## Usage
```bash
make up         # start RTSP streams on machines 2/3, then secrets, certs, .env, docker compose up -d
make status
make test       # end-to-end test via the SceneScape UI and data flow (needs the stack running; this is `make test-e2e` in the repository root)
make down       # stop the stack and the remote streams;  make clean also deletes volumes
make streams-up | streams-down | streams-status   # remote streams only
make up STREAMS=0   # skip the remote streams (also for down/clean)
```
Log in at `https://<machine-1 lab address>/` (or `https://localhost/` on machine-1) as `admin`; the password is in `secrets/supass`.
The certificate is self-signed: accept the browser warning.
`make up` starts the RTSP publishers (`../camera-node/streams/`: mediamtx + ffmpeg over ssh, same start time on both
hosts; needs passwordless ssh and docker on `STREAM_HOSTS`, default `tsn-machine-2:Cam_x1_0 tsn-machine-3:Cam_x2_0`). Change
camera hosts in `.env` and `mediamtx/mediamtx.yml` if they differ.

## Notes
- The scene comes from `webserver/hota-scene-appdata.tar.bz2`. 2026.3 images no longer
  support `EXAMPLEDB`, so `scene-loader` runs `manage.py loaddata` and skips if the scene exists.
- `scene` and `analytics` load scenes only at startup, so they wait for `scene-loader`.
- The UI shows Rate and per-camera FPS only with "Show Telemetry" enabled.
- Autocalibration is not deployed (the UI logs a failing `/api/v1/autocalibration` websocket); not needed for HOTA.
- Detection frames carry the SEI `frame` number (`sscape_adapter.py`, a reduced copy of SceneScape's adapter).
