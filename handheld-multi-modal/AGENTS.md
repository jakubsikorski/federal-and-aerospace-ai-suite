# Bootstrap contract

Repo preps stock Open Edge Platform integrations, not final handheld app.
Read `BOOTSTRAP_STATUS.md`, `BOOTSTRAP_MANIFEST.yaml`, `COMPONENTS.md`,
`MODEL_PROVENANCE.md` before deployment.

## Release and evidence

- Baseline: `release-2026.2.0`. Pin source commits + exact container tags.
  No floating image tags, no silent model swap.
- Manifest `requirements` = owner asks, not resolved facts. Null = unresolved.
  Source-verified interface ≠ smoke pass.
- Host: **Lunar Lake**, Intel Core Ultra 9 288V. 2026-09-09 owner accepted
  this machine as execution/acceptance target. Hardware naming not a blocker.
  Record actual identity in evidence; passing tests here satisfy acceptance —
  don't mislabel silicon or claim separate Panther Lake measurements.
- Scaffold-only phase superseded for authorized integration work. Public
  registries/models may work without creds; never persist tokens or assume
  auth required before trying public access.
- Save component logs + actual outputs under `docs/bootstrap/evidence/`.
  JSON parsing, port open, device node exists, or env var says `GPU`/`NPU`
  ≠ smoke pass.
- Official skills copied under `.agents/skills/` w/ `skills-lock.json`.
  Generic examples there don't override these constraints. No default model
  downloads, floating tags, or alt architectures from them.

## Models and stock components

- No PRC-trained model families/derivatives. Prohibited: Qwen, DeepSeek, GLM,
  Yi, InternLM, MiniCPM. Audit embedding/reranking defaults too. Uncertain
  provenance blocks use.
- Chat Q&A Core (required): OpenVINO, GPU, `Intel/neural-chat-7b-v3-3`
  (owner-directed replacement for `microsoft/Phi-3.5-mini-instruct`,
  2026-09-14; fine-tuned from mistralai/Mistral-7B-v0.1, non-PRC lineage —
  see `MODEL_PROVENANCE.md`);
  no stock UI in final stack. Owner accepts stock `/chat` + doc endpoints for
  general answers. Use supported `PROMPT_TEMPLATE` answering general Qs from
  model knowledge, mission context only when relevant. Retrieval may still
  run — not a retrieval bypass; don't delete mission docs for general Qs or
  claim stateless `/chat` has conversation memory. Use unmodified stock Chat
  Q&A backend's own model download/conversion/loading path — no separate Phi
  exporter, no config rewrite, no one-off inference script replacing backend.
- Audio Analyzer (required): OpenVINO, `whisper-medium`, GPU; sentiment +
  diarization disabled. Backend captures mic audio; service transcribes.
- OEP TTS (required): stock-supported, provenance-approved model, GPU.
- Detector: YOLO26s OpenVINO IR, INT8 preferred, NPU default, CPU `gvatrack`,
  Intel HW decode/encode (owner-directed 2026-09-24 replacement for YOLO11s).
  CPU/GPU = fixed bootstrap alternatives, not dynamically edited pipelines.
  Never substitute nano.
- Alerts: rule/action dispatch, no ADK/LLM in critical path.
- Resolve actual model owners/caches; don't force all downloads through
  Model Download. Never persist access tokens in configs/logs/Git.
- Report incompatibilities incl. missing stock interfaces. No forking Chat
  Q&A or hiding gaps behind custom model servers.

## Architectural boundaries

```text
MediaMTX -> DLSPS clean RTSP
DLSPS -> MediaMTX annotated WebRTC (WHIP push; owner-approved amendment,
    supersedes the original RTSP-for-annotated draft — see
    docs/bootstrap/video-contracts.md)
MediaMTX -> UI WebRTC/WHEP
DLSPS -> MQTT -> Backend observations
DLSPS -> shared snips -> Backend static evidence images
Metrics Manager -> Backend -> UI
Backend -> UI HTTP/SSE/WebSocket
Coturn <-> MediaMTX WebRTC only
```

Backend not in video path, doesn't crop images, doesn't reconfigure DLSPS
at runtime. UI never touches SQLite or snip filesystem paths directly.
No DLSPS-to-Coturn path. No final Backend, UI, DB schema, business rules,
speech arbitration, or telemetry aggregator in this bootstrap.

## Commands and integration work

Run commands from repo root. `scripts/smoke/README.md` splits implemented
smoke tests from blocked entry points; blocked entry point exits 2, never
success. Run one component: `bash scripts/smoke/01_mediamtx.sh`. Don't
confuse scaffolding/schema validation with service execution.

Reinstall official copies via pinned command in
`docs/bootstrap/release-and-host.md`. Don't edit upstream skill copies;
repo-specific overrides belong in local skills + this contract.

## Application operations and UX (owner direction, 2026-09-21)

Prioritize repeatability across devices. Keep application setup, start, stop,
restart, and readiness checks in the root Makefile; do not add custom scripts
just to launch pipelines. Backend owns pipeline REST lifecycle. Use
`make demo-up`, `demo-stop`, `demo-restart`, and `demo-down`; document asset
prerequisites and detect device groups/network addresses instead of hardcoding
this host. Stop DLSPS with the application to release stuck instances.
Keep model/device UI concise: status, model, device, host; distinguish configured
placement from service-reported execution without verbose technical diagnostics.
