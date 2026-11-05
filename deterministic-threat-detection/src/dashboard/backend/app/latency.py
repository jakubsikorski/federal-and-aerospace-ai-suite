# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Per-source latency bookkeeping: rolling window statistics and sequence gaps."""

import math
import threading
import time
from collections import deque


def percentile(values, pct):
    """Linear-interpolated percentile of a non-empty list (pct in 0..100)."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct / 100.0
    low = math.floor(rank)
    high = math.ceil(rank)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


class SourceStats:
    def __init__(self):
        self.samples = deque()  # (recv_time, network_s, processing_s or None)
        self.total = 0
        self.rejected = 0  # implausible latency (unsynchronised clocks)
        self.gaps = 0  # sequence numbers that never arrived in order
        self.last_seq = None
        self.last_recv = None


class LatencyTracker:
    """Thread-safe; fed by the MQTT thread, read by the HTTP/SSE handlers."""

    def __init__(self, history_s=120, window_s=10, stall_after_s=2, max_latency_s=60):
        self.history_s = history_s
        self.window_s = window_s
        self.stall_after_s = stall_after_s
        self.max_latency_s = max_latency_s
        self._sources = {}
        self._lock = threading.Lock()

    def add(self, source, latency_s, seq=None, recv_time=None, processing_s=None):
        """Record one message (latency_s = network latency). False when the latency is implausible."""
        now = recv_time if recv_time is not None else time.time()
        with self._lock:
            src = self._sources.setdefault(source, SourceStats())
            src.total += 1
            src.last_recv = now
            if seq is not None:
                if src.last_seq is not None and seq > src.last_seq + 1:
                    src.gaps += seq - src.last_seq - 1
                src.last_seq = seq
            if not 0 <= latency_s <= self.max_latency_s:
                src.rejected += 1
                return False
            src.samples.append((now, latency_s, processing_s))
            cutoff = now - self.history_s
            while src.samples and src.samples[0][0] < cutoff:
                src.samples.popleft()
            return True

    def points_since(self, since):
        """New (recv_time, network_ms, processing_ms|None) samples per source after 'since'."""
        out = {}
        with self._lock:
            for name, src in self._sources.items():
                out[name] = [
                    (t, lat * 1000.0, None if proc is None else proc * 1000.0)
                    for t, lat, proc in src.samples
                    if t > since
                ]
        return out

    def history(self):
        return self.points_since(0)

    def snapshot(self, now=None):
        """KPI numbers per source over the rolling window."""
        now = now if now is not None else time.time()
        out = {}
        with self._lock:
            for name, src in self._sources.items():
                recent = [(lat, proc) for t, lat, proc in src.samples if t >= now - self.window_s]
                window = [lat for lat, _ in recent]
                procs = [proc for _, proc in recent if proc is not None]
                entry = {
                    "count": src.total,
                    "rejected": src.rejected,
                    "gaps": src.gaps,
                    "rate_hz": round(len(window) / self.window_s, 1),
                    "stalled": src.last_recv is None or now - src.last_recv > self.stall_after_s,
                    "last_ms": round(src.samples[-1][1] * 1000.0, 2) if src.samples else None,
                    "proc_ms": round(sum(procs) / len(procs) * 1000.0, 2) if procs else None,
                }
                if window:
                    entry.update(
                        mean_ms=round(sum(window) / len(window) * 1000.0, 2),
                        p95_ms=round(percentile(window, 95) * 1000.0, 2),
                        max_ms=round(max(window) * 1000.0, 2),
                    )
                else:
                    entry.update(mean_ms=None, p95_ms=None, max_ms=None)
                out[name] = entry
        return out

    def reset(self):
        with self._lock:
            self._sources.clear()
