<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Handheld Performance KPI Implementation Plan

## Scope and outcome

Give the Handheld application the ability to measure, display, and export a
small set of industry-standard performance KPIs for the application, its models,
and the hardware underneath. The operator sees them in the Handheld Panel. A
tester can run a repeatable measurement and save the result.

The goal is measurement capability, not a target-device qualification. No
target hardware is available, so this plan does not include acceptance runs.
Targets are configurable thresholds that the Panel compares against.

The work is split into two phases:

- **Phase A — application-owned (first).** Everything the Handheld application
  can measure with its own code: end-to-end latency for video and voice, FPS
  and drop counters, approximate answer speed, host memory peak, and the Panel
  UI. Deployment time to ready is planned but deferred. No microservice changes.
- **Phase B — upstream requests (later).** Values that only the service itself
  can produce: exact LLM token timing, internal ASR/TTS stage timings, and
  service-side Prometheus metrics. Section 3 is written as a hand-off
  specification for the teams that own those services.

Out of scope for this plan:

- **Performance gating.** Pass/fail evaluation of KPIs against targets, whether
  as CI jobs, automated tests, or acceptance checks, is a separate effort. The
  per-service reference values in Section 1 exist so such a mechanism can
  reuse them later; this plan only measures and displays.
- **Per-service metric aggregation in the Backend.** Metrics Manager owns
  collection, aggregation, and histograms for per-microservice metrics (see
  Section 3). The Backend only ingests what Metrics Manager provides, as it
  does today for hardware telemetry.

Design rules, from [AGENTS.md](../../../AGENTS.md):

- Open Edge Platform microservices stay unmodified and pinned. Missing data is
  requested upstream, never patched locally.
- The Backend owns collection and aggregation. The browser receives data only
  through the Backend.
- The Backend stays out of the video path. Video timing travels as metadata.
- All new endpoints stay behind the existing loopback/NGINX boundary.

## 1. KPIs, targets, and what the user experiences

| KPI | Default target | What the user experiences | Measured from → to | Phase |
| --- | --- | --- | --- | --- |
| Time to ready (Dashboard TTFV) | < 5 min | "How long until the console is usable after I start it?" | `make up` / `make demo-up` invoked → Panel shows live video and the assistant reports ready. Images present and models already prepared; pulls, downloads, conversion, compilation, and license prompts excluded. | A (deferred) |
| Time to alert | < 400 ms | "How quickly am I told something appeared?" | Decoded frame enters the pipeline → alert event created by the Backend. | A |
| Time to spoken answer | < 3 s | "After I stop talking, how long until I hear the answer?" | End of the operator's speech → first audible sound of the spoken answer. | A |
| Smooth cameras | ≥ 3 at 1080p | "Can I watch all my cameras without stutter or delay?" | Per camera: processed FPS vs. source FPS, frame drops, and time to alert over a rolling window. | A |
| Answer speed (LLM decode tokens/s, = 1 / TPOT) — stretch | > 15 tokens/s (7B, 4-bit) | "Does the answer stream faster than I can read it?" | Per request: (output tokens − 1) ÷ (first generated token → last generated token). A per-request decode speed, not aggregate throughput. | B (A shows an approximate words/s) |
| Compute busy | > 70 % under sustained load | "Is the hardware doing work, or idling while I wait?" | Host CPU utilization averaged over a sustained-load window. | A (existing Resources tab) |
| Memory headroom | < 80 % at peak | "Am I close to running out of memory?" | Peak host RAM vs. capacity over the rolling window. | A |

### Industry standards behind the KPIs

The seven operator KPIs are plain-words roll-ups, not named standards. Each is
built from standard signals so it can be explained and drilled into:

| Operator KPI | Standard parts |
| --- | --- |
| Time to spoken answer | ASR latency + LLM time to first token (TTFT) + TTS time to first audio, plus the silence wait |
| Time to alert | Pipeline latency (decode → detection) + rule and dispatch time |
| Smooth cameras | Processed FPS vs. source FPS + dropped-frame ratio |
| Answer speed | Decode tokens/s (1 / time per output token) |
| Compute busy, Memory headroom | USE method: utilization and saturation |
| Time to ready | Every required service passes its health check, then first live frame |

Per service, show the same few signals (RED: rate, errors, duration; plus
saturation from the Google SRE golden signals): requests, error rate, latency
p50/p95, and queue wait or in-flight count. Latency of failed requests is kept
apart from successful ones. Domain signals sit on top: TTFT and decode
tokens/s for the LLM, real-time factor (RTF) for ASR and TTS, FPS and drops for
video.

### Per-service tiles

One tile per service, one plain-words number. Reference values are starting
guidance from datacenter-class sources, **not evaluated** by this plan (no
pass/fail on these tiles); a later CI or test gate can adopt them. Calibrate on
the device first.

| Service | Tile says | Standard name | Reference value |
| --- | --- | --- | --- |
| Chat Q&A | "First words in 0.8 s" | TTFT p95 | < 2 s |
| Audio Analyzer | "Speech heard 2× faster than real time" | RTFx (RTF = 1 / RTFx) | > 1, aim ≥ 2 |
| Text To Speech | "Voice ready in 0.4 s" | Time to first audio; RTF | RTF < 1.0 |
| DL Streamer | "Camera 1: 29.8 of 30 fps" | Processed vs. source FPS; dropped % | ≥ 90 % of source; < 10 % dropped |
| Alert Agent | "Alert delivered in 190 ms" | Detection → dispatch p95 | < 400 ms |

Errors, queue wait, and p99 go in each tile's detail line. p99 appears only
with at least 100 samples. WER (ASR accuracy) is an offline benchmark value, not
a live tile.

### Measurement rules

- Latency is reported as the last value and the p50/p95 of a rolling window,
  with the sample count. Percentiles are never averaged or added. p99 is shown
  only in the drill-down and only with enough samples (at least 100); at
  handheld request volumes it is too noisy for a headline. Averages are fine
  for throughput values such as FPS and tokens/s, never for latency.
