# OEP Text To Speech — deployed and smoke-tested

Status: **PASS** on this Lunar Lake host (Intel Core Ultra 9 288V), 2026-09-10.

Image `intel/text-to-speech:2026.2.0-rc3`
(`sha256:81c265d7039f96a979999fa2e1188f01e9995ce18f1ec0c171e2eaa6d337a54e`),
source `microservices/text-to-speech/`.

## Model decision: SpeechT5 (owner-approved), not Parler

Two candidates fully investigated:

1. **Parler-TTS mini-v1** — provenance chain resolved and approved (base
   model, `google/flan-t5-large` text encoder, Descript DAC codec, and
   `mistralai/Mistral-7B-Instruct-v0.2` synthetic-annotation teacher all
   confirmed non-PRC — see `docs/bootstrap/ai-contracts.md` §3 and
   `MODEL_PROVENANCE.md`). **Blocked by real packaging bug**: this pinned
   image's own `requirements.txt` never lists `parler-tts` pip package
   its own source (`utils/ensure_parler.py`) requires — confirmed via
   direct deploy attempt crashing w/ `RuntimeError: parler-tts is
   not installed`, and via `docker run --rm --entrypoint sh
   intel/text-to-speech:2026.2.0-rc3 -c "python3 -c 'import parler_tts'"`
   failing w/ `ModuleNotFoundError`. Not fixed by patching stock
   image (out of scope).

2. **SpeechT5** (`microsoft/speecht5_tts`) — stock default model. This
   bootstrap originally flagged it because original SpeechT5 paper
   states its first two authors did work as interns at **Microsoft
   Research Asia**, w/ Southern University of Science and Technology and
   Tongji University affiliations. Owner reviewed this finding directly
   and confirmed: SpeechT5 is a Microsoft product, not PRC-origin model,
   approved deploying it (2026-09-10). **Real config change
   from earlier BLOCKED state, made only after explicit owner
   confirmation of provenance override.**

`config/tts/config.yaml` now pins: `models.tts.name: microsoft/speecht5_tts`,
`runtime: openvino`, `device: GPU`, `dtype: fp16`, `model_variant: default`,
`default_speaker: Ryan`. Deployed cleanly w/ no packaging issues —
`transformers`/`torch`/`optimum-intel[openvino]` (already in pinned
image's `requirements.txt`) are all SpeechT5 needs; no extra pip package
required, unlike Parler.

## Deploy notes

- Host port `127.0.0.1:8012` -> container `8011`.
- `user: 1000:1000` (hardcoded upstream, matches named-volume ownership).
- `group_add: [video, 992]` for `/dev/dri` GPU access.
- Named volumes for `/app/text-to-speech/{models,storage,.cache}` persist
  exported OpenVINO IR and HF cache across restarts.
- Needs host's real corporate proxy passed through (not blanked) for
  first-run HF download of `microsoft/speecht5_tts` and
  `microsoft/speecht5_hifigan` vocoder — same gotcha as Audio Analyzer.

## Evidence

- Startup log: real HF fetch + cached OpenVINO IR export, then
  `utils.preload_models: Preloaded TTS model microsoft/speecht5_tts`, then
  `main: GPU warmup completed`.
- `GET /health` -> 200 OK.
- `GET /v1/model-info` confirms live: `device: GPU`, `dtype: fp16`,
  `implementation: speecht5`, 7 supported CMU-Arctic-derived voices
  (Ryan/Miles/Aaron/Nora/Elena/Kabir/Angus).
- Real synthesis: `POST /v1/audio/speech`
  `{"input":"The camera stream is ready.","response_format":"wav"}` returned
  HTTP 200, `content-type: audio/wav`, real 50220-byte valid RIFF/WAVE
  PCM 16-bit mono 16000 Hz file (verified w/ `file` command) — not
  stub or empty response.
- Full log: `docs/bootstrap/evidence/tts/smoke.log`.

## Smoke test

```
TTS_URL=http://127.0.0.1:8012 bash scripts/smoke/11_tts.sh
```

Checks `/health`, asserts `device=GPU` via `/v1/model-info`, posts real
sentence to `/v1/audio/speech`, validates response is non-trivial
WAV file (RIFF/WAVE magic bytes, `audio/wav` content type, >1KB). PASS
confirmed on this host.

## Limitations

- SpeechT5's bundled voices are CMU Arctic x-vector averages
  (`Matthijs/cmu-arctic-xvectors`), not custom voice cloning.
- English only (`default_language: English`, no other language supported by
  this stock path).
- Request `model` field is compatibility metadata only, not dynamic model
  selection — server always runs configured model.
- Qwen TTS remains prohibited; not evaluated further.
