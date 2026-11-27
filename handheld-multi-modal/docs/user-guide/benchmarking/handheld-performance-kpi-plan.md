<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Handheld Performance KPI Plan

## 1. Scope

The Handheld measures and shows a small set of industry-standard performance
KPIs for the application, its services, and the hardware underneath. The
operator reads them in the Panel. Tools read the same values from
`GET /api/performance` and `GET /api/metrics`.

The goal is to make performance visible and explainable. It is not a
qualification of a target device; no target hardware is available.

**Out of scope**

- **Performance gating.** The Panel never judges a value: no target
  comparison, no pass/fail, no ✓/! status, no threshold settings. The reference
  values in Section 2 are kept so a later CI job or automated test can adopt
  them.
- **Time to ready** (start-up to usable console), **measurement runs and
  saved scorecards**, an expanded multi-tab view, sparklines, and model-strip
  chips. Not planned.
- **Changes to Open Edge Platform services.** Values only a service can produce
  are requested upstream (Section 6).

**Who measures what**

| Owner | Measures | Examples |
| --- | --- | --- |
| Handheld (Backend, browser, own DLSPS callbacks) | What the operator experiences end to end, at call sites the Handheld already has | Time to alert, time to spoken answer and its stages, camera FPS and drops, first words, speech-to-text and text-to-speech call time, approximate words/s, peak memory |
| Metrics Manager | Service-internal metrics, scraped from each service | Queue wait, model time, exact tokens/s, outcomes and error rate, in-flight requests, histograms |

The Backend keeps no queue, in-flight, outcome, or histogram state for
services. It reads the service series from Metrics Manager, as it already does
for hardware telemetry, and relays them.

**Design rules** (from [AGENTS.md](../../../AGENTS.md)):

- Open Edge Platform services stay unmodified and pinned.
- The browser receives data only through the Backend.
- The Backend stays out of the video path; video timing travels as metadata.
- New endpoints stay behind the existing loopback/NGINX boundary.
- No transcripts or questions are stored for KPIs.

## 2. KPIs

### Operator KPIs

| KPI | Operator question | Measured from → to | Reference value (not evaluated) | Status |
| --- | --- | --- | --- | --- |
| Time to alert | "How quickly am I told something appeared?" | Decoded frame enters the pipeline → alert raised by the Backend | < 400 ms | Implemented |
| Time to spoken answer | "After I stop talking, how long until I hear the answer?" | End of the operator's speech → first audible sound of the answer | < 3 s | Implemented |
| Smooth cameras | "Can I watch all my cameras without stutter?" | Per camera: processed vs. source FPS and skipped frames | ≥ 3 cameras at 1080p | Implemented |
| Answer speed | "Does the answer stream faster than I can read it?" | Decode tokens/s = (output tokens − 1) ÷ (first → last token), per request | > 15 tokens/s (7B, 4-bit) | Approximate words/s now; exact after B1 |
| Peak memory | "Am I close to running out of memory?" | Peak host RAM over the rolling window | < 80 % | Implemented |
| Compute busy | "Is the hardware working or idle?" | Host CPU utilization under load | > 70 % under load | Resources tab |

Reference values come from datacenter-class sources or product goals. They
are starting guidance; calibrate them on the device before any gate uses them.

### Service tiles

One tile per service. The headline is what the operator experiences, measured
by the Handheld. The service's own numbers explain it, in Details.

| Service | Headline (Handheld) | Details (Chat Q&A / Audio Analyzer / Text To Speech, via Metrics Manager) | Reference value |
| --- | --- | --- | --- |
| Chat Q&A | "First words in 1.2 s": question sent → first streamed text | Model time to first token (TTFT), queue wait, error rate | TTFT < 2 s |
| Audio Analyzer | "Speech heard 2.0x faster than real time": audio length ÷ call time | Model-only speed (RTFx), error rate | > 1x, aim ≥ 2x |
| Text To Speech | "Voice ready in 400 ms": call time for one clip (SpeechT5 returns the whole clip, so this is time to first audio) | Model time to first audio, real-time factor (RTF), error rate | RTF < 1.0 |

Video and alert KPIs already sit on the video badge, alert rows, and camera
table (Section 7), so DL Streamer and Alert Agent get no service tile.

**Why the headline is always the Handheld value:** it is what the operator
feels, and it exists today. Service values measure only part of the path (no
loopback, often no queue or retrieval). Swapping the headline to them would
make the number jump with no visible reason. The one exception is answer
speed: exact tokens/s **replaces** the approximate words/s, because both
measure the same thing and one is exact.

### Industry standards behind them

The operator KPIs are plain-words roll-ups, built from standard signals:

| Operator KPI | Standard parts |
| --- | --- |
| Time to spoken answer | Silence wait + ASR latency + LLM time to first token + TTS time to first audio + playback start |
| Time to alert | Pipeline latency (decode → detection) + rule and dispatch time |
| Smooth cameras | Processed FPS vs. source FPS, dropped-frame ratio |
| Answer speed | Decode tokens/s = 1 / time per output token (TPOT) |
| Peak memory, Compute busy | USE method: utilization and saturation |