- A value with too few samples shows "Not enough data", not a pass.
- Intervals inside one process use that process's monotonic clock. Intervals
  that cross containers use the host wall clock (all containers share one
  kernel clock on a single device). Browser intervals use the browser's
  monotonic clock only; browser and server timestamps are never subtracted.
- Utilization and memory targets only make sense under load. Idle time is never
  shown as a failure.
- Every saved result records hardware, driver versions, application revision,
  image digests, model revisions and precision, device placement, camera
  count, and duration.

### Time to spoken answer — definition

This is the main UX metric, so its boundaries are fixed:

- **Start:** the operator stops speaking. With tap-to-send this is the second
  tap. With auto-send it is the last audio buffer above the voice-activity
  threshold, not the moment auto-send fires.
- **End:** the browser `playing` event of the first answer clip, not when the
  WAV is ready.
- **Included:** silence-detection wait, upload, transcription, assistant
  routing and generation, synthesis of the first answer chunk, delivery, and
  playback start.
- **Excluded from the target:** the "Checking." acknowledgement (shown
  separately as "Acknowledged in"), and turns where audio is off or playback
  was blocked (counted, but not timed).

The current auto-send waits 1.5 s of silence, which uses half the 3 s budget.
The Panel shows this as its own "Listening" stage, so its cost is visible.
Tuning that delay is a product decision outside this plan.

## 2. Phase A — application-owned instrumentation

Everything in this phase lives in the Handheld component: Backend, browser,
and the application's own DL Streamer `gvapython` callbacks. No Open Edge
Platform service is modified.

### Implementation status

| Item | Status | Where |
| --- | --- | --- |
| A1. Video: ingress stamp, pipeline latency, FPS and drop counters, camera health, time to alert | Implemented | `scripts/dlsps/frame_ingress.py`, `crop_candidates.py`, `config/dlsps/*/config.json`, `backend/app/{ingest,alerts,performance}.py` |
| A2. Voice: time to spoken answer and its four stages, measured in the browser | Implemented | `backend/app/static/index.html`, `POST /api/performance/marks` |
| A3. Approximate answer speed (words/s) and time to first words | Implemented | `backend/app/assistant.py` → collector |
| A5. Host memory peak against the 80 % target | Implemented | collector reads existing telemetry |
| A6. Collector, `GET /api/performance`, `performance` SSE event, marks endpoint | Implemented | `backend/app/performance.py`, `backend/app/main.py` |
| Panel: video badge, answer footer, alert tooltip, Resources \| Performance tab | Implemented | `backend/app/static/index.html` |
| A4. Deployment time to ready | Deferred | — |
| Backend `/metrics` export, measurement runs, `make perf`, saved scorecards | Not planned here | — |
| Expanded Performance view, model-strip chips, header "Ready in" | Later | — |
| Browser `alert_shown` marks, WebRTC rendered FPS and frozen frames | Deferred | — |
| Backend-side ASR/TTS stage timings and real-time factor | Deferred (browser stages cover the UX need; exact values come from B2/B3) | — |

Targets live in `config/backend/backend.yaml` under `performance_targets`
(`time_to_alert_seconds`, `spoken_answer_seconds`, `smooth_cameras`,
`tokens_per_second`, `memory_percent`). Invalid values fall back to the
defaults above.

### A1. Video: time to alert and FPS counters

The pipelines in `config/dlsps/{npu,gpu,cpu}/config.json` are owned by the
application and already ran the application callback
[`crop_candidates.py`](../../../scripts/dlsps/crop_candidates.py) after
`gvametaconvert`. That callback runs after detection and tracking, so on its
own it cannot see pipeline latency or frames dropped by the leaky queues.

**Ingress callback.** A new application `gvapython` element,
[`frame_ingress.py`](../../../scripts/dlsps/frame_ingress.py) (`FrameIngress`),
sits after the source and before the first leaky `queue`:

```text
{auto_source} ! … ! gvapython name=ingress module=/opt/handheld-crops/frame_ingress.py class=FrameIngress
  ! queue … leaky=downstream ! gvadetect … ! gvatrack … ! gvametaconvert … ! gvapython name=cropcandidates … ! …
```

For each frame it increments a per-pipeline counter and attaches one JSON
message, `{"handheld_ingress": {"wall": time.time(), "mono_ns": time.monotonic_ns(), "sequence": n}}`.
The message travels with the frame buffer, so no PTS or FIFO pairing is
needed, and frames dropped by a leaky queue simply never arrive. It never maps
pixel data.

**`crop_candidates.py`** removes that message (otherwise DLSPS would merge it
into the published payload) and adds to `crop_context`:

| Field | Value |
| --- | --- |
| `ingress_at` | Host wall-clock seconds when the decoded frame entered the pipeline. |
| `pipeline_ms` | Ingress → this callback, on the shared monotonic clock (both callbacks run in one DLSPS process). |
| `frames_in` | Ingress counter at this frame. |
| `frame_sequence` | Existing per-instance counter; used as frames out. |
| `source_fps` | `video_info().fps_n / fps_d`, only when both are positive. |

Counts are cumulative per tracking session; the Backend derives rates from
deltas, so a lost MQTT message does not break them. Verified in the
stock `intel/dlstreamer-pipeline-server` image (tag pinned in Compose) with a real
GStreamer pipeline (one merged message per frame, stamp removed,
`pipeline_ms` ≈ 1 ms on `videotestsrc`), and covered offline by
`make test-crops`.

**Backend** ([`ingest.py`](../../../backend/app/ingest.py) `_on_message` →
[`performance.py`](../../../backend/app/performance.py) `Performance.frame`)
keeps per camera:

| Value | Formula | Window |
| --- | --- | --- |
| Processed FPS | `Δframe_sequence / Δt` | 5 s |
| Ingress FPS | `Δframes_in / Δt` | 5 s |
| Skipped frames | `1 − Δframe_sequence / Δframes_in` | 5 s |
| Detection delay | Backend receive wall time − `ingress_at` (0–60 s accepted) | 30 s |
| Pipeline latency | `pipeline_ms` | 30 s |
| Health | Offline (no frames for 5 s), Measuring, Dropping (> 10 % skipped, or processed < 90 % of source FPS), Delayed (detection delay p95 over the time-to-alert target), Smooth | — |

