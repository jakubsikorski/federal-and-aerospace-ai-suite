# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Normalize the pinned Metrics Manager payload without inventing series."""
from __future__ import annotations

import logging
import math
import threading
import time

log = logging.getLogger("backend.telemetry")

STALE_AFTER_SECONDS = 5.0
# inputs.docker runs every 2 s and Docker stats add ~1-2 s; 6 s ages observed.
CONTAINER_STALE_AFTER_SECONDS = 10.0

# One table maps every per-service Panel field to its upstream series name.
# A rename upstream is a one-line change here. Kinds:
#   hist    histogram family <name>_bucket/_sum/_count (Prometheus scrape) or
#           <name>_count/_sum/_avg (OTLP push), or a bare gauge <name>
#   gauge   bare series <name>; a real 0 stays 0
#   errors  counter <name>{outcome}; error_rate = (error + timeout) / all * 100
#   ratio   derived read-side from two other keys of the same service
# Names are from the plan, section 3; the pinned images do not expose them yet,
# so absent series report "missing". The tts time-to-first-audio name is not
# listed in the plan and is a placeholder until the upstream release names it.
SERVICE_METRICS = {
    "chat": {
        "ttft_seconds": ("hist", "chatqna_ttft_seconds", "s"),
        "queue_wait_seconds": ("hist", "chatqna_queue_wait_seconds", "s"),
        "decode_tokens_per_second": ("hist", "chatqna_decode_tokens_per_second", "tokens/s"),
        "request_seconds": ("hist", "chatqna_request_seconds", "s"),
        "error_rate_percent": ("errors", "chatqna_requests_total", "%"),
        "in_flight": ("gauge", "chatqna_requests_in_flight", "requests"),
    },
    "asr": {
        "rtfx": ("ratio", ("audio_seconds", "request_seconds"), "x"),
        "request_seconds": ("hist", "audio_analyzer_inference_seconds", "s"),
        "audio_seconds": ("hist", "audio_analyzer_audio_duration_seconds", "s"),
        "error_rate_percent": ("errors", "audio_analyzer_requests_total", "%"),
        "in_flight": ("gauge", "audio_analyzer_requests_in_flight", "requests"),
    },
    "tts": {
        "rtf": ("hist", "tts_rtf", "ratio"),
        "time_to_first_audio_seconds": ("hist", "tts_time_to_first_audio_seconds", "s"),
        "request_seconds": ("hist", "tts_generate_seconds", "s"),
        "error_rate_percent": ("errors", "tts_requests_total", "%"),
        "in_flight": ("gauge", "tts_requests_in_flight", "requests"),
    },
}
ERROR_OUTCOMES = ("error", "timeout")


def _epoch_ms(value) -> int | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value > 1e15:
        return int(value / 1_000_000)
    if value > 1e12:
        return int(value)
    if value > 1e9:
        return int(value * 1000)
    return None


def _metric(value=None, unit="", state="missing", p50=None, p95=None, count=None) -> dict:
    return {"value": value, "unit": unit, "state": state,
            "p50": p50, "p95": p95, "count": count}


def _missing_services() -> dict:
    """Every service and key present, all missing (no series, no poll yet)."""
    return {
        svc: {"state": "missing",
              "metrics": {key: _metric(unit=spec[2]) for key, spec in keys.items()}}
        for svc, keys in SERVICE_METRICS.items()
    }


def _number(item: dict):
    found = (item.get("fields") or {}).get("value")
    if isinstance(found, bool) or not isinstance(found, (int, float)):
        return None
    return float(found) if math.isfinite(found) else None


def _nearest_rank(buckets: list[tuple[float, float]], total: float, q: float):
    """Upper bound of the first cumulative bucket holding rank ceil(q*total)."""
    if total <= 0:
        return None
    rank = math.ceil(q * total)
    for bound, cumulative in sorted(buckets):
        if cumulative >= rank:
            return None if math.isinf(bound) else bound
    return None


