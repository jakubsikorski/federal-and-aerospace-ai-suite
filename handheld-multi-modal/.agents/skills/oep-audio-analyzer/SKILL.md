---
name: oep-audio-analyzer
description: Use when configuring the required stock Whisper-medium OpenVINO GPU transcription path or its Backend upload API.
---

# OEP Audio Analyzer

Pinned source/image: `edge-ai-libraries` commit
`c2cdea0ef9b137986b14e6bbeebcb80dbde4c0e9`,
`intel/audio-analyzer:2026.2.0-rc3`. **Deployed and runtime-verified** on
this Lunar Lake host (see `STATUS.md`,
`docs/application/evidence/audio-analyzer/smoke.log`), not source-only.
Full API, model ownership and source references: `docs/application/ai-contracts.md`.

Repository config: `config/audio-analyzer/config.yaml` is mounted as a
**direct read-only replacement** of the stock
`/app/audio_analyzer/config.yaml` (`compose/handheld-multi-modal.yaml`), not an
overlay via `AUDIO_ANALYZER_CONFIG_OVERRIDE_PATHS` — that env var is not set
in this repo's Compose.

Required values: `app.use_ov_genai=true`,
`models.asr.provider=openvino`, `models.asr.name=whisper-medium`,
`models.asr.device=GPU`, `models.asr.diarization=false`,
`sentiment.enabled=false`. Denoising is disabled too.

GPU access `/dev/dri` plus actual render/video groups (`video`, GID 992
observed on this host). No microphone or `/dev/snd` mapping: Backend owns
capture.
Stock persistent targets under `/app/audio_analyzer`: `models`, `chunks`,
`storage`, `.cache`; HF cache `.cache/huggingface`.
Service owns export of `openai/whisper-medium` to
`models/openvino/whisper-medium` (confirmed via a real first-run
`optimum-cli export openvino` this session; required a real corporate
`http(s)_proxy` for the outbound HF download). Immutable model revision is
not yet pinned.

**Container port 8010; host-mapped port is 8011** (`127.0.0.1:8011:8010` in
Compose) — use the host port from outside the container:

```sh
curl --fail --max-time 10 http://127.0.0.1:8011/health
curl --fail --max-time 180 http://127.0.0.1:8011/v1/audio/transcriptions \
  -F file=@utterance.wav -F model=whisper-1 -F response_format=json
```

Confirmed working this session: real transcription of a 15s spoken-philosophy
WAV returned recognizable non-empty text.
`whisper-1` is the required HTTP compatibility alias; sending
`model=whisper-medium` fails DTO validation. The server config selects the
actual checkpoint. Response is `{"text":"..."}` with `X-Session-ID`.
Do not claim GPU execution from `/health` or config text alone — the export
log and a real transcription result are the actual evidence.
Do not start before artifact/provenance gates: startup can download/export
models. Never persist tokens or enable sentiment/diarization defaults.
`scripts/smoke/10_audio_analyzer.sh` is **PASS** (needs `AUDIO_ANALYZER_URL`
and `AUDIO_ANALYZER_SAMPLE_WAV` set), not a blocked/exit-2 entry point.