Per service, Metrics Manager carries the RED and golden signals: request rate,
error rate, duration, and saturation (queue wait, in-flight). Domain signals
sit on top: TTFT and tokens/s for the LLM, RTF/RTFx for speech, FPS and drops
for video. ASR accuracy (WER) is an offline benchmark, never a live tile.

### Measurement rules

- **Latency:** p50 ("typical") and p95 ("95% at or below") over a rolling
  window, with the sample count. Percentiles are never averaged or added.
  No p99: at handheld request volumes it is noise.
- **Throughput** (FPS, tokens/s, words/s, RTF): p50 of per-request values.
  A ratio of totals, such as `sum(audio) / sum(model time)`, is the
  audio-weighted mean, not a typical request; the service RTFx in Section 5 is
  this kind and is labelled as such.
- **Few samples:** fewer than 5 samples reads "Not enough data yet". One
  sample is never shown as typical.
- **Missing, stale, zero:** kept distinct. A missing value is left out (a tile
  with nothing to show reads "Not available yet"), a stale service value is
  marked "◷", a real zero is shown as zero. Nothing is synthesized.
- **Successful requests only** for Handheld timings; failures are not timed.
  Error rates come only from services: (`error` + `timeout`) ÷ all counted
  requests. No counted requests means no rate, never 0 %. Cancellations,
  no-speech, and intentional suppression are not errors.
- **Utilization only under load:** Compute busy and memory describe load; an
  idle device is not a problem, and high utilization alone is not efficiency.
- **Clocks:** one process uses its monotonic clock. Intervals across containers
  use the host wall clock (one kernel clock on one device). Browser intervals
  use `performance.now()` only; browser and server timestamps are never
  subtracted.
- **Windows:** Handheld values cover the last 5 minutes. Service values from
  Metrics Manager are cumulative since the service started (Section 5), and
  the Panel says so.

### Time to spoken answer: definition

- **Start:** the operator stops speaking. Tap-to-send: the second tap.
  Auto-send: the last audio buffer above the voice threshold, not the moment
  auto-send fires.
- **End:** the browser `playing` event of the first answer clip. This is a
  playback-start proxy; leading silence in the clip and output-device latency
  need an acoustic check on the target device.
- **Included:** silence wait, upload, transcription, assistant routing and
  generation, synthesis of the first answer chunk, delivery, playback start.
- **Excluded:** the "Checking." acknowledgement (shown separately as
  "Acknowledged in"); turns started with Audio off (not counted, even if Audio
  is turned on during the turn); turns where the browser blocked playback or no
  answer was heard (counted as "not timed").

Auto-send waits 1.5 s of silence. The Panel shows it as its own "Listening"
stage so its cost is visible; tuning it is a product decision.

## 3. Status

| Item | Status | Where |
| --- | --- | --- |
| Video: ingress stamp, pipeline latency, FPS and drops, camera health, time to alert | Done | `scripts/dlsps/frame_ingress.py`, `crop_candidates.py`, `backend/app/{ingest,alerts,performance}.py` |
| Voice: time to spoken answer and its four stages (browser) | Done | `backend/app/static/index.html`, `POST /api/performance/marks` |
| First words and approximate words/s | Done | `backend/app/assistant.py` → collector |
| Speech-to-text and text-to-speech call timing | Done | `backend/app/voice.py`, `backend/app/main.py` `build_synthesizer` → collector |
| Peak memory | Done | collector reads existing telemetry |
| Collector, `GET /api/performance`, `performance` SSE event | Done | `backend/app/performance.py`, `backend/app/main.py` |
| Service series read from Metrics Manager (`services` in telemetry) | Done; no service exports yet | `backend/app/telemetry.py` |
| Scrape block for service `/metrics` | Done, commented out | `config/metrics/telegraf.conf` |
| Panel: video badge, answer footer, alert tooltip, Performance tab, service tiles (headline rule, no target judgement) | Done | `backend/app/static/index.html` |

## 4. Handheld instrumentation

### Video: time to alert and FPS

The pipelines in `config/dlsps/{npu,gpu,cpu}/config.json` belong to the
application. Two application `gvapython` callbacks carry the timing:

```text
{auto_source} ! … ! gvapython name=ingress module=/opt/handheld-crops/frame_ingress.py class=FrameIngress
  ! queue … leaky=downstream ! gvadetect … ! gvatrack … ! gvametaconvert … ! gvapython name=cropcandidates … ! …
```

[`frame_ingress.py`](../../../scripts/dlsps/frame_ingress.py) sits before the
first leaky queue. For each frame it increments a counter and attaches
`{"handheld_ingress": {"wall": …, "mono_ns": …, "sequence": n}}`. The message
travels with the buffer, so frames dropped by a leaky queue simply never
arrive. It never maps pixel data.

[`crop_candidates.py`](../../../scripts/dlsps/crop_candidates.py) removes that
message and adds to `crop_context`:

| Field | Value |
| --- | --- |
| `ingress_at` | Host wall-clock seconds when the decoded frame entered the pipeline |
| `pipeline_ms` | Ingress → this callback, on the monotonic clock of the DLSPS process |
| `frames_in` | Ingress counter at this frame |
| `frame_sequence` | Frames out |
| `source_fps` | `fps_n / fps_d` when both are positive |

The Backend ([`ingest.py`](../../../backend/app/ingest.py) →
[`performance.py`](../../../backend/app/performance.py) `Performance.frame`)
keeps per camera:

| Value | Formula | Window |
| --- | --- | --- |
| Processed FPS | `Δframe_sequence / Δt` | 5 s |
| Ingress FPS | `Δframes_in / Δt` | 5 s |
| Skipped frames | `1 − Δframe_sequence / Δframes_in` | 5 s |
| Detection delay | Backend receive time − `ingress_at` (0–60 s accepted) | 30 s |
| Pipeline latency | `pipeline_ms` | 30 s |
| Health | Offline (no frames for 5 s), Measuring, Dropping (> 10 % skipped; without the ingress counter, processed < 90 % of the caps frame rate, which cameras can overstate), Delayed (detection delay p95 over 400 ms, at least 5 samples), Smooth | — |

Health is the camera's operating state, shown to explain a stutter. Its fixed
limits (`MAX_DROP_PERCENT`, `MIN_SOURCE_RATIO`, `DELAYED_SECONDS` in
`performance.py`) are part of the state definition, not KPI targets, and are not
configurable.

A new tracking session, or a counter that goes backwards, restarts the counts.

**Time to alert** = alert raised − `ingress_at` of the frame that first saw
the object. [`alerts.py`](../../../backend/app/alerts.py) records it only for
alerts raised on that first observation by a rule without dwell, so a rule that
waits on purpose is not counted as a slow pipeline.

DLSPS's own `avg_pipeline_latency` is not used: it pairs probes through a FIFO
that drifts when leaky queues drop frames.

### Voice: time to spoken answer and stages

Measured in the browser on one monotonic clock.

| Mark | When |
| --- | --- |
| `speechEnd` | Last loud audio buffer (auto-send) or the tap (manual) |
| `sent` | Upload to `/api/voice/transcribe` starts |
| `heard` | Transcription returned with text |
| `firstText` | First streamed answer text from `/api/ask` |
| `playing` | `playing` event of the first answer clip that is not "Checking." |

Stages: **Listening** = sent − speechEnd, **Hear** = heard − sent, **Think** =
firstText − heard, **Speak** = playing − firstText. The browser posts one mark
per turn:

```json
POST /api/performance/marks
{"kind": "voice_turn", "outcome": "spoken", "spoken_answer_ms": 2410,
 "ack_ms": 820, "stages": {"listening": 1540, "hear": 310, "think": 420, "speak": 140}}
```

`outcome` is `spoken`, `blocked`, or `failed`; only `spoken` carries timings.
The Backend accepts only known keys and whole milliseconds 0–120 000 (else
422).

### Service calls seen by the Handheld

Timed with `time.monotonic()` around calls the Backend already makes, and
recorded only after success:

| Value | Call | Formula |
| --- | --- | --- |
| First words | `assistant.py` `_chat_stream` | Request start → first streamed text |
| Approximate words/s | same | Words ÷ (generation − first text); answers with ≥ 5 words over ≥ 0.2 s |
| Speech-to-text call time and speed | `voice.py` `transcribe` | Call time; speed = WAV length ÷ call time |
| Text-to-speech call time and RTF | `main.py` `build_synthesizer` | Call time; RTF = call time ÷ WAV length |

Not recorded: failures, TTS cache hits, and the idle warm-up calls (the
"Checking." warm-up and the silent Whisper wake-up), which are not operator
requests.

These values include the service's queue and the loopback hop. That is
intended: it is what the operator waits for. Text chunks are not tokens, so
words/s is always shown with "~" and never as tokens/s.

### Collector and endpoints

[`performance.py`](../../../backend/app/performance.py) keeps bounded rolling
windows (5 minutes, at most 2000 samples each, at most 64 cameras). Each
summary has `last`, `p50`, `p95` (nearest rank), `count`, and `status`
(`no_data`, `insufficient` below 5 samples, otherwise `info`).

| Endpoint | Purpose |
| --- | --- |
| `GET /api/performance` | Snapshot `{version, window_seconds, generated_at, kpis, cameras}` |
| `performance` event on `/api/stream` | Same snapshot, about 1 Hz |
| `POST /api/performance/marks` | Browser voice-turn marks |

`kpis` holds `time_to_alert`, `spoken_answer` (with stage p50s,
`acknowledged_p50`, `untimed`), `cameras` (`smooth`, `running`),
`answer_speed` (approximate words/s), `memory` (`last`, `peak`), and
`services`. No KPI carries a target or a pass/fail status:

```json
"services": {
  "chat": {"first_words": {…summary, "unit": "s", "source": "handheld"}},
  "asr":  {"request_seconds": {…, "unit": "s"}, "rtfx": {…, "unit": "x"}},
  "tts":  {"request_seconds": {…, "unit": "s"}, "rtf": {…, "unit": "ratio"}}
}
```