def _services(by_name: dict[str, list[dict]], now_ms: int) -> dict:
    """Read per-service series; derivations are read-side, nothing is kept."""
    def fresh(stamps: list) -> str:
        oldest = min(stamps)
        return ("stale" if not oldest
                or now_ms - oldest > STALE_AFTER_SECONDS * 1000 else "ok")

    def groups(name: str, suffixes: tuple[str, ...]) -> dict:
        """Series of one family, grouped by their labels (le/url/host ignored)."""
        found: dict[tuple, dict] = {}
        for suffix in suffixes:
            for item in by_name.get(name + suffix, []):
                number = _number(item)
                if number is None:
                    continue
                tags = item.get("tags") or {}
                bound = None
                if suffix == "_bucket":
                    try:
                        bound = float(tags.get("le"))
                    except (TypeError, ValueError):
                        continue  # a malformed bucket must not create a group
                    if math.isnan(bound):
                        continue
                key = tuple(sorted((k, str(v)) for k, v in tags.items()
                                   if k not in ("le", "url", "host")))
                part = found.setdefault(key, {"stamps": [], "bucket": []})
                part["stamps"].append(_epoch_ms(item.get("timestamp")) or 0)
                if suffix == "_bucket":
                    part["bucket"].append((bound, number))
                else:
                    part[suffix or "value"] = number
        return found

    def read_hist(name: str, unit: str) -> dict:
        found = groups(name, ("", "_bucket", "_sum", "_count", "_avg"))
        if not found:
            return _metric(unit=unit)
        part = max(found.values(), key=lambda row: max(row["stamps"]))
        count = part.get("_count")
        if count is None and part["bucket"]:
            infinite = [c for bound, c in part["bucket"] if math.isinf(bound)]
            count = infinite[0] if infinite else None
        if "_sum" in part and count is not None:
            value = part["_sum"] / count if count > 0 else None
        elif "_avg" in part:
            value = part["_avg"]
        else:
            value = part.get("value")
        if value is None:
            return _metric(unit=unit)  # no observations yet is not a zero
        p50 = p95 = None
        if part["bucket"] and count is not None:
            p50 = _nearest_rank(part["bucket"], count, 0.50)
            p95 = _nearest_rank(part["bucket"], count, 0.95)
        return _metric(value, unit, fresh(part["stamps"]), p50, p95,
                       None if count is None else int(count))

    def read_errors(name: str, unit: str) -> dict:
        total = errors = 0.0
        stamps: list = []
        for item in by_name.get(name, []):
            number = _number(item)
            if number is None:
                continue
            total += number
            stamps.append(_epoch_ms(item.get("timestamp")) or 0)
            if str((item.get("tags") or {}).get("outcome")) in ERROR_OUTCOMES:
                errors += number
        if not stamps or total <= 0:
            return _metric(unit=unit)  # nothing counted yet: no rate to report
        return _metric(errors / total * 100.0, unit, fresh(stamps))

    services = {}
    for svc, keys in SERVICE_METRICS.items():
        metrics: dict[str, dict] = {}
        for key, (kind, source, unit) in keys.items():
            if kind in ("hist", "gauge"):
                metrics[key] = read_hist(source, unit)
            elif kind == "errors":
                metrics[key] = read_errors(source, unit)
        for key, (kind, source, unit) in keys.items():
            if kind != "ratio":
                continue
            top, bottom = metrics[source[0]], metrics[source[1]]
            if (top["state"] == "missing" or bottom["state"] == "missing"
                    or not bottom["value"]):
                metrics[key] = _metric(unit=unit)
            else:
                state = "stale" if "stale" in (top["state"], bottom["state"]) else "ok"
                metrics[key] = _metric(top["value"] / bottom["value"], unit, state)
        metrics = {key: metrics[key] for key in keys}
        states = {m["state"] for m in metrics.values()} - {"missing"}
        services[svc] = {
            "state": "missing" if not states else "ok" if "ok" in states else "stale",
            "metrics": metrics,
        }
    return services


