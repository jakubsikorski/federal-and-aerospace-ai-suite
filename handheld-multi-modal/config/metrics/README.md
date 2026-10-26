# Metrics Manager (CPU/RAM/GPU/NPU/temp telemetry)

Pinned image `intel/metrics-manager:2026.2.0`
(digest `sha256:95721fba4300c9778a0d6b133602f19a331ac3d3b0578510269ae19350a8465e`;
the observations below were first recorded on the earlier 2026.1.0 image
and re-checked on 2026.2.0). Deployed via `compose/handheld-multi-modal.yaml` (`metrics`
service), using upstream source's own `compose.yaml` defaults
(`microservices/metrics-manager`, edge-ai-libraries commit `3b44c78`, dated
2026-09-01): `privileged: true`, `pid: host`, `apparmor:unconfined`,
`/dev/dri` device, `group_add: 992` (example `render` GID; resolved per device), `/sys`
and `/run` read-only bind mounts. These are source's own required
settings for host CPU/GPU/NPU telemetry, not an invented application addition.

`config/metrics/telegraf.conf` is copy of upstream `telegraf.conf` from same
commit with one local edit: upstream's commented
`[[inputs.docker]]` example enabled, scoped to running `handheld-*`
containers, 2 s interval, labels dropped, CPU/memory fields only. Compose
mounts `/var/run/docker.sock` read-only into `metrics` for it. Docker API
access is root-equivalent; `metrics` already runs privileged with `pid: host`.
Series: `docker_container_cpu_usage_percent{container_name,cpu="cpu-total"}`
(Docker stats %, 100 = one core, may exceed 100),
`docker_container_mem_usage` / `docker_container_mem_limit` (bytes, usage
excludes page cache), `docker_container_mem_usage_percent`, and the engine's
host CPU count `docker_n_cpus`. Backend returns them as `containers` in
`/api/metrics` with `cpu_host_percent` (CPU / `n_cpus`, same scale as the CPU
tile) and `cpu_cores`; UI Resources tab shows them as Services cards.

## Run

```sh
docker compose -f compose/handheld-multi-modal.yaml up -d metrics
METRICS_URL=http://127.0.0.1:9090 METRICS_TELEGRAF_URL=http://127.0.0.1:9273 \
  bash scripts/smoke/13_metrics.sh
```

## Expected behavior

Observed on an Intel Core Ultra (Series 2, Lunar Lake) test device:

- `GET :9090/health` → `{"status":"healthy","version":"2026.1.0",...}` (2026.2.0 reports the same).
- `scripts/smoke/13_metrics.sh` PASS: two fresh, advancing CPU/RAM samples
  (`cpu_usage_user/system/idle`, `mem_used_percent`, `mem_available_percent`,
  `mem_total`, `mem_used`).
- `GET :9090/api/v1/metrics/latest` and `GET :9273/metrics` both real:
  `cpu_frequency_avg_frequency` (kHz), `temp_temp{sensor=coretemp_package_id_0}`
  (°C, real CPU package temp, observed 52-65°C across samples),
  `gpu_engine_usage_usage` (bcs/ccs/rcs/vcs/vecs, idle-observed 0% — no GPU
  load running), `gpu_frequency{type=cur_freq}` (450 MHz idle),
  `gpu_power{type=gpu_cur_power}` (near-zero, idle) and
  `gpu_power{type=pkg_cur_power}` (real nonzero package RAPL power,
  ~10-18 W observed), `npu_temperature` (50°C, real PMT SoC reading),
  `npu_utilization`/`npu_power`/`npu_bandwidth`/`npu_frequency`/
  `npu_tile_config` (0, idle — no NPU workload was running during this
  smoke), `npu_memory_mb` = **-1**, matching the documented "Lunar Lake
  returns -1" sentinel exactly (source only attempts this on PTL+).
- `GET :9090/metrics/stream` (SSE) emits the same merged set as a JSON array
  on a live `text/event-stream` connection.

## Known limitations / gotchas

- **Hostname-tag bug:** Telegraf's `${METRICS_MANAGER_HOSTNAME}` agent
  hostname override emits as **literal unexpanded string** on all
  CPU/RAM/temp/`cpu_frequency` series in this pinned image (Telegraf logs
  `Strict environment variable handling will be the new default...` and
  doesn't substitute the empty env var). GPU/NPU series (from
  `qmassa_reader.py`/`npu_reader.py`, reading env var directly in Python,
  not via Telegraf's `[agent]` block) correctly fall back to real
  kernel hostname (`a500ab633217` observed). Set `METRICS_MANAGER_HOSTNAME`
  to real value to avoid literal string appearing as host tag on
  Telegraf-sourced series.
- **Image predates newer source:** this pinned tag's image (built
  2026-06-16) doesn't include `/api/v1/capabilities` or
  `DELETE /api/v1/metrics`, both present in newer
  `edge-ai-libraries` checkout (commit `ce292e3`, 2026-09-09) used for
  reference during contract research. Don't assume those two routes exist
  against pinned image; routes this application actually exercises
  (`/health`, `/api/v1/metrics/latest`, `/metrics`, `/metrics/stream`) all
  work as documented.
- `npu_memory_mb` is `-1` on Lunar Lake by design (source only reads it on PTL+); not
  a failure.
- All GPU/NPU values observed here are **idle-state** readings (no DLSPS
  pipeline running detection/tracking during this metrics smoke). Values
  change under real inference load; load-correlation not re-verified this
  pass.
- Panther Lake PMT GUID (`0x3086000`) recognition exists in source but
  unverified here; only Lunar Lake GUID (`0x3072005`) behavior confirmed.
