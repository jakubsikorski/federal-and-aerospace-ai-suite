---
name: handheld-camera-connection
description: Use when adding, publishing to, removing or troubleshooting a handheld camera (publisher or network RTSP), or when live video does not show in the browser locally or through the SSH tunnel (WHEP/TURN).
---

# Camera connection

The Backend owns cameras (`backend/app/cameras.py`). Each camera `camN` gets a
MediaMTX input path `camN`, one DLSPS pipeline started by the Backend, and an
annotated WebRTC output `annotatedN` that the browser plays over WHEP. The
Backend is never in the video path; do not start DLSPS pipelines by hand while
it runs (it reaps unowned instances).

## Add a camera

Operators use **Cameras > + Add camera** in the UI
(`docs/OPERATOR_GUIDE.md`). The same API, through the TLS proxy:

```sh
export NO_PROXY='*' no_proxy='*'
API=https://127.0.0.1:8443/api
CA=--cacert config/tls/server.crt
# Publisher on this device: an app/encoder pushes RTSP to the returned address.
curl -sS $CA -X POST $API/cameras -H 'content-type: application/json' \
  -d '{"display_name":"Gate","source":{"kind":"publish"}}'
# Network camera: MediaMTX pulls over RTSP/TCP.
curl -sS $CA -X POST $API/cameras -H 'content-type: application/json' \
  -d '{"display_name":"Yard","source":{"kind":"pull","url":"rtsp://192.168.1.20:554/stream1"}}'
curl -sS $CA $API/cameras                                  # list, feed state
curl -sS $CA -X PATCH $API/cameras/cam3 -H 'content-type: application/json' -d '{"enabled":false}'
curl -sS $CA -X DELETE "$API/cameras/cam3?confirm=cam3"
```

- **Publisher**: send H.264 RTSP to `rtsp://127.0.0.1:8554/camN` from this
  device only, e.g.
  `ffmpeg -re -stream_loop -1 -i video.mp4 -an -c:v libx264 -preset veryfast -tune zerolatency -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8554/camN`.
  RealSense: `RTSP_URL=rtsp://127.0.0.1:8554/camN examples/realsense-rtsp.sh`
  (see its header for device selection and VA-API vs CPU encoding).
- **Network camera**: `rtsp://` or `rtsps://` without credentials. IPs must be
  inside `camera_source_allow` in `config/backend/backend.yaml` (default the
  RFC 1918 ranges); hostnames must be listed exactly. Loopback, link-local,
  multicast and unspecified addresses are always refused (403
  `source_not_allowed`). Cameras that need a password are configured by an
  administrator as a MediaMTX path, never stored by the Backend.
- Demo cameras: `make demo-up CAMERAS=1..2` (pinned FFmpeg loops in
  `compose/cameras.yaml`) appear with `origin: config`; console-added ones with
  `origin: operator`.

## Check a camera

Feed states in `GET /api/cameras` and the UI: `starting` (within
`pipeline_start_timeout_seconds`, default 180), `no_signal_yet` ("No frames
received"), signal lost, `off`. A new publisher is usually live in ~10 s and
`annotatedN` ready in ~15 s. Walk the chain in order:

```sh
curl -s http://127.0.0.1:9997/v3/paths/list | python3 -m json.tool | grep -E '"name"|"ready"'
curl -s http://127.0.0.1:8080/pipelines/status | python3 -m json.tool | grep -E '"state"|"id"'
docker logs --since 5m handheld-multi-modal-backend 2>&1 | grep -i camera
docker logs --since 5m handheld-multi-modal-mediamtx-1 2>&1 | tail -20
```

1. `camN` not ready: nothing is publishing, or MediaMTX cannot reach/pull the
   network camera (check its address, codec H.264, RTSP over TCP).
2. `camN` ready but no RUNNING DLSPS instance: see Backend logs; `make check`.
3. `annotatedN` ready but the tile is black: a browser/WebRTC problem, below.

## View video in the browser

Local browser: `https://localhost:8443/`; WebRTC media uses
`127.0.0.1:8189/udp` directly.

Remote browser: SSH tunnel with **both** forwards and the same local ports:

```sh
ssh -N -L 8443:127.0.0.1:8443 -L 3478:127.0.0.1:3478 <user>@<device-address>
```

SSH carries no UDP, so remote video is relayed over TURN/TCP through
loopback-only Coturn. MediaMTX advertises `turn:127.0.0.1:3478?transport=tcp`
in WHEP `OPTIONS` `Link` headers; the UI uses them.

Symptom: page loads, tiles stay black, MediaMTX logs
`closed: deadline exceeded while waiting connection` and nothing connects to
3478 on the device (`ss -tn | grep :3478`). The device side is fine; check
the laptop:

- `ssh` printed `bind ... Address already in use` for 3478;
- `nc -vz 127.0.0.1 3478` (Windows `Test-NetConnection 127.0.0.1 -Port 3478`)
  fails;
- `chrome://webrtc-internals` / `about:webrtc` shows no `relay` candidate
  (managed browser WebRTC policies can block it).

Prove the device relay path with the relay-only diagnostic (needs `aiortc`;
the Backend image has no pip, so use a stock Python container):

```sh
docker run --rm --network host -e https_proxy -e http_proxy \
  -e no_proxy=localhost,127.0.0.1 -v "$PWD:/srv:ro" -w /srv python:3.12-slim sh -c \
  'pip install -q -r scripts/smoke/requirements.txt && python3 scripts/smoke/whep_client.py \
   --relay-only --cafile config/tls/server.crt https://localhost:8443/annotated1/whep'
```

`PASS: WHEP negotiated and decoded three video frames over TURN relay` means
the server side works. `aiortc` decodes H.264 only (NPU/GPU variants); the CPU
variant sends VP9.

## Boundaries

- Localhost-only profile: never publish 8443, 8554, 8189, 9997 or 3478 on
  non-loopback interfaces, add `webrtcAdditionalHosts`, or share the tunnel
  (`ssh -g`, `GatewayPorts`). Fix remote viewing on the client side.
- Do not route video through the Backend, edit DLSPS pipelines at runtime, or
  store camera credentials in the Backend database.
- Video pipeline: `scripts/dlsps/README.md`; MediaMTX details:
  `mediamtx-integration`; DLSPS variants: `handheld-dlsps-contract`.