def normalize(payload: dict, host: str, *, now: float | None = None) -> dict:
    """Return UI-ready values while preserving absent, zero and unavailable."""
    raw = payload.get("metrics") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return {"status": "unavailable", "reason": "invalid_payload",
                "history_seconds": 300, "metrics": [],
                "services": _missing_services()}

    observed_hosts = sorted({
        str(item.get("tags", {}).get("host"))
        for item in raw.values()
        if isinstance(item, dict) and item.get("tags", {}).get("host") is not None
    })
    series = [
        item for item in raw.values()
        if isinstance(item, dict) and item.get("tags", {}).get("host") == host
    ]
    if not series:
        return {
            "status": "host_mismatch" if raw else "unavailable",
            "reason": "telemetry_host_mismatch" if raw else "no_series",
            "host": host,
            "observed_hosts": observed_hosts,
            "history_seconds": 300,
            "metrics": [],
            "services": _missing_services(),
        }

    newest_ms = max((_epoch_ms(item.get("timestamp")) or 0 for item in series), default=0)
    now_ms = int((time.time() if now is None else now) * 1000)
    stale = not newest_ms or now_ms - newest_ms > STALE_AFTER_SECONDS * 1000
    state = "stale" if stale else "ok"
    by_name: dict[str, list[dict]] = {}
    for item in series:
        by_name.setdefault(item.get("name", ""), []).append(item)

    metrics: list[dict] = []

    def value(name: str, *, tags: dict | None = None):
        for item in by_name.get(name, []):
            actual = item.get("tags") or {}
            if tags and any(actual.get(key) != expected for key, expected in tags.items()):
                continue
            found = (item.get("fields") or {}).get("value")
            if isinstance(found, (int, float)) and not isinstance(found, bool):
                return float(found), item
        return None, None

    def sample_stale(item: dict) -> bool:
        item_ms = _epoch_ms(item.get("timestamp"))
        return not item_ms or now_ms - item_ms > STALE_AFTER_SECONDS * 1000

    def add(metric_id: str, label: str, found, item, unit: str, *,
            scale: float = 1.0, metric_state: str | None = None,
            note: str | None = None, derived: bool = False):
        if (item is None or not isinstance(found, (int, float))
                or isinstance(found, bool)):
            return
        item_ms = _epoch_ms(item.get("timestamp"))
        item_stale = sample_stale(item)
        metrics.append({
            "id": metric_id,
            "label": label,
            "value": None if metric_state == "unavailable" else found * scale,
            "unit": unit,
            "state": metric_state or ("stale" if item_stale else "ok"),
            "source": item.get("name"),
            "source_tags": item.get("tags") or {},
            "sampled_utc": (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(item_ms / 1000))
                if item_ms else None
            ),
            "derived": derived,
            "note": note,
        })

    user, user_item = value("cpu_usage_user", tags={"cpu": "cpu-total"})
    system, system_item = value("cpu_usage_system", tags={"cpu": "cpu-total"})
    if user_item is not None and system_item is not None:
        add("cpu_load_percent", "CPU load", user + system, user_item, "%",
            note="user + system", derived=True)
        metrics[-1]["source"] = "cpu_usage_user + cpu_usage_system"
    else:
        idle, idle_item = value("cpu_usage_idle", tags={"cpu": "cpu-total"})
        if idle_item is not None:
            add("cpu_load_percent", "CPU load", 100.0 - idle, idle_item, "%",
                note="100 - idle", derived=True)

    found, item = value("cpu_frequency_avg_frequency_hz")
    add("cpu_frequency_mhz", "CPU frequency", found, item, "MHz", scale=0.000001,
        note="source converted from Hz")
    found, item = value("mem_used_percent")
    add("memory_used_percent", "Memory used", found, item, "%")
    found, item = value("mem_used")
    add("memory_used_bytes", "Memory used", found, item, "bytes")
    found, item = value("mem_total")
    add("memory_total_bytes", "Memory total", found, item, "bytes")
    available, available_item = value("mem_available_percent")
    if item is not None and available_item is not None:
        add("memory_available_bytes", "Memory available", found * available / 100.0,
            available_item, "bytes", note="mem_total × mem_available_percent", derived=True)
    for item in by_name.get("temp_temp", []):
        sensor = str((item.get("tags") or {}).get("sensor", ""))
        if sensor.startswith("coretemp_package_id_"):
            found = (item.get("fields") or {}).get("value")
            add("cpu_temperature_c", "CPU package temperature", found, item, "°C")
            break

    for item in sorted(by_name.get("gpu_engine_usage_usage", []),
                       key=lambda row: str((row.get("tags") or {}).get("engine", ""))):
        engine = str((item.get("tags") or {}).get("engine", "unknown"))
        found = (item.get("fields") or {}).get("value")
        add(f"gpu_engine_{engine}_percent", f"GPU {engine} engine", found, item, "%")

    found, item = value("gpu_frequency_cur_freq_hz", tags={"type": "cur_freq"})
    add("gpu_frequency_mhz", "GPU frequency", found, item, "MHz", scale=0.000001,
        note="source converted from Hz")
    found, item = value("gpu_power_power_w", tags={"type": "gpu_cur_power"})
    add("gpu_power_w", "GPU power domain", found, item, "W")
    found, item = value("powerstat_package_current_power_consumption_watts",
                       tags={"package_id": "0"})
    rapl_found, rapl_item = value("rapl_power_power_w", tags={"domain": "package-0"})
    if item is None or (sample_stale(item) and rapl_item is not None
                        and not sample_stale(rapl_item)):
        found, item = rapl_found, rapl_item
    add("package_power_w", "Package power", found, item, "W",
        note="package domain; not wall or GPU-only power")

    npu_specs = (
        ("npu_utilization", "npu_utilization_percent", "NPU utilization", "%", None),
        ("npu_power_w", "npu_power_w", "NPU power", "W", None),
        ("npu_temperature_c", "npu_temperature_c", "SoC temperature", "°C",
         "PMT SoC field; not necessarily an isolated NPU sensor"),
        ("npu_frequency_hz", "npu_frequency_raw", "NPU frequency", "Hz", None),
        ("npu_bandwidth_bytes_per_second", "npu_bandwidth_raw", "NPU bandwidth", "bytes/s", None),
        ("npu_tile_config", "npu_tile_config", "NPU tile configuration", "config", None),
    )
    for source, metric_id, label, unit, note in npu_specs:
        found, item = value(source)
        add(metric_id, label, found, item, unit, note=note)
    found, item = value("npu_memory_bytes")
    unavailable = found is not None and found < 0
    add("npu_memory_mib", "NPU memory", found, item, "MiB", scale=1 / (1024 * 1024),
        metric_state="unavailable" if unavailable else None,
        note="negative memory reading is unavailable" if unavailable else "source converted from bytes")

    # Telegraf inputs.docker series; Docker CPU percent uses 100 = one core.
    container_fields = (
        ("docker_container_cpu_usage_percent", "cpu_percent"),
        ("docker_container_mem_usage", "memory_bytes"),
        ("docker_container_mem_limit", "memory_limit_bytes"),
        ("docker_container_mem_usage_percent", "memory_percent"),
    )
    rows: dict[str, dict] = {}
    for source, key in container_fields:
        for item in by_name.get(source, []):
            tags = item.get("tags") or {}
            name = tags.get("container_name")
            if not name or tags.get("cpu", "cpu-total") != "cpu-total":
                continue
            found = (item.get("fields") or {}).get("value")
            if not isinstance(found, (int, float)) or isinstance(found, bool):
                continue
            row = rows.setdefault(str(name), {
                "name": str(name), "cpu_percent": None, "memory_bytes": None,
                "memory_limit_bytes": None, "memory_percent": None, "_ms": 0,
            })
            row[key] = float(found)
            row["_ms"] = max(row["_ms"], _epoch_ms(item.get("timestamp")) or 0)
    host_cpus, _ = value("docker_n_cpus")
    host_cpus = host_cpus if host_cpus and host_cpus > 0 else None
    containers = []
    for row in sorted(rows.values(), key=lambda entry: entry["name"]):
        item_ms = row.pop("_ms")
        cpu = row["cpu_percent"]
        row["cpu_cores"] = None if cpu is None else cpu / 100.0
        row["cpu_host_percent"] = None if cpu is None or not host_cpus else cpu / host_cpus
        row["state"] = ("stale" if not item_ms
                        or now_ms - item_ms > CONTAINER_STALE_AFTER_SECONDS * 1000 else "ok")
        row["sampled_utc"] = (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(item_ms / 1000))
            if item_ms else None
        )
        containers.append(row)

    sampled_utc = (
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(newest_ms / 1000))
        if newest_ms else None
    )
    try:
        services = _services(by_name, now_ms)
    except Exception:  # one bad service series must not blank host telemetry
        log.exception("service series could not be read")
        services = _missing_services()
    return {
        "status": state,
        "reason": "stale_samples" if stale else None,
        "host": host,
        "observed_hosts": observed_hosts,
        "sampled_utc": sampled_utc,
        "history_seconds": 300,
        "metrics": metrics,
        "containers": containers,
        "host_cpus": int(host_cpus) if host_cpus else None,
        "services": services,
    }