A new tracking session, or a counter that goes backwards, restarts the
camera's counts.

**Time to alert** = alert raised (wall clock) − `ingress_at` of the frame that
first saw the object. [`alerts.py`](../../../backend/app/alerts.py) records it
only for alerts raised on that first observation by a rule without dwell, so a
rule that intentionally waits, an object that later enters a zone, or a
confidence that rises later is not counted as a slow pipeline. Every alert
still carries its own `latency.time_to_alert` for display.

**Not used:** DLSPS `avg_pipeline_latency`/`frame_latency` pair a source-pad
probe with an appsink probe through a FIFO; with leaky queues, dropped frames
leave unmatched entries and the value drifts. `avg_fps` is lifetime, not live.

### A2. Voice: time to spoken answer and stages

Measured entirely in the browser on one monotonic clock
(`performance.now()`), so no cross-machine clock is subtracted.

| Mark | When |
| --- | --- |
| `speechEnd` | Last loud audio buffer when auto-sent after silence; the tap when sent manually. |
| `sent` | Upload to `/api/voice/transcribe` starts. |
| `heard` | Transcription returned with text. |
| `firstText` | First streamed answer text (`delta` or `answer`) from `/api/ask`. |
| `playing` | `playing` event of the first answer clip that is not the "Checking." cue. |

Stages: **Listening** = sent − speechEnd (includes the 1.5 s silence wait),
**Hear** = heard − sent, **Think** = firstText − heard, **Speak** = playing −
firstText. Marks are clamped to be non-decreasing. The "Checking." cue time is
kept separately as `ack_ms`; the speech snapshot now marks that clip with
`ack: true`.

The browser posts one mark per voice turn:

```json
POST /api/performance/marks
{"kind": "voice_turn", "outcome": "spoken", "spoken_answer_ms": 2410,
 "ack_ms": 820, "stages": {"listening": 1540, "hear": 310, "think": 420, "speak": 140}}
```

`outcome` is `spoken`, `blocked` (browser blocked audio), or `failed`
(transcription or answer failed, playback error, or no answer audio within
30 s). Only `spoken` carries timings; the others are counted as "not timed".
Turns with Audio off or cancelled by the operator are not posted. The Backend
validates the body strictly (known keys only, whole milliseconds 0–120 000)
and returns 422 otherwise.

### A3. LLM speed without upstream changes

[`assistant.py`](../../../backend/app/assistant.py) `_chat_stream` already
timed `first_text_ms` and `generation_ms`; it now reports each streamed answer
to the collector:

- **Time to first words** = first streamed text (client-observed TTFT).
- **Answer speed** = words ÷ (generation − first text), only for answers with
  at least 5 words streamed over at least 0.2 s. Shown as "~N words/s".

Text chunks are not tokens and the Backend has no tokenizer, so the Panel
never shows tokens/s and never compares words/s against the 15 tokens/s
target. It shows "Tokens/s: needs Chat Q&A update" until B1 ships.

### A4. Deployment time to ready (deferred)

Proposal kept for later: Make `up`/`demo-up` write a start marker after
prerequisite checks; the Backend records when each service first passes its
readiness check; the browser marks the first live frame. Time to ready =
ready − start, excluding image pulls and model preparation.

### A5. Hardware

The collector reads the existing 1 Hz telemetry snapshot
([`telemetry.py`](../../../backend/app/telemetry.py)) and keeps host
`memory_used_percent` samples (deduplicated by sample time) for the peak. Host
CPU and per-container CPU/RAM stay on the Resources tab. Per-service GPU/NPU
usage and device memory are out of scope.

### A6. Collector and endpoints

