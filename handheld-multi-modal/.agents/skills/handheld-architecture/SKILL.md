---
name: handheld-architecture
description: Use when planning handheld integration boundaries or reviewing an application handoff; enforces the owner-defined video, observation, evidence, speech, and metrics contracts.
---

# Handheld architecture

These are fixed owner requirements, not claims that components are running.
Pinned baseline and resolved source commits: `MANIFEST.yaml`.
Read `AGENTS.md` and `STATUS.md` first.

```text
MediaMTX -> DLSPS clean RTSP
DLSPS -> MediaMTX annotated WebRTC (WHIP push; owner-approved amendment,
    supersedes the original RTSP-for-annotated draft — see
    docs/application/video-contracts.md)
MediaMTX -> UI WebRTC/WHEP

DLSPS -> MQTT -> Backend observations
DLSPS -> shared snips -> Backend static evidence images

Metrics Manager -> Backend -> UI
Backend -> UI HTTP/SSE/WebSocket
Coturn <-> MediaMTX WebRTC only (loopback listener/relay/peer)
Browser on the device or via SSH tunnel: every host port binds to 127.0.0.1

Backend does NOT sit in the video path.
Backend does NOT reconfigure DLSPS at runtime.
UI does NOT access SQLite directly.
UI does NOT access snip filesystem paths directly.
```

The browser captures microphone audio; Backend validates and forwards bounded
audio; Audio Analyzer owns transcription. Backend owns pipeline REST lifecycle
(start/stop fixed pipelines per camera), alert rules and speech arbitration.
Snips must be cropped inside the DLSPS pipeline, never in Backend.
CPU/GPU/NPU pipelines are predefined, NPU is the intended default. Do not
introduce a dynamic pipeline-editing API or expose the stock Chat Q&A UI.

Use verified component contracts for actual APIs, health checks, devices,
volumes, and commands. Null runtime fields are not values to invent.
Owner accepts the current Lunar Lake host as the execution target. Label
actual hardware truthfully; its name is not an application blocker. Stock `/chat`
is accepted for general conversation with retrieval allowed and a suitable
prompt. Do not erase mission documents to implement general chat.