class Telemetry:
    """One 1 Hz poller shared by every API and SSE client."""

    def __init__(self, services, host: str):
        self.services = services
        self.host = host
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.latest = {
            "status": "unavailable", "reason": "starting", "host": host,
            "history_seconds": 300, "metrics": [],
            "services": _missing_services(),
        }

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="telemetry", daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stopping.set()
        self.thread.join(timeout=10.0)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                **self.latest,
                "metrics": [dict(metric) for metric in self.latest.get("metrics", [])],
                "containers": [dict(row) for row in self.latest.get("containers", [])],
                "services": {
                    svc: {**data, "metrics": {key: dict(m) for key, m in data["metrics"].items()}}
                    for svc, data in (self.latest.get("services") or _missing_services()).items()
                },
            }

    def _run(self) -> None:
        while not self.stopping.is_set():
            started = time.monotonic()
            try:
                latest = normalize(self.services.metrics_latest(), self.host)
            except Exception as exc:
                log.warning("metrics poll failed: %s: %s", type(exc).__name__, exc)
                latest = {
                    "status": "unavailable", "reason": "metrics_manager_unavailable",
                    "host": self.host, "history_seconds": 300, "metrics": [],
                    "services": _missing_services(),
                }
            with self.lock:
                self.latest = latest
            self.stopping.wait(max(0.0, 1.0 - (time.monotonic() - started)))
