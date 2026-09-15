# Audio Analyzer — deployed and smoke-tested

Status: **PASS** on this Lunar Lake host (Intel Core Ultra 9 288V), 2026-09-10.

Image `intel/audio-analyzer:2026.2.0-rc3`
(`sha256:b2c5cacb2073e89ecc07af211169e8c705f673bcb6980d503fff54a7e25581f1`),
source `microservices/audio-analyzer/` (edge-ai-libraries).

## Config

`config/audio-analyzer/config.yaml` is full copy of stock base config
(mounted read-only at `/app/audio_analyzer/config.yaml`), w/ only two
values pinned away from stock defaults:

- `models.asr.name: whisper-medium` (stock default was `whisper-base`)
- `models.asr.device: GPU` (stock default was `CPU`)

`models.asr.provider: openvino` and `models.asr.diarization: False`,
`sentiment.enabled: false` already stock defaults, unchanged —
matches bootstrap requirement (sentiment/diarization both disabled).

## Deploy notes

- Ports: host `127.0.0.1:8011` -> container `8010`. (8010 already used
  for Alert Agent's host mapping in this compose file — distinct host
  port required.)
- `user: 1000:1000` (hardcoded upstream, matches named-volume ownership).
- `group_add: [video, 992]` for `/dev/dri` GPU access (`992` is this host's
  real `render` GID, confirmed via `getent group render`).
- Named volumes for `/app/audio_analyzer/{models,chunks,storage,.cache}`
  persist exported OpenVINO IR and HF cache across restarts.
- **Real proxy required.** First container start runs
  `optimum-cli export openvino --model openai/whisper-medium` to
  download+convert model. Needs outbound access to
  `huggingface.co`. Unlike other bootstrap services (which only talk to
  local peers and can safely blank `http_proxy`/`https_proxy`), this
  container must receive host's actual corporate proxy
  (`http://proxy.example.com:911` / `:912` on this host) or export
  silently stalls forever, no error, no retry log. Only `no_proxy`
  should list local addresses.
- `HF_TOKEN` passed through but not required for this public checkpoint
  (only needed if diarization were enabled).

## Evidence

- Startup log: `optimum-cli` export completes
  (`utils.ensure_model: Export successful`), then
  `components.asr.openvino_genai.whisper: Loading Model: model name=whisper-medium, device=GPU`,
  then `utils.preload_models: ASR warmup transcription completed`.
- `GET /health` -> `{"status":"ok"}`.
- Real transcription: `POST /v1/audio/transcriptions` (multipart `file`,
  `model=whisper-1` OpenAI-compatibility alias — actual model
  selected by server config, not this field) against 15s clip of
  upstream repo's own test fixture
  (`tests/philosophy_10_russell_128kb.mp3`) returned recognizable
  transcription of spoken content, proving real GPU whisper-medium
  inference (not a stub).
- Full log: `docs/bootstrap/evidence/audio-analyzer/smoke.log`.

## Smoke test

```
AUDIO_ANALYZER_URL=http://127.0.0.1:8011 \
AUDIO_ANALYZER_SAMPLE_WAV=/path/to/real-speech.wav \
bash scripts/smoke/10_audio_analyzer.sh
```

Checks `/health`, posts given WAV to `/v1/audio/transcriptions`, fails
if returned `text` is empty. No sample audio bundled w/ this repo —
supply a real one.

## Limitations

- Optional HTTP `prompt` field accepted by API but not forwarded to
  ASR (source behavior, not bootstrap gap).
- Model revision not pinned by stock export path (no explicit HF
  commit hash pin); only repo id `openai/whisper-medium` fixed.
- No microphone/`/dev/snd` mapping — Backend captures audio, uploads it;
  this service only transcribes uploaded files.