[`performance.py`](../../../backend/app/performance.py) holds bounded rolling
windows (5 minutes, at most 2000 samples each, at most 64 cameras). Summaries
are last, p50, p95 (nearest rank), and count. Status is `on_target` (p95 below
target), `missed`, `insufficient` (fewer than 5 samples), `no_data`, or `info`
(no target). Too few samples is never a pass.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/performance` | Versioned snapshot: `{version, window_seconds, generated_at, kpis, cameras}` |
| `performance` event on `/api/stream` | Same snapshot, with the other live events (about 1 Hz) |
| `POST /api/performance/marks` | Browser voice-turn marks (above) |

`kpis` contains `time_to_alert` (+ `detection_delay_p50`), `spoken_answer`
(+ stage p50s, `acknowledged_p50`, `untimed`), `cameras`
(`smooth`, `running`, `target`, `status`), `answer_speed` (words/s,
`approximate: true`, `first_words_p50`, `tokens_per_second: null`,
`token_target`), and `memory` (`last`, `peak`, `target`). `cameras` maps each
enabled stream to health, reason, FPS values, skipped %, and delay summaries.
All routes sit behind the existing NGINX boundary; no transcripts or questions
are stored.

## 3. Phase B — upstream requests (hand-off specification)

Source checked: [`open-edge-platform/edge-ai-libraries`](https://github.com/open-edge-platform/edge-ai-libraries)
at tag `v2026.3.0-rc1`, the source of the stock images pinned in Compose. File
references below are relative to that tag. Each "Current state" was re-checked
against the previous tag (`v2026.2.0-rc3`); changes are listed under
**New in this release**. None of the requested values below is exposed yet.
Confirm against the released image source before implementation.

### Minimum request (ask for this first)

Keep the upstream ask small. Each service needs only these, exposed on
`GET /metrics` (Prometheus text, internal port) for Metrics Manager to scrape:

| Service | Minimum set |
| --- | --- |
| Chat Q&A | Queue wait, time to first token, output tokens and decode time (gives tokens/s), request duration, requests by `outcome`, in-flight |
| Audio Analyzer | Request duration, audio duration (gives RTFx), requests by `outcome`, in-flight |
| Text To Speech | Request duration, time to first audio, audio duration (gives RTF), requests by `outcome`, in-flight |

Everything else listed under B1–B3 (retrieval and rerank time, TPOT, headers,
per-request JSON, model-info fields, load and warmup durations, INT4 option) is
an optional extra: useful, but not required for the Panel tiles. Derived
values (TPOT, tokens/s, RTF) are computed from the minimum set when not
supplied.

### Common contract for all services

Same shape for every service, so consumers integrate once:

1. **Backward compatible.** Existing responses stay unchanged; new data is
   additive or opt-in.
2. **Durations in seconds as histograms**, totals as counters, in-flight as a
   gauge, on `GET /metrics`.
3. **Bounded labels only:** `model`, `device`, `precision`, `outcome`. Never
   request IDs, text, filenames, or session IDs.
4. **Queue wait** measured from before lock acquisition to after it, so
   queueing is not mistaken for slow inference.
5. **Requested vs. verified precision** reported separately where a service
   reports its model.

### B1. Chat Q&A Core — exact LLM token metrics (highest priority)

**Current state**

- `POST /v1/chatqna/chat` takes `{input, stream=true}`
  ([`app/server.py`](https://github.com/open-edge-platform/edge-ai-libraries/blob/v2026.3.0-rc1/sample-applications/chat-question-and-answer-core/app/server.py)).
  Streaming yields raw `data: {chunk}\n\n` frames with no event name, usage, or
  timing (`app/chain.py` `process_query`). Non-streaming returns
  `{"status", "metadata"}` and only logs elapsed time.
- Generation uses LangChain `HuggingFacePipeline.from_model_id(backend="openvino")`
  (`app/openvino_backend.py`), not `openvino_genai.LLMPipeline`, so OpenVINO
  GenAI `PerfMetrics` (TTFT, TPOT, throughput) is not available at the call site.
  The tokenizer is reachable as `llm.pipeline.tokenizer`.
- On CPU and GPU, the LLM is exported with a fixed `weight_format="int8"`
  (`app/openvino_backend.py` `OVModelForCausalLM.from_pretrained`). **The 4-bit
  stretch profile cannot be deployed on CPU or GPU** (see the NPU path below).
- RAG is `retriever (FAISS) → optional ContextualCompressionRetriever (reranker)
  → prompt → llm → StrOutputParser` (`app/chain.py` `build_chain`, `get_retriever`).
- `GET /model` returns only the configured model ID. No `/metrics`, Prometheus,
  or OpenTelemetry.

**New in this release**

- A process-wide `_generation_lock` (`app/chain.py`) now serializes generation,
  so concurrent questions queue implicitly. The wait is not measured, which
  makes `queue_wait_s` more important: without it, queue time shows up as TTFT.
- Streaming generation is cancelled when the client disconnects.
- With sources enabled, streaming sends `event: sources` before the answer
  `data:` frames; non-streaming adds `sources`. There is still no usage or
  timing event, so the opt-in metrics event below is still needed.
- **NPU path:** with the LLM on NPU, the model is exported as int4 channel-wise
  and runs through OpenVINO GenAI (`GenAILLM`, `app/openvino_xpu.py`) with a
  prompt-token limit (`NPU_MAX_PROMPT_LEN`). `GenAILLM` has a `count_tokens`
  helper (used for prompt fitting) and streams through a text callback, so
  exact token counts and GenAI `PerfMetrics` are reachable on this path but
  not returned. CPU and GPU keep the `HuggingFacePipeline` int8 path, so the
  4-bit stretch profile is still only possible on NPU.
- The image is now a single `core` image for all devices; the upstream Compose
  service is `chatqna-core-openvino`.

**Requested values**

| Value | Definition | How to compute |
| --- | --- | --- |
| `input_tokens` | Tokens in the fully rendered prompt (system + context + question) | `len(tokenizer(rendered_prompt).input_ids)` |
| `output_tokens` | Generated tokens, excluding the echoed prompt | Token IDs from the generation streamer, or `len(tokenizer(answer, add_special_tokens=False).input_ids)` |
| `queue_wait_s` | Request arrival → generation admitted | Timestamp before and after acquiring the existing `_generation_lock` in `app/chain.py` |
| `retrieval_s` | Query embedding + FAISS search | Wrap the base retriever (`Runnable` timing or LangChain callback `on_retriever_start/end`) |
| `rerank_s` | Reranker compression | Wrap the compressor separately |
| `ttft_s` | Generation start → first generated token | Timestamp of the first token from the streamer, measured from the LLM call start (exclude retrieval) |
| `decode_s` | First token → last token | Streamer timestamps |
| `tpot_s` | Mean time per output token after the first | `decode_s / (output_tokens − 1)`, only when `output_tokens ≥ 2` |
| `decode_tokens_per_s` | Decode throughput | `(output_tokens − 1) / decode_s` |
| `e2e_tokens_per_s` | Whole-response throughput | `output_tokens / (ttft_s + decode_s)` |
| `request_s` | Request received → last byte | Monotonic, in the route handler |

Two implementation options:

1. **Minimal:** keep LangChain; attach a callback handler that timestamps
   `on_llm_new_token` and counts tokens with the pipeline tokenizer. Token
   boundaries from `TextIteratorStreamer` are decoded text pieces, so recount
   the final answer with the tokenizer for the exact `output_tokens`.
2. **Preferred long-term:** generate with `openvino_genai.LLMPipeline` and
   return its `PerfMetrics` (`get_ttft`, `get_tpot`, `get_throughput`,
   `num_generated_tokens`, `num_input_tokens`). Exact, runtime-measured, and
   lower overhead.

**Requested exposure**

- **Streaming, opt-in:** request field `"stream_options": {"include_metrics": true}`
  (mirrors OpenAI `include_usage`). After the last text frame, emit
  `event: metrics\ndata: {"usage": {...}, "timing": {...}, "model": {...}}\n\n`.
  Opt-in is required because current clients concatenate every `data:` frame
  into the answer text.
- **Non-streaming:** add sibling fields `usage` and `timing` next to
  `metadata`.
- **`GET /metrics`:** histograms `chatqna_queue_wait_seconds`,
  `chatqna_retrieval_seconds`, `chatqna_rerank_seconds`,
  `chatqna_ttft_seconds`, `chatqna_tpot_seconds`,
  `chatqna_decode_tokens_per_second`, `chatqna_request_seconds`; counters
  `chatqna_input_tokens_total`, `chatqna_output_tokens_total`,
  `chatqna_requests_total{outcome}`; gauges `chatqna_requests_in_flight`,
  `chatqna_requests_waiting`.
- **`GET /model`:** add `runtime`, `device` (LLM, embedding, reranker),
  `weight_format_requested`, `weight_format_verified` (from the exported
  model's `openvino_config.json` / IR `rt_info` / weight element types),
  `model_revision`, `load_seconds`, `warmup_seconds`.
- **Configuration:** `LLM_WEIGHT_FORMAT` (`int8` default, `int4`, `fp16`)
  for CPU and GPU, with INT4 group size/ratio, so the stretch profile can be
  deployed off NPU.

**Handheld use:** opts into `include_metrics`, replaces the approximate words/s
with exact tokens/s, compares against the 15 tokens/s target only when
`weight_format_verified` is 4-bit, and uses `ttft_s` and `retrieval_s` in the
"Think" stage.

### B2. Audio Analyzer — ASR stage timings

**Current state**

- `POST /v1/audio/transcriptions` (OpenAI-compatible; `response_format`
  `json` (default, `{"text"}`), `verbose_json` (with `duration` and
  `segments`), `text`, `srt`, `vtt`; `stream=true` sends SSE text deltas and a
  final transcript event for `json`/`verbose_json`)
  ([`api/openai_endpoints.py`](https://github.com/open-edge-platform/edge-ai-libraries/blob/v2026.3.0-rc1/microservices/audio-analyzer/api/openai_endpoints.py)).
- Uses `openvino_genai.WhisperPipeline`; audio loaded with `soundfile`,
  resampled with `librosa`; `generate()` is serialized by a module-level
  `_TRANSCRIBE_LOCK` (`components/asr/openvino_genai/whisper.py`). No
  `result.perf_metrics` is read.
- `/v1/performance` returns only the **last** ASR latency in ms
  (`utils/latency_store.py`); with several clients it cannot be attributed to
  a request. `/v1/model-info` reports configured `weight_format`, not verified
  precision. Load and warmup are logged without durations. No `/metrics`.

**New in this release**

- `POST /v1/audio/transcriptions` now honors an optional per-request `device`
  (`CPU`, `GPU`, `NPU`); unsupported or unavailable devices return 400. A
  `diarization` field can turn diarization off per request. Because the device
  can now vary per request, the `performance` object below must include the
  resolved `device`.
- The VSS-style job route rejects a `model_name` that does not match the
  configured model (400) instead of logging a warning.
- English-only (`.en`) Whisper checkpoints drop the `language` hint instead of
  failing.
- The realtime WebSocket `/v1/realtime` adds `input_audio_buffer.preview`
  (a throwaway transcript of the trailing ~2 s while the user is still
  speaking), `sequence` numbers on transcript events, and a `low_latency`
  session mode in which a full-buffer preview can be reused by the commit.
  `config.yaml` can define a smaller `preview:` ASR model. These lower the
  perceived ASR delay for clients that stream microphone audio; the Handheld
  uploads one bounded WAV per utterance, so they do not apply today.
- The Handheld client (`model=whisper-1`, `response_format=json`, `/health`)
  is unaffected.

**Requested values**

| Value | Definition |
| --- | --- |
| `queue_wait_s` | Before → after acquiring `_TRANSCRIBE_LOCK` |
| `preprocess_s` | Decode + resample + channel mix |
| `inference_s` | `WhisperPipeline.generate()` duration; if the installed GenAI version provides `result.perf_metrics`, also `features_extraction_s` and `ttft_s` |
| `postprocess_s` | Segment/text assembly |
| `audio_duration_s` | Decoded samples ÷ sample rate (not last segment end) |
| `rtf` | `(preprocess_s + inference_s + postprocess_s) / audio_duration_s` |
| `request_s` | Route entry → response |

**Requested exposure**

- JSON responses: optional `performance` object with the values above, enabled
  by a request flag or always present in `verbose_json`.
- All non-streaming responses: `Server-Timing: queue;dur=…, pre;dur=…, infer;dur=…, post;dur=…`
  (milliseconds) and `X-Audio-Duration-Seconds`.
- Streaming (SSE/NDJSON): a final event with the same `performance` object.
- `/metrics`: histograms `audio_analyzer_queue_wait_seconds`,
  `audio_analyzer_preprocess_seconds`, `audio_analyzer_inference_seconds`,
  `audio_analyzer_postprocess_seconds`, `audio_analyzer_audio_duration_seconds`,
  `audio_analyzer_rtf`; counter `audio_analyzer_requests_total{outcome}`; gauge
  `audio_analyzer_requests_in_flight`; gauges `audio_analyzer_model_load_seconds`,
  `audio_analyzer_warmup_seconds`.
- `/v1/model-info`: add `runtime`, `device`, `precision_verified`.

**Handheld use:** splits the "Hear" stage into queue, preprocessing, and model
time, and shows model-only RTF instead of API RTF.

### B3. Text To Speech — TTS stage timings

**Current state**

- `POST /v1/audio/speech` returns a fully buffered WAV (or JSON with base64)
  ([`api/openai_endpoints.py`](https://github.com/open-edge-platform/edge-ai-libraries/blob/v2026.3.0-rc1/microservices/text-to-speech/api/openai_endpoints.py)).
- SpeechT5 runs through `openvino_genai.Text2SpeechPipeline`; text
  normalization is `normalize_for_speech`; `generate()` is serialized by an
  inference lock (`components/tts/openvino/speecht5.py`). Acoustic model and
  vocoder run inside one GenAI call, so they cannot be timed separately without
  runtime support.
- `/v1/performance` returns only the last latency in ms (`pipeline.py`,
  `utils/latency_store.py`). Startup warmup is logged without duration. No
  `/metrics`.

**New in this release**

- `POST /v1/audio/speech/stream`
  ([`api/streaming_endpoints.py`](https://github.com/open-edge-platform/edge-ai-libraries/blob/v2026.3.0-rc1/microservices/text-to-speech/api/streaming_endpoints.py))
  returns SSE `data:` events, each a JSON object with `index`, `duration`,
  `sampling_rate`, `voice` and one phrase as base64 WAV, then `[DONE]`.
  `POST /v1/audio/speech` is unchanged.
- Only backends with incremental decoding (Kokoro) emit several phrases.
  SpeechT5, which the Handheld uses, emits **one** event after full synthesis,
  so the stream does not reduce time to first audio for the Handheld today.
- For streamed requests, `/v1/performance` now records latency to the first
  chunk instead of total synthesis. It is still a last-value store, not per
  request.
- The optional streaming request below is therefore **partly met**: the
  transport exists. Still missing are per-phrase streaming for SpeechT5 and
  `time_to_first_audio_s` in the first event.

**Requested values**

| Value | Definition |
| --- | --- |
| `queue_wait_s` | Before → after acquiring the inference lock |
| `normalize_s` | `normalize_for_speech` duration |
| `generate_s` | `Text2SpeechPipeline.generate()` duration |
| `encode_s` | WAV encoding |
| `audio_duration_s` | Output samples ÷ sample rate |
| `rtf` | `generate_s / audio_duration_s` |
| `chars_per_s` | Normalized input characters ÷ `generate_s` |

**Requested exposure**

- Response headers (body is binary): `Server-Timing: queue;dur=…, norm;dur=…, gen;dur=…, enc;dur=…`,
  `X-Audio-Duration-Seconds`, `X-Real-Time-Factor`; add them to CORS
  `expose_headers`.
- `/metrics`: histograms `tts_queue_wait_seconds`, `tts_generate_seconds`,
  `tts_audio_duration_seconds`, `tts_rtf`; counters `tts_requests_total{outcome}`,
  `tts_input_characters_total`; gauge `tts_requests_in_flight`; gauges
  `tts_model_load_seconds`, `tts_warmup_seconds`.
- `/v1/model-info`: add `precision_verified` and load/warmup durations.
- **Optional, highest UX value:** phrase-level output on the existing
  `/v1/audio/speech/stream` for SpeechT5: split normalized text into sentences
  and send each sentence's audio as soon as it is ready, with
  `time_to_first_audio_s` and the `performance` values in the events. The
  Handheld arbiter already splits answers into chunks, so this mainly helps
  longer first sentences.

**Handheld use:** splits the "Speak" stage into queue wait and synthesis, and
shows TTS RTF from the service.

### B4. DL Streamer Pipeline Server — optional improvements

Phase A covers the Handheld needs with application callbacks. These upstream
changes would benefit all DLSPS users and remove the need for the ingress
callback:

**Current state** ([`src/server/gstreamer_pipeline.py`](https://github.com/open-edge-platform/edge-ai-libraries/blob/v2026.3.0-rc1/microservices/dlstreamer-pipeline-server/src/server/gstreamer_pipeline.py)):
status has `avg_fps` (lifetime), `frame_fps` (about 1 s window), and, when
probes match, `avg_pipeline_latency`/`frame_latency`. Latency pairs a probe on
the source element's pad with an appsink probe through a FIFO, which
mispairs when any queue drops frames or when source buffers are not one per
frame. OpenTelemetry export exists with only `cpu_usage_percentage`,
`memory_usage_bytes`, and `fps_per_pipeline` (`src/opentelemetry/opentelemetryexport.py`).
No relevant change in this release.

**Requested**

| Value | Definition | Exposure |
| --- | --- | --- |
| `frames_in_total`, `frames_out_total`, `frames_dropped_total` | Counted after decode, at appsink/publish, and from leaky-queue `overrun` signals | Status fields + OTel counters |
| `fps_window` | `Δframes_out / Δt` over a configurable window (default 5 s), plus window length | Status field |
| `pipeline_latency` | Decode → publish, paired by a per-buffer custom meta or frame ID instead of a FIFO | Status p50/p95 + OTel histogram `dlsps_pipeline_latency_seconds` |
| `inference_latency` | Per `gvadetect`/`gvaclassify` element processing time | OTel histogram `dlsps_element_latency_seconds{element}` |
| `ingress_unix_ns`, `frame_seq` | Optional fields in published metadata. The Handheld `FrameIngress` callback already adds `ingress_at` and `frame_sequence`; a built-in option would remove that application callback. | `gvametaconvert` property, e.g. `add-timing=true` |

### B5. Alert Agent — optional

`POST /alerts` and `POST /actions/execute` exist; `duration_ms` covers only the
action execution, and `/metrics` is explicitly 404
([`src/main.py`](https://github.com/open-edge-platform/edge-ai-libraries/blob/v2026.3.0-rc1/microservices/alert-agent-service/src/main.py)).
Request: an `alert_agent_ingest_to_dispatch_seconds` histogram (route entry →
dispatch completion) with `{mode, outcome}` labels, plus request counters.
Handheld already measures its own dispatch call, so this is low priority. No
relevant change in this release.

### Metrics Manager: the aggregation path

Per-microservice metrics follow the same route as hardware telemetry:
**service `/metrics` (or pushed custom metrics) → Metrics Manager → Backend →
UI**. Metrics Manager is Telegraf-based, accepts pushed custom metrics (JSON,
InfluxDB line protocol, OTLP), and re-exports Prometheus text. Duration series
from B1–B3 arrive as Prometheus histograms; Metrics Manager is where any
histogram handling or re-aggregation belongs, not the Backend.

- Handheld change: enable scraping of each service's `/metrics` in
  `config/metrics/telegraf.conf` (the `[[inputs.prometheus]]` block is present
  but commented out) once the service ships the endpoint.
- Backend change: read the new series in `telemetry.py` the way it reads the
  existing ones, keep stale/missing/zero distinct, and relay them. No
  per-service windows, percentiles, or call timing in the Backend.
- To confirm before implementation: whether the pinned Metrics Manager image
  can produce histogram quantiles (for example via a Telegraf histogram
  aggregator) or only passes raw buckets through. If only buckets, p50/p95 are
  derived from bucket counts at the Backend read step, which is a read-side
  calculation, not collection.

### Not requested

- **Metrics Manager changes:** existing host CPU/GPU/NPU and per-service
  CPU/RAM are sufficient as-is; only scrape configuration is added above.
- **Model Download, MediaMTX, Coturn, Mosquitto:** no KPI gap for this plan.

## 4. How the KPIs appear in the Panel

### Principles

1. **Answer the operator's question where they look.** Time to alert sits on
   the video; time to spoken answer sits under the answer; camera health sits
   in the camera list. The expanded view is for detail, not the main view.
2. **Plain words first, numbers second.** "Answered aloud in 2.4 s" beats
   "e2e p95 2412 ms". Units are always visible.
3. **Status by text and icon, not color alone:** ✓ On target, ! Slow,
   … Not enough data, ~ Approximate, — Unsupported, ◷ Stale.
4. **Never cover the scene or interrupt work.** No pop-ups; updates are quiet;
   screen readers are not spammed at 1 Hz.
5. **Show why, on demand.** Every slow value opens a stage breakdown that names
   the slowest stage.
6. **Be honest about precision of data.** Approximate values carry "~" and a
   tooltip naming the upstream update that makes them exact.
7. **One tile per service, one headline number.** Each service shows a single
   plain-words number, such as "First words in 0.8 s" or "2× faster than real
   time", with "Not available yet" when its series is absent (never a
   synthesized value). The standard name (TTFT, RTF) appears in small text
   beside it. No pass/fail judgement is shown for per-service tiles.
   Histograms, p99, queue, and token-level values stay in the detail view.
   Validate the wording with operators.
8. **Nothing to configure, nothing to learn.** The default view is the tiles
   only. Operators never pick metrics, windows, or percentiles. A tile that has
   no data says so in words, and every number has its unit.

### Placement map

Items 3–6 are implemented; items 1–2 are deferred.

```text
┌ Header ───────────────────────────────────────────────────────────────┐
│ Field console  [Cameras 3 ✓]  Talk  Mission  Alerts  Audio    ● Ready  │ ← (1)
├ Model strip ──────────────────────────────────────────────────────────┤
│ Architecture | YOLO26 NPU 30 fps | Whisper GPU 0.2×RT | LLM GPU ~9 w/s │ ← (2)
├ Live camera ────────────────────────┬ Evidence / Assistant ───────────┤
│ ┌───────────────────────────────┐   │ Q: Any vehicles at the gate?     │
│ │ Detect 120 ms · 30/30 fps     │ ← (3)  A: Two vehicles ...         │
│ │            (video)            │   │                                  │
│ └───────────────────────────────┘   │ 🔊 Answered aloud in 2.4 s ✓  ▸  │ ← (4)
│ Alerts: Person · North gate · 190 ms │                                  │ ← (5)
├ Resources | Performance ────────────┤                                  │
│ Alert  Spoken  Cameras  Speed  Mem  │                                  │ ← (6)
└─────────────────────────────────────┴──────────────────────────────────┘
```

1. **Header status (`#connection` area).** After a deployment, shows
   "Ready in 2 min 14 s" for one minute, then collapses to "● Ready". The
   Cameras button gains a health icon: ✓ all smooth, ! one or more degraded.
