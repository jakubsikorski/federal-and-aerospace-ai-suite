---
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
name: oep-audio-analyzer
description: Use when configuring the stock Open Edge Platform Audio Analyzer (Whisper-medium on OpenVINO GPU) or the Backend voice upload path that calls it.
---

# Audio Analyzer

Image `intel/audio-analyzer:2026.2.0`, source `edge-ai-libraries`
(release-2026.2.0). Compose service `audio-analyzer`; host
`127.0.0.1:8011` maps to container port `8010`.

## Configuration

`config/audio-analyzer/config.yaml` replaces the stock
`/app/audio_analyzer/config.yaml` through a read-only mount.
`AUDIO_ANALYZER_CONFIG_OVERRIDE_PATHS` is not used. Required values:

- `app.use_ov_genai=true`
- `models.asr.provider=openvino`, `models.asr.name=whisper-medium`,
  `models.asr.device=GPU`
- `models.asr.diarization=false`, `sentiment.enabled=false`, denoising off

The container needs `/dev/dri` and the host `video` and render groups
(`RENDER_GID`, often 992; confirm on the target). Persistent volumes cover
`models`, `chunks`, `storage` and `.cache` under `/app/audio_analyzer`.

On first start the service downloads `openai/whisper-medium` from
Hugging Face and exports it to `models/openvino/whisper-medium-fp16` (Docker volume
`audio_analyzer_models`). It needs
outbound access (and the proxy settings, if any); pass `HF_TOKEN` through the
environment when required, and never persist it.

## Data path

The browser records the microphone. The Backend (`backend/app/voice.py`)
validates and forwards a bounded WAV. The container has no `/dev/snd`.

## API

```sh
curl -fsS --max-time 10 http://127.0.0.1:8011/health
curl -fsS --max-time 180 http://127.0.0.1:8011/v1/audio/transcriptions \
  -F file=@utterance.wav -F model=whisper-1 -F response_format=json
```

- `model` must be `whisper-1` (compatibility alias); `whisper-medium` fails
  validation. The server configuration selects the real checkpoint.
- The response is `{"text": "..."}` with an `X-Session-ID` header.
- `/health` does not prove GPU execution. Check the export log and a real
  transcription.
