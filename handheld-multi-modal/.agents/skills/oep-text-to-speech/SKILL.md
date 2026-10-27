---
name: oep-text-to-speech
description: Use when configuring the stock Open Edge Platform Text To Speech service (SpeechT5, CPU by default), switching its device, or changing how the Backend requests speech.
---

# Text To Speech

Image `intel/text-to-speech:2026.2.0`, source `edge-ai-libraries`
(release-2026.2.0). Compose service `tts`; host `127.0.0.1:8012` maps to
container port `8011`.

## Configuration

- `config/tts/config.yaml` replaces the stock
  `/app/text-to-speech/config.yaml` through a read-only mount.
  `TEXT_TO_SPEECH_CONFIG_OVERRIDE_PATHS` is not used.
- The model is the stock SpeechT5. Parler-TTS cannot be used with this image
  because it does not include the `parler-tts` package.
- Settings live under `models.tts`; runtime (`openvino` or `pytorch`),
  `device`, model and variant are separate fields.
- The device is CPU by default (Compose `cpus: 4`) so speech does not queue
  behind LLM generation on the GPU. `make speech-device TTS_DEVICE=GPU`
  switches to GPU through the generated `config/devices.yaml`;
  `TTS_DEVICE=CPU` switches back.
- The service downloads and exports its model on first start; warmup errors
  are not fatal. Persistent volumes cover `models`, `storage` and `.cache`
  under `/app/text-to-speech`.

## Usage

The Backend (`backend/app/speech.py`) is the only caller. Do not add a custom
TTS service.

```sh
curl -fsS http://127.0.0.1:8012/health
curl -fsS http://127.0.0.1:8012/v1/model-info
curl -fsS -o out.wav http://127.0.0.1:8012/v1/audio/speech \
  -H 'content-type: application/json' \
  -d '{"input":"The camera stream is ready.","response_format":"wav"}'
```

- WAV responses return `audio/wav` with `X-Session-ID`. JSON mode returns
  `audio_base64`, sampling rate, duration and model and voice metadata.
- The request `model` field is metadata only; it does not select a model.
- `GET /v1/audio/voices` lists voices.
- `/health` does not prove synthesis works; check the `device` field from
  `/v1/model-info` and a real WAV.