2. **Model strip (`#model-buttons`).** Each model chip gets one small live
   number: detector processed FPS, Whisper real-time factor, LLM speed
   ("~9 w/s" in Phase A, "18 t/s" after B1). The **Model details** panel
   (`#model-details`) gains a Performance block: last/p95 latency, throughput,
   device, precision (requested vs. verified), and that service's CPU and RAM.
3. **Video overlay (`#latency-badge`).** Implemented. For the selected
   camera the existing badge reads "Detect 120 ms · 29.8/30 fps", followed by
   the browser's own video delay estimate when WebRTC stats are available.
   When the camera is not smooth, the badge starts with the reason
   ("Dropping frames", "Delayed") in the warning colour and the tooltip gives
   the detail. While counters warm up it shows "Measuring camera…". Deferred:
   per-tile health dots in the all-cameras grid and health in **Manage
   cameras**.
4. **Assistant answer footer (`#answer-timing`, below
   `#assistant-speech-line`).** Implemented. After each voice turn:
   "Answered aloud in 2.4 s · ✓ within 3.0 s target" (or "! over"). It is a
   `<details>` element; opening it shows a stage bar and legend:

   ```text
   [Listening 1.5 s][Hear 310 ms][Think 420 ms][Speak 140 ms]
   From the end of your speech to the first sound of the answer. Acknowledged after 820 ms.
   ```

   Typed questions show "First words in 0.6 s". A voice turn that could not
   be timed says why: "Spoken answer not timed · Audio is off", "· browser
   blocked audio", or "· no spoken answer was heard". Service-internal stage
   parts (queue, model, retrieval) are added after B1–B3.
