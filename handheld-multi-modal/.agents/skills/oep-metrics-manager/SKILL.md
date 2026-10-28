---
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
name: oep-metrics-manager
description: Use when reading or extending handheld resource telemetry from the stock Open Edge Platform Metrics Manager (CPU, memory, GPU, NPU, power and per-service container metrics).
---

# Metrics Manager

Image `intel/metrics-manager:2026.2.0`, source `edge-ai-libraries`
`microservices/metrics-manager/` (release-2026.2.0). Compose service
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
  `cpu_frequency_avg_frequency` is kHz.
- Memory: `mem_used_percent`, `mem_available_percent` (%), `mem_total`,
  `mem_used` (bytes). CPU package temperature: `temp_temp` (°C).
- GPU: `gpu_engine_usage_usage` (%) labelled by engine;
  `gpu_frequency{type="cur_freq"}` (MHz);
  `gpu_power{type="gpu_cur_power"}` and `{type="pkg_cur_power"}` (W).
  Some collector versions add a `_value` suffix; `backend/app/telemetry.py`
  accepts both.
  There is no GPU memory utilization series and no `gpu_usage_percent`.
- NPU: `npu_utilization` (%), `npu_power` (W), `npu_temperature` (°C),
  `npu_memory_mb` (actually MiB; `-1` means unavailable). Utilization can
  read 0 when the driver does not expose NPU busy time.
- Containers: `docker_container_cpu_usage_percent{cpu="cpu-total"}` (100 =
  one core), `docker_container_mem_usage`, `docker_container_mem_limit`
  (bytes), `docker_container_mem_usage_percent`, and `docker_n_cpus`, which
  the Backend divides by to show a share of the whole machine.
- There is no CPU-only, battery or total-system power series. `pkg_cur_power`
  is package power, not CPU power. Do not add overlapping GPU, package and NPU
  power domains.
- Keep missing, stale, unsupported, sentinel and real-zero values distinct;
  never synthesize a value.

Check that host and container series are fresh:

```sh
curl -fsS http://127.0.0.1:9273/metrics \
  | grep -E '^(cpu_usage_idle|mem_used_percent|docker_container_cpu_usage_percent)'
```
