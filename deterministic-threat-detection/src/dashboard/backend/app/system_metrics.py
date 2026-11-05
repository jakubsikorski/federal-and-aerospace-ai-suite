# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""System load of the machines in the demo, from Intel's metrics-manager.

Each machine runs a metrics-manager container that streams its readings (CPU, memory, GPU, temperature)
as server-sent events on /metrics/stream. One thread per machine keeps the newest reading; the dashboard
shows them in the "System load" card.
"""

import json
import logging
import threading
import time
import urllib.request

log = logging.getLogger("system_metrics")


def parse_sources(text):
    """'machine-1=http://127.0.0.1:19090/metrics/stream,machine-2=http://host:19090/metrics/stream' -> dict"""
    out = {}
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, url = part.partition("=")
        if not sep or not name.strip() or not url.strip():
            raise ValueError(f"bad metrics source '{part}', expected name=url")
        out[name.strip()] = url.strip()
    return out


def parse_event(payload):
    """One metrics-manager event -> {'cpu', 'mem', 'gpu', 'npu', 'temp', 'mem_used', 'mem_total'} (missing = None)."""
    metrics = payload.get("metrics") or []
    by_name = {}
    for m in metrics:
        by_name.setdefault(m.get("name"), []).append(m)

    def first(name, **labels):
        for m in by_name.get(name, []):
            if all(m.get("labels", {}).get(k) == v for k, v in labels.items()):
                return m.get("value")
        return None

    idle = first("cpu_usage_idle", cpu="cpu-total")
    if idle is not None:
        cpu = 100.0 - idle
    else:
        user, system = first("cpu_usage_user", cpu="cpu-total"), first("cpu_usage_system", cpu="cpu-total")
        cpu = None if user is None or system is None else user + system
    gpu_engines = [m["value"] for m in by_name.get("gpu_engine_usage_usage", []) if isinstance(m.get("value"), (int, float))]
    return {
        "cpu": cpu,
        "mem": first("mem_used_percent"),
        "gpu": max(gpu_engines) if gpu_engines else None,
        "npu": first("npu_utilization"),
        "temp": first("temp_temp"),
        "mem_used": first("mem_used"),
        "mem_total": first("mem_total"),
    }


class _Source:
    def __init__(self, name, url):
        self.name, self.url = name, url
        self.latest = None
        self.received = 0.0
        self.error = None


class SystemMetrics:
    def __init__(self, sources, stale_after_s=5.0, opener=None):
        self.stale_after_s = stale_after_s
        self._sources = {n: _Source(n, u) for n, u in sources.items()}
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))  # lab hosts, no proxy
        self._stop = threading.Event()

    def start(self):
        for src in self._sources.values():
            threading.Thread(target=self._run, args=(src,), daemon=True).start()

    def stop(self):
        self._stop.set()

    def _run(self, src):
        while not self._stop.is_set():
            try:
                with self._opener.open(src.url, timeout=10) as resp:
                    for raw in resp:
                        if self._stop.is_set():
                            return
                        line = raw.decode("utf-8", "replace").strip()
                        if line.startswith("data:"):
                            self.ingest(src.name, json.loads(line[5:]))
            except (OSError, ValueError) as exc:  # unreachable, reset, bad JSON: retry shortly
                src.error = f"{type(exc).__name__}: {exc}"
                log.warning("metrics source %s: %s", src.name, src.error)
            self._stop.wait(2.0)

    def ingest(self, name, payload, now=None):
        src = self._sources[name]
        if not payload.get("metrics"):  # the manager is up but its collector delivers nothing: not a reading
            src.error = "metrics-manager sends no metrics"
            return
        src.latest = parse_event(payload)
        src.received = now if now is not None else time.time()
        src.error = None

    def snapshot(self, now=None):
        now = now if now is not None else time.time()
        out = {}
        for name, src in self._sources.items():
            age = None if not src.received else round(now - src.received, 1)
            out[name] = {**(src.latest or {}), "age_s": age,
                         "ok": age is not None and age <= self.stale_after_s, "error": src.error}
        return out