5. **Alert rows (`#alerts`).** Implemented. The existing latency chip's
   tooltip and accessible label start with "Time to alert (camera frame to
   alert raised): 210 ms".
6. **Performance tab next to Resources (`#telemetry`).** Implemented. The
   panel heading is a two-button switch, **Resources | Performance**.
   Performance shows five tiles and a per-camera table:

   ```text
   ┌ Time to alert ───┐┌ Spoken answer ───┐┌ Smooth cameras ──┐┌ Answer speed ─────┐┌ Peak memory ─────┐
   │ 350 ms           ││ 2.8 s            ││ 1 of 2           ││ ~9.5 words/s      ││ 52%              │
   │ 95% of 12 alerts ││ 95% of 14 voice  ││ Camera 2: 25% of ││ First words       ││ Now 41% of host  │
   │ · typical 250 ms ││ · typical 2.4 s  ││ frames skipped   ││ typical 800 ms    ││ RAM              │
   │ Target under 400 ││ Target under 3 s ││ Target 3 or more ││ Tokens/s: needs   ││ Target under 80% │
   │ ✓ Meets target   ││ ✓ Meets target   ││ ! Misses target  ││ Chat Q&A update   ││ ✓ Meets target   │
   └──────────────────┘└──────────────────┘└──────────────────┘└───────────────────┘└──────────────────┘
   Camera   | Health          | Frames/s    | Skipped | Detect
   Camera 1 | Smooth          | 29.8 / 30   | 0%      | 120 ms
   Camera 2 | Dropping frames | 22.5 / 30   | 25%     | 200 ms
   ```

   The headline is the p95 over the last 5 minutes; the status line is text
   (✓ Meets target, ! Misses target, Not enough data yet, No data yet,
   Information only) with a coloured left border as a secondary cue. Tile
   tooltips define each value; the Spoken answer tooltip lists typical stage
   times. Sparklines are deferred. Compute busy % stays on the Resources tab.