### Hardware

The collector reads the existing 1 Hz telemetry snapshot
([`telemetry.py`](../../../backend/app/telemetry.py)) for peak memory. Host
CPU and per-container CPU/RAM stay on the Resources tab.

## 5. Service metrics through Metrics Manager

Path: **service `/metrics` → Metrics Manager (Telegraf scrape) → Backend
`telemetry.py` → Panel**.

### What Metrics Manager does (checked in `intel/metrics-manager:2026.3.0-rc2`)

- **Prometheus scrape keeps histograms.** `_bucket{le}`, `_sum`, and `_count`
  reach `:9273`, `GET :9090/api/v1/metrics/latest`, and `/metrics/stream`.
- **OTLP push drops buckets.** It keeps only `_count`, `_sum`, `_avg`, `_min`,
  `_max`, so services should be scraped, not pushed, when percentiles matter.
- **Every series arrives flat:** `{name, tags{host, …}, fields{value},
  timestamp}`. The type is lost; a scrape adds a `url` tag. Timestamps are ns
  on `/latest`, ms on the stream. Series live up to 60 s after the last write.
- **Latest value only, no history.** Percentiles from buckets are therefore
  cumulative since the service started. The Backend keeps no history to turn
  them into a window, by design.
- **`metric_version` must be 1.** With 2, a scraped histogram duplicates the
  `le` label, `/latest` returns HTTP 500, and *all* telemetry stops.

### Handheld side

- `config/metrics/telegraf.conf`: one commented-out `[[inputs.prometheus]]`
  block for the three services, `metric_version = 1`, `namepass` limited to
  the series below. Enable it only after a pinned release serves `/metrics`.
- `backend/app/telemetry.py`: one name table, `SERVICE_METRICS`, maps upstream
  series to Panel keys, so a rename is a one-line change. `normalize()` adds a
  `services` object to the telemetry snapshot. It is always present, all
  `missing` before the first poll or if parsing fails, so host telemetry is
  never lost. Derivations are read-side only: RTFx from audio and model durations,
  error rate = (`error` + `timeout`) ÷ total from the `outcome` counter, p50/p95
  by nearest rank on cumulative buckets, mean from `_sum` / `_count`.

| Service | Panel key | Upstream series |
| --- | --- | --- |
| Chat Q&A | `ttft_seconds` | `chatqna_ttft_seconds` (histogram) |
| | `queue_wait_seconds` | `chatqna_queue_wait_seconds` (histogram) |
| | `decode_tokens_per_second` | `chatqna_decode_tokens_per_second` (histogram) |
| | `request_seconds` | `chatqna_request_seconds` (histogram) |
| | `error_rate_percent` | `chatqna_requests_total{outcome}` |
| | `in_flight` | `chatqna_requests_in_flight` (gauge) |
| Audio Analyzer | `model_seconds` | `audio_analyzer_inference_seconds` (histogram) |
| | `audio_seconds` | `audio_analyzer_audio_duration_seconds` (histogram) |
| | `rtfx` | derived: mean `audio_seconds` ÷ mean `model_seconds` (audio-weighted, since start) |
| | `error_rate_percent` | `audio_analyzer_requests_total{outcome}` |
| | `in_flight` | `audio_analyzer_requests_in_flight` (gauge) |
| Text To Speech | `time_to_first_audio_seconds` | `tts_time_to_first_audio_seconds` (histogram) |
| | `rtf` | `tts_rtf` (histogram) |
| | `request_seconds` | `tts_generate_seconds` (histogram) |
| | `error_rate_percent` | `tts_requests_total{outcome}` |
| | `in_flight` | `tts_requests_in_flight` (gauge) |

Each value is `{value, unit, state: ok|stale|missing, p50, p95, count}`.

## 6. Upstream requests (hand-off)

