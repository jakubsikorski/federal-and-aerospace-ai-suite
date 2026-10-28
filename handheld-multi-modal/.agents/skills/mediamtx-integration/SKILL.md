---
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
name: mediamtx-integration
description: Use when configuring the handheld MediaMTX relay (camera RTSP input, DLSPS WHIP push of annotated video, browser WHEP playback) or its loopback-only network profile.
---

# MediaMTX

Image `bluenviron/mediamtx:1.21.3`, configuration
`config/mediamtx/mediamtx.yml` mounted at `/mediamtx.yml:ro`, Compose service
`mediamtx`. No devices or media volumes are needed.

## Paths

One pair per camera, matched by regex so no per-camera configuration is
needed:

- `~^cam[0-9]+$`: clean camera input over RTSP (`source: publisher`).
- `~^annotated[0-9]+$`: annotated video that DLSPS pushes over WebRTC (WHIP),
  not RTSP.

## Ports

All host ports bind to `127.0.0.1`: RTSP TCP `8554`, ICE UDP `8189`, API TCP
`9997`. WHEP/WHIP HTTP `8889` is not published. Containers use
`mediamtx:8889`; the browser uses the TLS proxy at
`https://localhost:8443/annotatedN/whep`.

The only ICE server is loopback Coturn, `turn:127.0.0.1:3478?transport=tcp`,
for browsers connected through an SSH tunnel. The UI reads it from the
`Link` headers of the WHEP `OPTIONS` response.

## Usage

```sh
curl -fsS http://127.0.0.1:9997/v3/paths/list
```

- `make demo-up` starts demo publishers (`compose/cameras.yaml`, `CAMERAS=N`)
  into `rtsp://mediamtx:8554/camN`. With `make up`, operators add cameras.
- WHEP: POST the SDP offer to `/annotatedN/whep` with
  `Content-Type: application/sdp`; expect `201`, an SDP answer and a
  `Location` header; DELETE that location when done.
- A path showing `ready` is not proof of playback. Prove it by decoding real
  frames on the client with `scripts/whep_client.py` (see the
  `handheld-camera-connection` skill).

## Boundaries

- Never publish these ports on external interfaces or add a non-loopback
  `webrtcAdditionalHosts` entry. A LAN profile needs authentication and
  access controls first.
- The Backend is never in the video path.
- DLSPS RTSP output is JPEG over RTP. Do not relay it as if it were H.264.