### Later (not planned here)

Expanded multi-tab Performance view, model-strip chips, header "Ready in",
sparklines, and scripted measurement runs with saved scorecards are possible
follow-ups. They are not part of this plan, and none of them introduces
pass/fail gating.

## 5. Delivery steps

| Step | Deliverable | Status |
| --- | --- | --- |
| **Phase A** | | |
| A1. Contracts | KPI definitions, configurable targets, payload schema | Done |
| A2. Collector and APIs | Rolling windows, `/api/performance`, SSE event, marks | Done; `/metrics` deferred |
| A3. Video counters and latency | `frame_ingress.py`, `crop_context` fields, camera health, time to alert | Done |
| A4. Voice latency | Browser marks and stages, ack timing | Done; Backend-side stage timings deferred |
| A5. LLM approx | Words/s and first-words time | Done |
| A6. Panel UI | Badge, answer footer, alert tooltip, Performance tab | Done; model chips, header, expanded view deferred |
| A7. Deployment time to ready | Make start marker and ready mark | Deferred |
| **Phase B** | | |
| B0. Hand-off | Section 3 filed as requests to each service team | Ready to file |
| B1. Chat Q&A | Minimum set above (exact tokens, TTFT, queue wait, `/metrics`) | Upstream release |
| B2–B3. ASR, TTS | Minimum set above (durations, audio length, `/metrics`) | Upstream release |
| B4–B5. DLSPS, Alert Agent | Optional; not needed for the tiles | Upstream release |
| B6. Adopt | Pin new images; enable the service scrape in Metrics Manager; the Backend reads the new series when present and keeps the Phase A values otherwise | Each upstream release |

