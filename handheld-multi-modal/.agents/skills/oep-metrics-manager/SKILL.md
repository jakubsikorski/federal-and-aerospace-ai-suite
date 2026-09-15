---
name: oep-metrics-manager
description: Validate the pinned OEP Metrics Manager deployment and stock CPU GPU NPU RAM power telemetry contracts, preserving unavailable data and Lunar Lake versus Panther Lake evidence boundaries. Deployed and runtime-verified (CPU/RAM/GPU/NPU/temperature).
---

# OEP Metrics Manager — repository override

Read `AGENTS.md` and `docs/bootstrap/service-contracts.md` first.
Locally authored; source baseline is `release-2026.2.0`, commit
`c2cdea0ef9b137986b14e6bbeebcb80dbde4c0e9`,
`microservices/metrics-manager/`. This is not an upstream official skill.
**Deployed and runtime-verified** on this Lunar Lake host — see
`BOOTSTRAP_STATUS.md` and `docs/bootstrap/evidence/metrics/smoke.log`.

## Required workflow

1. Pinned **`intel/metrics-manager:2026.1.0`** confirmed running, as selected
   by `VERSION` in the release checkout.
2. Stock mounts include `/sys:ro`, `/run:ro`, Telegraf configs and
   custom-input storage; GPU uses `/dev/dri`, host PID namespace and render-
   group access (`compose/bootstrap-compose.yaml`, service `metrics`).
   Privileged/AppArmor settings are the source's own required host
   permissions for sysfs telemetry, not an invented bootstrap addition.
3. Keep **Metrics Manager → Backend → UI**. Do not build an aggregator or wire
   the browser directly to privileged host telemetry as part of bootstrap.
4. `:9090/health` confirmed healthy (liveness only). Real data confirmed via
   `:9273/metrics`, `:9090/api/v1/metrics/latest`, and SSE
   `:9090/metrics/stream`.
5. Use `Accept:text/event-stream`. SSE samples contain `name`, `labels`,
   `value`, `timestamp` in epoch milliseconds. Preserve labels and errors.
   `/latest` merges custom second timestamps and scraped nanosecond timestamps;
   normalize explicitly, not by assuming the whole endpoint has one unit.

## Metric interpretation

- CPU: `cpu_usage_user`, `cpu_usage_system`, `cpu_usage_idle` in percent,
  aggregate only. `cpu_frequency_avg_frequency` is kHz, not MHz/per-core.
- RAM: `mem_used_percent`, `mem_available_percent` (%), `mem_total`,
  `mem_used` (bytes). CPU package temperature: `temp_temp` (°C), sensor-filtered.
- GPU: `gpu_engine_usage_usage` (%) with engine/type/host/gpu_id labels;
  `gpu_frequency_value{type="cur_freq"}` is MHz (current/requested).
  `gpu_power_value{type="gpu_cur_power"}` and
  `gpu_power_value{type="pkg_cur_power"}` are W — **confirmed real values this
  session, ~10-18W idle**. Units were traced into pinned qmassa source.
  Retain GPU versus package domains and verify counter availability. No
  default GPU-memory utilization series. Do not invent `gpu_usage_percent`.
- NPU: `npu_power` (W), `npu_utilization` (%), `npu_temperature` (°C,
  **confirmed real ~50°C via PMT this session**), `npu_tile_config` (integer).
  `npu_memory_mb` actually divides by 1024² (MiB); `-1` means unavailable,
  **confirmed on this Lunar Lake host** (documented sentinel, not a bug).
- NPU utilization can be false-zero when `npu_busy_time_us` is unavailable.
  `npu_frequency` is documented Hz but requires scaling validation.
  `npu_bandwidth` is documented MB/s but the reader omits elapsed-time division.
- No dedicated default CPU-only, battery, or total-system power series.
  Intel iGPU package RAPL may appear under the qmassa `pkg_cur_power` label,
  conditional on counter support/permissions; it is not CPU-only power.
  Do not sum overlapping GPU/package and NPU domains.
- Treat missing, stale, unsupported, sentinel and real zero separately.
  Values confirmed this session are **idle-state** (no concurrent DLSPS
  load) — do not extrapolate idle metrics into loaded-workload behavior.

## Target and evidence gates

The project host is Lunar Lake (Core Ultra 9 288V), not the requested Panther
Lake. Owner accepts this Lunar Lake host as the execution target (2026-09-09);
passing tests here satisfy acceptance — actual hardware identity stays
recorded, not relabeled, and no separate Panther Lake measurement is claimed.

On any further run, save actual payloads and logs under bootstrap evidence.
Report absent fields and source unit discrepancies rather than synthesizing
telemetry or claiming a smoke pass beyond what was actually run.

`scripts/smoke/13_metrics.sh` requires `METRICS_URL` (API) and
`METRICS_TELEGRAF_URL` (collector) for the same operator-started instance. It
is **PASS** for CPU/RAM (two original, fresh, advancing Prometheus export
timestamps; `/latest` timestamps alone cannot prove freshness because that
API can synthesize them) — its scope is CPU/RAM-only by design. Real
GPU/NPU/temperature data was additionally confirmed manually this session
(see evidence log above), not by this script.
