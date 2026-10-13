---
name: mediamtx-integration
description: Use when configuring the handheld clean RTSP input path, annotated WHIP/WHEP relay, and direct UI WHEP transport; preserves the no-Backend-video boundary.
---

# MediaMTX

Image `bluenviron/mediamtx:1.11.3`, selected from the pinned OEP DLSPS Compose
example. Schema verified against upstream v1.11.3 `mediamtx.yml`.
Local configuration `config/mediamtx/mediamtx.yml`, mounted at `/mediamtx.yml:ro`.
Compose `compose/handheld-multi-modal.yaml`. No hardware devices or media
persistence volume needed for relaying.

Prepared paths `/cam1` (clean RTSP input, `source: publisher`) and
`/annotated` (native DLSPS WHIP push, not RTSP — see
`docs/application/video-contracts.md` for the owner-approved amendment).
Host loopback: RTSP TCP 8554; ICE UDP 8189; API TCP 9997. WHEP/WHIP HTTP
8889 has no host port: containers use `mediamtx:8889`, the host browser uses
the loopback TLS proxy `https://localhost:8443/annotatedN/whep`.

**Runtime-verified** (`bash scripts/smoke/01_mediamtx.sh`, hardened
2026-09-10, live PASS with 79 real decoded `video/x-raw` WHEP frames):

```sh
docker compose -f compose/handheld-multi-modal.yaml up -d mediamtx
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

Localhost-only profile (2026-10-13, `docs/design/THREAT_MODEL.md`): never
publish these ports on external interfaces or add a non-loopback
`webrtcAdditionalHosts` entry. The only ICE server is loopback Coturn,
`turn:127.0.0.1:3478?transport=tcp` (`clientOnly`), for SSH-tunnelled browsers;
the UI reads it from the WHEP OPTIONS `Link` headers. A LAN/tablet profile needs the
authentication controls in `docs/design/SECURITY_ARCHITECTURE_REVIEW.md` first.
No Backend in video path. Do not relay DLSPS JPEG RTP and call it H.264.
See `docs/application/video-contracts.md` for source links and limitations.