Phase B never blocks Phase A; the Panel shows "~" approximate values or
"needs Chat Q&A update" until the upstream value is available.

## 6. Tests

Implemented, in the existing targets (`make test-all`):

- `make test-crops`: ingress stamp consumed and removed; `frames_in` vs.
  `frame_sequence`; `pipeline_ms` and `ingress_at` present; malformed stamps
  ignored; unstamped frames carry no timing.
- `make test-coverage` (`backend/test_backend.py`):
  `test_performance_frame_rates_drops_health_and_session_reset`,
  `test_performance_status_needs_samples_and_valid_targets`,
  `test_voice_marks_are_validated_and_time_the_spoken_answer`,
  `test_time_to_alert_counts_only_alerts_raised_on_appearance`; the live SSE
  batch includes the `performance` event; the speech snapshot flags the
  acknowledgement clip.
- `make test-ui` (`backend/test_ui.py`): badge text from a fixture snapshot;
  Performance tab shows five tiles, "Not enough data" is not a pass, camera
  table and degraded reason; Resources view restores; a voice turn ignores the
  acknowledgement, renders the footer and stages, and posts one valid mark;
  typed questions show first words.

Still to add: reading the new service series, time to ready, and stale-state
handling for the Performance tab.

## References

Handheld:

- [Architecture and constraints](../../../AGENTS.md)
- [Application README](../../../README.md)
- [Telemetry poller](../../../backend/app/telemetry.py)
- [LLM streaming timing](../../../backend/app/assistant.py)
- [Voice upload and transcription](../../../backend/app/voice.py)
- [Speech arbiter](../../../backend/app/speech.py)
- [Routes, TTS callback, and SSE](../../../backend/app/main.py)
- [Observation ingest](../../../backend/app/ingest.py)
- [Alert dispatch timing](../../../backend/app/alerts.py)
- [Pipeline crop callback](../../../scripts/dlsps/crop_candidates.py)
- [Panel UI](../../../backend/app/static/index.html)
- [Edge Workloads and Benchmarks guide](run-edge-benchmarks.md)

Upstream (tag `v2026.3.0-rc1`):

- [Chat Q&A Core](https://github.com/open-edge-platform/edge-ai-libraries/tree/v2026.3.0-rc1/sample-applications/chat-question-and-answer-core)
- [Audio Analyzer](https://github.com/open-edge-platform/edge-ai-libraries/tree/v2026.3.0-rc1/microservices/audio-analyzer)
- [Text To Speech](https://github.com/open-edge-platform/edge-ai-libraries/tree/v2026.3.0-rc1/microservices/text-to-speech)
- [DL Streamer Pipeline Server](https://github.com/open-edge-platform/edge-ai-libraries/tree/v2026.3.0-rc1/microservices/dlstreamer-pipeline-server)
- [Alert Agent](https://github.com/open-edge-platform/edge-ai-libraries/tree/v2026.3.0-rc1/microservices/alert-agent-service)

Standards:

- [OpenAI streaming usage (`stream_options.include_usage`)](https://platform.openai.com/docs/api-reference/chat/create)
- [OpenVINO GenAI performance metrics](https://docs.openvino.ai/2025/openvino-workflow-generative/inference-with-genai.html)
- [W3C Server-Timing](https://www.w3.org/TR/server-timing/)
- [Prometheus histograms](https://prometheus.io/docs/practices/histograms/)
- [Google SRE book: Monitoring Distributed Systems (four golden signals)](https://sre.google/sre-book/monitoring-distributed-systems/)
- [OpenTelemetry HTTP metrics](https://opentelemetry.io/docs/specs/semconv/http/http-metrics/)
- [OpenTelemetry GenAI metrics](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-metrics.md)
- [vLLM metrics design (TTFT, TPOT, queue time)](https://docs.vllm.ai/en/stable/design/metrics/)
- [LLM inference latency overview (chat target guidance)](https://clickhouse.com/resources/engineering/llm-inference-latency)
- [MLPerf inference scenarios](https://mlcommons.org/benchmarks/inference-datacenter/)
