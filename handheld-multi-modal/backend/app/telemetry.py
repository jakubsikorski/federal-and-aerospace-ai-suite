"""Normalize the pinned Metrics Manager payload without inventing series."""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("backend.telemetry")

STALE_AFTER_SECONDS = 5.0


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


def normalize(payload: dict, host: str, *, now: float | None = None) -> dict:
    """Return UI-ready values while preserving absent, zero and unavailable."""
    raw = payload.get("metrics") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return {"status": "unavailable", "reason": "invalid_payload",
                "history_seconds": 300, "metrics": []}

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

    def first(names: tuple[str, ...], *, tags: dict | None = None):
        for name in names:
            found, item = value(name, tags=tags)
            if item is not None:
                return found, item
        return None, None

    def add(metric_id: str, label: str, found, item, unit: str, *,
            scale: float = 1.0, metric_state: str | None = None,
            note: str | None = None, derived: bool = False):
        if (item is None or not isinstance(found, (int, float))
                or isinstance(found, bool)):
            return
        item_ms = _epoch_ms(item.get("timestamp"))
        item_stale = not item_ms or now_ms - item_ms > STALE_AFTER_SECONDS * 1000
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

    found, item = value("cpu_frequency_avg_frequency")
    add("cpu_frequency_mhz", "CPU frequency", found, item, "MHz", scale=0.001,
        note="source converted from kHz")
    found, item = value("mem_used_percent")
    add("memory_used_percent", "Memory used", found, item, "%")
    found, item = value("mem_used")
    add("memory_used_bytes", "Memory used", found, item, "bytes")
    found, item = value("mem_total")
    add("memory_total_bytes", "Memory total", found, item, "bytes")
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

    found, item = first(("gpu_frequency_value", "gpu_frequency"),
                       tags={"type": "cur_freq"})
    add("gpu_frequency_mhz", "GPU frequency", found, item, "MHz")
    found, item = first(("gpu_power_value", "gpu_power"),
                       tags={"type": "gpu_cur_power"})
    add("gpu_power_w", "GPU power domain", found, item, "W")
    found, item = first(("gpu_power_value", "gpu_power"),
                       tags={"type": "pkg_cur_power"})
    add("package_power_w", "Package power", found, item, "W",
        note="package domain; not wall or GPU-only power")

    npu_specs = (
        ("npu_utilization", "npu_utilization_percent", "NPU utilization", "%",
         "zero is not proof of idle when busy-time support is absent"),
        ("npu_power", "npu_power_w", "NPU power", "W", None),
        ("npu_temperature", "npu_temperature_c", "SoC temperature", "°C",
         "PMT SoC field; not necessarily an isolated NPU sensor"),
        ("npu_frequency", "npu_frequency_raw", "NPU frequency (raw)", "source units",
         "hardware scaling is not independently validated"),
        ("npu_bandwidth", "npu_bandwidth_raw", "NPU bandwidth (raw)", "source units",
         "source rate semantics are unresolved"),
        ("npu_tile_config", "npu_tile_config", "NPU tile configuration", "config", None),
    )
    for source, metric_id, label, unit, note in npu_specs:
        found, item = value(source)
        add(metric_id, label, found, item, unit, note=note)
    found, item = value("npu_memory_mb")
    add("npu_memory_mib", "NPU memory", found, item, "MiB",
        metric_state="unavailable" if found == -1 else None,
        note="source -1 means unsupported on this host" if found == -1 else None)

    sampled_utc = (
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(newest_ms / 1000))
        if newest_ms else None
    )
    return {
        "status": state,
        "reason": "stale_samples" if stale else None,
        "host": host,
        "observed_hosts": observed_hosts,
        "sampled_utc": sampled_utc,
        "history_seconds": 300,
        "metrics": metrics,
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
                }
            with self.lock:
                self.latest = latest
            self.stopping.wait(max(0.0, 1.0 - (time.monotonic() - started)))
