---
name: mediamtx-integration
description: Use when configuring the handheld clean RTSP input path, annotated WHIP/WHEP relay, and direct UI WHEP transport; preserves the no-Backend-video boundary.
---

# MediaMTX

Image `bluenviron/mediamtx:1.11.3`, selected from the pinned OEP DLSPS Compose
example. Schema verified against upstream v1.11.3 `mediamtx.yml`.
Local configuration `config/mediamtx/mediamtx.yml`, mounted at `/mediamtx.yml:ro`.
Compose `compose/bootstrap-compose.yaml`. No hardware devices or media
persistence volume needed for relaying.

Prepared paths `/cam1` (clean RTSP input, `source: publisher`) and
`/annotated` (native DLSPS WHIP push, not RTSP — see
`docs/bootstrap/video-contracts.md` for the owner-approved amendment).
Host RTSP TCP 8554; WHEP HTTP 8889; ICE UDP 8189; API TCP 9997.
Containers use `mediamtx:8554`; host uses loopback.

**Runtime-verified** (`bash scripts/smoke/01_mediamtx.sh`, hardened
2026-09-10, live PASS with 79 real decoded `video/x-raw` WHEP frames — see
`docs/bootstrap/IMPROVEMENT_PLAN.md`):

```sh
docker compose -f compose/bootstrap-compose.yaml up -d mediamtx
curl --fail --max-time 10 http://127.0.0.1:9997/v3/paths/list
bash scripts/smoke/01_mediamtx.sh
```

The gate requires both a live `/cam1` RTSP publisher (a build agent must
start one, e.g. an ffmpeg loop of a demo file into
`rtsp://127.0.0.1:8554/cam1` — no publisher runs by default) and a running
DLSPS pipeline pushing to `/annotated`; the script starts/stops its own DLSPS
pipeline instance but does not start an RTSP publisher for you.
SDP offer goes to `/annotated/whep` with `Content-Type: application/sdp`;
expect 201 plus SDP answer and Location, then DELETE the session when done.
Server-side path-ready state (`GET /v3/paths/list`) is supplementary
confirmation only — the load-bearing check is a real client-side decode
(`decodebin` producing `video/x-raw` buffers), not just a log line.

Loopback-only unauthenticated bootstrap configuration is not a tablet/LAN
deployment. Do not publish these ports on external interfaces without
authentication/reachability work. No Backend in video path. Coturn, if needed,
assists MediaMTX WebRTC only. Do not relay DLSPS JPEG RTP and call it H.264.
See `docs/bootstrap/video-contracts.md` for source links and limitations.
