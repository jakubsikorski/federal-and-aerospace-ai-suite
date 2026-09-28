---
name: oep-text-to-speech
description: Use when resolving provenance-compliant stock OEP TTS configuration (CPU placement, D52), synthesis API, and model ownership; deployed as SpeechT5 (owner-approved).
---

# OEP Text To Speech

Pinned source/image: `edge-ai-libraries` commit
`c2cdea0ef9b137986b14e6bbeebcb80dbde4c0e9`,
`intel/text-to-speech:2026.2.0-rc3`.
See `docs/application/ai-contracts.md` for source evidence and model lineages.

**Deployed and runtime-verified** on this Lunar Lake host as SpeechT5, not
blocked. Parler-TTS provenance was fully resolved (non-PRC chain confirmed,
incl. Mistral-7B annotation teacher) but is blocked by a real packaging gap:
the pinned image's own `requirements.txt` omits the `parler-tts` package its
source dispatches to (see `config/tts/README.md` for the full record). The
owner reviewed SpeechT5's institutional-authorship provenance flag directly
(Microsoft Research Asia internship) and approved deploying it as a
Microsoft product instead.

`config/tts/config.yaml` mounts as a **direct read-only replacement** of the
stock `/app/text-to-speech/config.yaml` (`compose/handheld-multi-modal.yaml`),
not an overlay via `TEXT_TO_SPEECH_CONFIG_OVERRIDE_PATHS` — that env var is
not set in this repo's Compose.
Model configuration lives under `models.tts`; runtime `openvino|pytorch`,
`device`, model identity and variant are distinct fields.

Device: CPU (owner-directed 2026-09-28, D52; Compose `cpus: 4`, no `/dev/dri`).
On the shared GPU SpeechT5 queued behind LLM generation.
Stock persistent targets `/app/text-to-speech/{models,storage,.cache}`;
service owns model export/download. No custom TTS service or unrelated LLM.

**Container port 8011; host-mapped port is 8012** (`127.0.0.1:8012:8011` in
Compose) — use the host port from outside the container. `GET /health`;
`POST /v1/audio/speech`; `GET /v1/model-info`; `GET /v1/audio/voices`.

```json
{"input":"The camera stream is ready.","response_format":"wav"}
```

Confirmed: `/v1/model-info` reports `device:CPU` (2026-09-28; `device:GPU` at the
2026-09-10 deploy); a real
synthesis via `POST http://127.0.0.1:8012/v1/audio/speech` returned a valid
50220-byte WAV file (see `docs/application/evidence/tts/smoke.log`). A
successful WAV request returns `audio/wav` and `X-Session-ID`; JSON mode
returns `audio_base64`, sampling rate, duration and model/voice metadata.
Request `model` is metadata, not a dynamic model-selection field.

Qwen TTS remains prohibited (PRC provenance). Do not equate a Microsoft or
Hugging Face namespace with provenance approval by default — this specific
SpeechT5 approval was an explicit, recorded owner decision, not a general rule.
Startup exports models and warmup failures are nonfatal; health alone is not
synthesis evidence — the `/v1/model-info` device field and a real
synthesized WAV are the actual evidence.
`scripts/smoke/11_tts.sh` is **PASS** (needs `TTS_URL`), not a blocked gate.
