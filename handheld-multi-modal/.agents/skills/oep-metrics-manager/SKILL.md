---
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
name: oep-metrics-manager
description: Use when reading or extending handheld resource telemetry from the stock Open Edge Platform Metrics Manager (CPU, memory, GPU, NPU, power and per-service container metrics).
---

# Metrics Manager

Published container image `intel/metrics-manager` (tag pinned in `compose/handheld-multi-modal.yaml`),
source `edge-ai-libraries/microservices/metrics-manager/`. Compose service
`metrics`; host ports `127.0.0.1:9090` (API), `9273` (Prometheus export) and
`8186` (custom metrics input).

## Deployment

- `privileged`, `pid: host`, `apparmor:unconfined`, `/sys` and `/run`
  read-only, `/dev/dri` and the render group are the upstream service's own
  requirements for host sysfs telemetry.
- `config/metrics/telegraf.conf` adds a `[[inputs.docker]]` block that reads
  `/var/run/docker.sock` (read-only) for per-container CPU and memory of
  running `handheld-*` containers.
- Data flows **Metrics Manager → Backend → UI**
  (`backend/app/telemetry.py`). Do not wire the browser to host telemetry or
  build a separate aggregator.

## API

- `GET :9090/health` is liveness only.
- `GET :9090/api/v1/metrics/latest` mixes second and nanosecond timestamps
  from different sources; normalize per series.
- `GET :9090/metrics/stream` with `Accept: text/event-stream`: samples have
  `name`, `labels`, `value` and `timestamp` (epoch ms).
- `GET :9273/metrics`: Prometheus text export.

## Metrics

- CPU: `cpu_usage_user`, `cpu_usage_system`, `cpu_usage_idle` (%, aggregate).
  `cpu_frequency_avg_frequency_hz` is Hz (displayed as MHz).
- Memory: `mem_used_percent`, `mem_available_percent` (%), `mem_total`,
  `mem_used` (bytes). CPU package temperature: `temp_temp` (°C).
- GPU: `gpu_engine_usage_usage` (%) labelled by engine;
  `gpu_frequency_cur_freq_hz{type="cur_freq"}` (Hz, displayed as MHz);
  `gpu_power_power_w{type="gpu_cur_power"}` (W).
  There is no GPU memory utilization series and no `gpu_usage_percent`.
- NPU: `npu_utilization` (%), `npu_power_w` (W), `npu_temperature_c` (°C),
  `npu_frequency_hz` (Hz), `npu_bandwidth_bytes_per_second` (bytes/s), and
  `npu_memory_bytes` (displayed as MiB; negative readings are unavailable).
  Utilization can read 0 when the driver does not expose NPU busy time.
- Package power: `powerstat_package_current_power_consumption_watts{package_id="0"}`
  (W), with `rapl_power_power_w{domain="package-0"}` when `ENABLE_RAPL=true`
  as an alternative.
  These measure the same package domain; the Backend selects one, never their sum.
- Containers: `docker_container_cpu_usage_percent{cpu="cpu-total"}` (100 =
  one core), `docker_container_mem_usage`, `docker_container_mem_limit`
  (bytes), `docker_container_mem_usage_percent`, and `docker_n_cpus`, which
  the Backend divides by to show a share of the whole machine.
- Package power is not CPU-only or total-system power. Do not add overlapping
  GPU, package and NPU power domains.
- Keep missing, stale, unsupported, sentinel and real-zero values distinct;
  never synthesize a value.

## Per-service series (not served by the pinned images yet)

Chat Q&A, Audio Analyzer and Text To Speech are meant to expose these series;
until the pinned images do, every one reads as `missing`. The one name table is
`SERVICE_METRICS` at the top of `backend/app/telemetry.py`; a rename is a
one-line change there (names from section 3 of
`docs/user-guide/benchmarking/handheld-performance-kpi-plan.md`).

- Chat: `chatqna_ttft_seconds`, `chatqna_queue_wait_seconds`,
  `chatqna_decode_tokens_per_second`, `chatqna_request_seconds`,
  `chatqna_requests_total{outcome}`, `chatqna_requests_in_flight`.
- ASR: `audio_analyzer_inference_seconds`, `audio_analyzer_queue_wait_seconds`,
  `audio_analyzer_audio_duration_seconds`, `audio_analyzer_rtf`,
  `audio_analyzer_requests_total{outcome}`, `audio_analyzer_requests_in_flight`.
- TTS: `tts_generate_seconds`, `tts_audio_duration_seconds`, `tts_rtf`,
  `tts_queue_wait_seconds`, `tts_requests_total{outcome}`,
  `tts_requests_in_flight`.

Shape seen by the Backend: every series is `{name, tags{host,...}, fields{value},
timestamp}`; types are lost. A Prometheus-scraped histogram arrives as flat
`<name>_bucket{le}`, `_sum` and `_count` series, so p50/p95 can be read from the
cumulative buckets (cumulative since service start, not a window). An OTLP push
keeps only `_count`, `_sum`, `_avg`, `_min`, `_max` (no buckets), so p50/p95 stay
null. Series stay on `:9273` up to 60 s after the last write with their original
timestamp, which is how stale is told from fresh.

Scrape block: `config/metrics/telegraf.conf` has a **commented-out**
`[[inputs.prometheus]]` block for the three services' `/metrics` with a `namepass`
list. Enable it only when the pinned images serve `/metrics`. It must keep
`metric_version = 1`: version 2 makes a scraped histogram emit a duplicate `le`
label, `/api/v1/metrics/latest` returns HTTP 500 and all telemetry stops.

Snapshot: the telemetry snapshot (`GET /api/metrics`, SSE `telemetry` event)
carries a top-level `services` key: `{chat, asr, tts}`, each
`{state, metrics{key: {value, unit, state, p50, p95, count}}}` with
`state` of `ok`, `stale` or `missing`. Every key is always present; absent
series are `missing` with `value` null, and a real 0 stays 0. The Backend only
reads and relays these series: no timing of its own calls, no windows, no
outcome counting, no targets or pass/fail.

Check that host and container series are fresh:

```sh
curl -fsS http://127.0.0.1:9273/metrics \
  | grep -E '^(cpu_usage_idle|mem_used_percent|docker_container_cpu_usage_percent)'
```