Source checked: [`open-edge-platform/edge-ai-libraries`](https://github.com/open-edge-platform/edge-ai-libraries)
at tag `v2026.3.0-rc1`. Compose now pins `2026.3.0-rc2` (rc3 for DL Streamer
Pipeline Server); re-check the "Today" notes against the pinned release before
filing. None of the requested values is exposed yet.

These are reusable service measurements for any caller, benchmark, or
operations team; the Panel uses a subset. The reference values in Section 2
are application goals, not service SLOs, and no per-service budget is
requested.

### Minimum request

Each service exposes these on `GET /metrics` (Prometheus text, internal port),
exactly with the names in Section 5:

| Service | Minimum set |
| --- | --- |
| Chat Q&A | Queue wait, time to first token, decode tokens/s, request duration (histograms); requests by `outcome`; in-flight |
| Audio Analyzer | Inference (model) duration, audio duration (histograms); requests by `outcome`; in-flight |
| Text To Speech | Time to first audio, RTF, generate duration (histograms); requests by `outcome`; in-flight |

Everything else under B1–B6 is optional.

### Common contract

1. Backward compatible: existing responses unchanged; new data additive or
   opt-in.
2. Durations in seconds as histograms with buckets, totals as counters,
   in-flight as a gauge. Percentiles are derived downstream, never exported
   pre-averaged.
3. Bounded labels only: `model`, `device`, `precision`, `outcome` (`ok`,
   `error`, `timeout`). Never request IDs, prompts, transcripts, URLs,
   filenames, or session IDs.
4. Scraped as Prometheus text (Section 5). An OTLP exporter is fine for other
   deployments if it is configurable and keeps histogram buckets; do not
   hard-code Metrics Manager in service code.
5. Queue wait measured from before lock acquisition to after it.
6. Requested and verified precision reported separately where the service
   reports its model.
7. A failed or missing export never blocks inference and never creates a zero
   sample.

**Effort** is the upstream change needed to *measure* the value, not to make it
fast: **Low** adds a timer or counter; **Medium** ties a measurement to the
individual request; **High** changes how token, frame, or metric data is
captured. Related values share instrumentation.

### B1. Chat Q&A Core (highest priority)

**Today:** `POST /v1/chatqna/chat` streams raw `data: {chunk}` frames with no
usage or timing ([`app/server.py`](https://github.com/open-edge-platform/edge-ai-libraries/blob/v2026.3.0-rc1/sample-applications/chat-question-and-answer-core/app/server.py),
`app/chain.py` `process_query`); with sources enabled it first sends
`event: sources`. CPU and GPU generate through LangChain `HuggingFacePipeline`
with a fixed `weight_format="int8"` (`app/openvino_backend.py`), so OpenVINO
GenAI `PerfMetrics` is not reachable and **the 4-bit profile cannot run on CPU
or GPU**; the tokenizer is reachable as `llm.pipeline.tokenizer`. The NPU path
runs OpenVINO GenAI with int4 (`GenAILLM`, `app/openvino_xpu.py`), so exact
tokens and `PerfMetrics` are reachable there but not returned. A process-wide
`_generation_lock` serializes generation without timing the wait, so queueing
shows up as slow first words. RAG is FAISS retriever → optional reranker →
prompt → LLM. `GET /model` returns only the model ID. No `/metrics`.

| Value | Definition | What is missing | Effort |
| --- | --- | --- | --- |
| Request duration | Request received → last byte, including RAG and queue | Callers can time it; the service reports nothing | Low |
| Requests by outcome | `ok` / `error` / `timeout` counter | No totals, so no error-rate denominator | Low |
| Queue wait | Before → after acquiring `_generation_lock` | Lock exists, wait not timed | Low |
| Time to first token (TTFT) | LLM call start → first generated token (retrieval excluded) | Only decoded text leaves the model; CPU/GPU and NPU paths both need changes | High |
| Decode tokens/s | `(output_tokens − 1) / decode_s`, only when `output_tokens ≥ 2` | No token counts or first/last-token times; text chunks are not tokens | High, shared token work |
| Input/output tokens | Rendered-prompt tokens; generated tokens | Same token work; totals are Low once counts exist | High → Low |
| Retrieval, rerank time | Embedding + FAISS search; reranker (optional) | Not timed | Low |

Implementation options: a LangChain callback that timestamps
`on_llm_new_token` and recounts the answer with the tokenizer (minimal), or
`openvino_genai.LLMPipeline` `PerfMetrics` (preferred long term). Optional:
`"stream_options": {"include_metrics": true}` emitting a final
`event: metrics` (opt-in, because current clients join every `data:` frame
into the answer); `GET /model` with runtime, devices, requested and verified
weight format; `LLM_WEIGHT_FORMAT` (`int8`, `int4`, `fp16`) for CPU and GPU.

**Handheld use:** the Answer speed tile shows exact tokens/s instead of
~words/s; the Chat Q&A tile Details show model TTFT, queue wait, and error
rate.

### B2. Audio Analyzer

**Today:** `POST /v1/audio/transcriptions` (OpenAI-compatible) runs
`openvino_genai.WhisperPipeline`, serialized by `_TRANSCRIBE_LOCK`
([`api/openai_endpoints.py`](https://github.com/open-edge-platform/edge-ai-libraries/blob/v2026.3.0-rc1/microservices/audio-analyzer/api/openai_endpoints.py),
`components/asr/openvino_genai/whisper.py`). `/v1/performance` holds only the
**last** latency, which another caller can overwrite, so it cannot be tied to a
request. `/v1/model-info` reports configured, not verified, precision. No
`/metrics`. `/v1/realtime` previews do not apply: the Handheld uploads one WAV
per utterance.

| Value | Definition | What is missing | Effort |
| --- | --- | --- | --- |
| Inference duration | `generate()` time for one recording | Only the last latency, not per request | Medium |
| Audio duration | Decoded samples ÷ sample rate | Not exported, so no service RTFx | Medium, shared with inference |
| Requests by outcome | `ok` / `error` / `timeout` | No totals | Low |
| Queue wait (optional) | Wait at `_TRANSCRIBE_LOCK` | Lock exists, wait not timed | Low |

Optional: `Server-Timing` headers and a `performance` object in
`verbose_json`; `/v1/model-info` with runtime, resolved device, verified
precision.

**Handheld use:** the Audio Analyzer tile Details show model-only speed and
error rate next to the Handheld's call speed.

### B3. Text To Speech

**Today:** `POST /v1/audio/speech` returns a fully buffered WAV
([`api/openai_endpoints.py`](https://github.com/open-edge-platform/edge-ai-libraries/blob/v2026.3.0-rc1/microservices/text-to-speech/api/openai_endpoints.py));
SpeechT5 runs through `openvino_genai.Text2SpeechPipeline` behind an inference
lock. `/v1/audio/speech/stream` exists, but SpeechT5 emits one event after full
synthesis. `/v1/performance` holds only the last latency, and its timer stops
before the audio is encoded. No `/metrics`.

| Value | Definition | What is missing | Effort |
| --- | --- | --- | --- |
| Time to first audio | Request start → first playable audio encoded and ready to send | Timer stops before encoding; not per request | Medium |
| Generate duration | Request start → final encoded audio | Not per request | Medium |
| RTF | `generate_s / audio_duration_s` | Audio duration is returned, matching service time is not | Medium, shared |
| Requests by outcome | `ok` / `error` / `timeout`; a stream that fails after HTTP 200 is an error | No totals | Low |
| Queue wait (optional) | Wait at the inference lock | Not timed | Low |

SpeechT5 produces one chunk, so time to first audio equals generate duration
today. Neither measures playback start. Earlier first audio needs an
incremental model; transport changes alone do not give it. Optional, highest
UX value: phrase-level output on `/v1/audio/speech/stream`.

**Handheld use:** the Text To Speech tile Details show model time to first
audio, RTF, and error rate.

### B4. DL Streamer Pipeline Server (optional)

The Handheld callbacks already cover video and remain the fallback.

| Value | What is missing | Effort |
| --- | --- | --- |
| `frames_in/out/dropped_total` per stream, with bounded drop reasons | Output FPS exists; matching ingress counters and direct drop counts do not | Medium |
| Decode → result latency paired per buffer | Current probes pair by arrival order (FIFO), which drifts when leaky queues drop frames | High |
| Frame freshness (age of last output) | Not exposed | Low |
| Ingress timestamp option on `gvametaconvert` (would retire `frame_ingress.py`) | Not available | Medium |

Source: [`src/server/gstreamer_pipeline.py`](https://github.com/open-edge-platform/edge-ai-libraries/blob/v2026.3.0-rc1/microservices/dlstreamer-pipeline-server/src/server/gstreamer_pipeline.py).

### B5. Alert Agent (optional, low priority)

`/metrics` returns 404 and `duration_ms` covers only action execution
([`src/main.py`](https://github.com/open-edge-platform/edge-ai-libraries/blob/v2026.3.0-rc1/microservices/alert-agent-service/src/main.py)).
One request can trigger several actions and retries, and an accepted webhook
does not prove delivery.

| Value | What is missing | Effort |
| --- | --- | --- |
| `alert_agent_ingest_to_dispatch_seconds{mode, outcome}` | Returned duration does not cover the whole request | Low |
| Action attempts, retries, and failures by outcome | No totals; request counts cannot recover action counts | Medium |
| Suppressed alerts | The Handheld counts its own; the service exports nothing | Low |

The Handheld already times its own dispatch.

### B6. Metrics Manager (optional)

Only scrape configuration is added on the Handheld side. For other users of
the service, the useful upstream changes are:

| Value | What is missing | Effort |
| --- | --- | --- |
| Keep histogram buckets on OTLP ingest | OTLP push keeps only count, sum, average, min, max, so no percentiles (Section 5) | High |
| Standard OTLP protobuf/gzip on the HTTP route | At rc1 the route parses JSON only | Medium |
| Rejected/dropped and forwarded points, by reason and output | Error counters do not cover every lost point | Medium |

**OTLP push as a fallback.** A service that can only push may use the existing
OTLP/JSON route. The Panel then shows counts and means but p50/p95 stay empty,
so prefer the scrape. Compose settings for that path (set both proxy-bypass
variables so in-network exports skip the download proxy):

```yaml
OTEL_EXPORTER_OTLP_METRICS_ENDPOINT: "http://metrics:9090/api/v1/metrics/otlp"
OTEL_EXPORTER_OTLP_METRICS_PROTOCOL: "http/json"
OTEL_EXPORTER_OTLP_METRICS_COMPRESSION: "none"
OTEL_SERVICE_NAME: "chat-qna"
no_proxy: "localhost,127.0.0.1,metrics"
NO_PROXY: "localhost,127.0.0.1,metrics"
```

The Backend reads the latest value of each series and labels it "since it
started", which assumes cumulative counters. Check the temporality the route
stores before relying on this path; with delta, counts cover only the last
export interval.

**Not requested:** MediaMTX, Coturn, Mosquitto, Model Download; per-service
GPU/NPU or device-memory metrics.

### Upstream acceptance checks

For each service change, the service team checks:

- Each request is counted once with one outcome; cancellations, no-speech, and
  warm-up calls do not count as errors or successes.
- Timings are per request and stay correct under concurrent requests.
- Token counts use the model's tokenizer; RTF uses decoded audio duration;
  time to first audio is not playback start.
- Names, units, labels, and buckets survive restarts and multiple workers.
- No sensitive or unbounded values appear in labels.
- Speed comparisons across models also compare quality: WER for ASR,
  intelligibility for TTS, answer grounding for RAG, precision/recall for
  video. These are offline benchmarks, never live tiles.

## 7. Display in the Panel

### Principles

1. **Answer the question where the operator looks.** Time to alert on the
   video, time to spoken answer under the answer, camera health in the camera
   list. The Performance tab is the summary.
2. **Plain words, then the number, always with a unit.** "Answered aloud in
   2.4 s", not "e2e p95 2412 ms". The standard name (TTFT, RTFx, RTF) is small
   text beside it.
3. **Describe, never judge.** No ✓/!, no "Meets target", no coloured status
   border. The only state markers are text: "Not enough data yet", "Not
   available yet", "◷" before a stale service value, "~" for approximate.
4. **The headline is what the operator experiences.** Service values explain
   it in Details and never replace it, except exact tokens/s replacing ~words/s.
5. **Say where a number comes from.** Details name the source: "Measured by
   the Handheld (last 5 min)" or "Reported by Chat Q&A (since it started)".
6. **Show only what exists.** Details list only values that are present. One
   closing line says what would add more, for example "More detail after the
   Chat Q&A update."
7. **One number per tile, nothing to configure.** Operators never pick
   metrics, windows, or percentiles. Typical (p50) first, 95% in Details.
8. **Quiet and touch-friendly.** No pop-ups, no 1 Hz screen-reader
   announcements, 44 px touch targets, tiles wrap on narrow screens, and
   anything important is visible text, not only a tooltip. Tiles are built once
   and updated in place, so an open Details stays open.

### Where values appear

1. **Video badge (`#latency-badge`).** "Detect 120 ms · 29.8/30 fps". When a
   camera is not smooth the badge starts with the reason ("Dropping frames",
   "Delayed"); while warming up, "Measuring camera…".
2. **Answer footer (`#answer-timing`).** "Answered aloud in 2.4 s". Opening it
   shows the stages:

   ```text
   [Listening 1.5 s][Hear 310 ms][Think 420 ms][Speak 140 ms]
   From the end of your speech to the first sound of the answer. Acknowledged after 820 ms.
   ```

   Typed questions show "First words in 640 ms". An untimed turn says why:
   "Spoken answer not timed · Audio is off" (or "· browser blocked audio",
   "· no spoken answer was heard").
3. **Alert rows (`#alerts`).** The latency chip reads "Time to alert (camera
   frame to alert raised): 210 ms", only for alerts raised on appearance (the
   same alerts the KPI counts); dwell rules and later zone entries omit it.
4. **Performance tab (Resources | Performance switch).** Five KPI tiles, the
   service row, and the camera table:

   ```text
   ┌ Time to alert ─┐┌ Spoken answer ─┐┌ Smooth cameras ┐┌ Answer speed ──┐┌ Peak memory ───┐
   │ 250 ms         ││ 2.4 s          ││ 1 of 2         ││ ~9.5 words/s   ││ 52%            │
   │ typical, 12    ││ typical, 14    ││ Camera 2:      ││ Approximate:   ││ Now 41% of     │
   │ alerts         ││ answers        ││ 25% of frames  ││ words, not     ││ host RAM       │
   │                ││                ││ skipped        ││ tokens         ││                │
   └────────────────┘└────────────────┘└────────────────┘└────────────────┘└────────────────┘
   Services
   ┌ Chat Q&A ──────────┐┌ Audio Analyzer ────────────┐┌ Text To Speech ──┐
   │ First words in 1.2 s││ Speech heard 2.0x faster   ││ Voice ready in   │
   │ TTFT                ││ than real time · RTFx      ││ 400 ms · TTFA·RTF│
   │ ▸ Details           ││ ▸ Details                  ││ ▸ Details        │
   └─────────────────────┘└────────────────────────────┘└──────────────────┘
   Camera   | Health          | Frames/s  | Skipped | Detect
   Camera 1 | Smooth          | 29.8 / 30 | 0%      | 120 ms
   Camera 2 | Dropping frames | 22.5 / 30 | 25%     | 200 ms
   ```

   Chat Q&A Details today, and after the service update:

   ```text
   Typical 1.2 s · 95% at or below 2.1 s          Typical 1.2 s · 95% at or below 2.1 s
   Measured by the Handheld (last 5 min, 18       Measured by the Handheld (last 5 min, 18
   requests), including time waiting in the       requests), including time waiting in the
   service queue                                  service queue
   More detail after the Chat Q&A update          Reported by Chat Q&A (since it started):
                                                    model first token 600 ms, queue wait 300 ms,
                                                    errors 0.0%
   ```

   Durations use one format everywhere: milliseconds below 1 s, otherwise
   seconds with one decimal. KPI tiles are built once and updated in place,
   and the tap explanation is rewritten only when it changes. In Performance
   mode the resource-only header controls (status, Details, Expand) are
   hidden. Compute busy stays on the Resources tab.

## 8. Remaining work

Paths are relative to `handheld-multi-modal/`. Each package ends with
`make test-all` passing.

**Guardrails:** no gating; no Backend queue, in-flight, outcome, or histogram
state for services; Handheld timings only at existing call sites, successful
calls only; never synthesize a value; no OEP service changes.

### Done: WP6 headline rule and WP7 no target judgement

- Service tile headlines always come from `kpis.services` (Handheld). Metrics
  Manager values add one "Reported by … (since it started)" Details line listing
  only present values, else "More detail after the … update". Exact decode
  tokens/s replaces ~words/s on the Answer speed tile.
- ASR key `model_seconds` reads the inference histogram; TTS RTF unit is
  `ratio` in both snapshots.
- `performance_targets`, `targets()`, the target and status lines, ✓/!,
  coloured borders and "within target" are removed. KPI tiles lead with the
  typical (p50) value and the count; 95% is in the tap explanation. Camera
  Delayed uses the fixed `DELAYED_SECONDS = 0.4`.

### When an upstream release ships `/metrics`

Pin the image, check the series names against Section 5 (fix only the
`SERVICE_METRICS` table if they differ), uncomment the scrape block, and run
`make test-live`.

## 9. Tests

In `make test-all`:

- `make test-crops`: ingress stamp consumed and removed, `frames_in` vs.
  `frame_sequence`, malformed stamps ignored.
- `make test-coverage` (`backend/test_backend.py`, 75 % gate): camera rates,
  drops, health (including the fixed Delayed limit) and session reset; no target
  or pass/fail field in any KPI; voice marks validation; time to alert only
  for alerts raised on appearance; speech-to-text and text-to-speech recorders
  (success only, cache hits and warm-ups excluded, malformed WAV skipped);
  `services` in telemetry (missing, stale, real zero, derivations, bucket
  percentiles, malformed buckets do not drop host telemetry).
- `make test-ui` (`backend/test_ui.py`): video badge, answer footer and stages
  (no target text); KPI tiles lead with the typical value, no ✓/!, target text
  or accent border, "Not enough data yet" below 5 samples, 95% in the tap
  explanation; service tiles keep the Handheld headline when service series
  appear, Details list only present values with source and window as visible
  text, "Not available yet" before the first snapshot, tiles and open Details
  survive refresh, no sideways scroll.

Not run offline: `make test-live` and the other targets that need a running
stack.

## References

Handheld:

- [Architecture and constraints](../../../AGENTS.md)
- [Telemetry and service series](../../../backend/app/telemetry.py)
- [KPI collector](../../../backend/app/performance.py)
- [LLM streaming timing](../../../backend/app/assistant.py)
- [Voice upload and transcription](../../../backend/app/voice.py)
- [Speech arbiter](../../../backend/app/speech.py)
- [Routes, TTS call, and SSE](../../../backend/app/main.py)
- [Observation ingest](../../../backend/app/ingest.py)
- [Alert dispatch timing](../../../backend/app/alerts.py)
- [Pipeline callbacks](../../../scripts/dlsps/crop_candidates.py)
- [Metrics Manager scrape config](../../../config/metrics/telegraf.conf)
- [Panel UI](../../../backend/app/static/index.html)
- [Edge Workloads and Benchmarks guide](run-edge-benchmarks.md)

Upstream (tag `v2026.3.0-rc1`):

- [Chat Q&A Core](https://github.com/open-edge-platform/edge-ai-libraries/tree/v2026.3.0-rc1/sample-applications/chat-question-and-answer-core)
- [Audio Analyzer](https://github.com/open-edge-platform/edge-ai-libraries/tree/v2026.3.0-rc1/microservices/audio-analyzer)
- [Text To Speech](https://github.com/open-edge-platform/edge-ai-libraries/tree/v2026.3.0-rc1/microservices/text-to-speech)
- [DL Streamer Pipeline Server](https://github.com/open-edge-platform/edge-ai-libraries/tree/v2026.3.0-rc1/microservices/dlstreamer-pipeline-server)
- [Alert Agent](https://github.com/open-edge-platform/edge-ai-libraries/tree/v2026.3.0-rc1/microservices/alert-agent-service)

Standards:

- [Google SRE book: four golden signals](https://sre.google/sre-book/monitoring-distributed-systems/)
- [Prometheus histograms](https://prometheus.io/docs/practices/histograms/)
- [OpenTelemetry HTTP metrics](https://opentelemetry.io/docs/specs/semconv/http/http-metrics/)
- [OpenTelemetry GenAI metrics](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-metrics.md)
- [vLLM metrics (TTFT, TPOT, queue time)](https://docs.vllm.ai/en/stable/design/metrics/)
- [OpenVINO GenAI performance metrics](https://docs.openvino.ai/2025/openvino-workflow-generative/inference-with-genai.html)
- [OpenAI streaming usage](https://platform.openai.com/docs/api-reference/chat/create)
- [W3C Server-Timing](https://www.w3.org/TR/server-timing/)
- [LLM inference latency overview](https://clickhouse.com/resources/engineering/llm-inference-latency)
