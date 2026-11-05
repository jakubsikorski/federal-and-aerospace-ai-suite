# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.system_metrics import SystemMetrics, parse_event, parse_sources

SAMPLE = {"timestamp": 1, "metrics": [
    {"name": "cpu_usage_idle", "labels": {"cpu": "cpu-total", "host": "h"}, "value": 40.0},
    {"name": "cpu_usage_user", "labels": {"cpu": "cpu-total"}, "value": 55.0},
    {"name": "cpu_usage_user", "labels": {"cpu": "cpu0"}, "value": 99.0},
    {"name": "mem_used_percent", "labels": {}, "value": 26.5},
    {"name": "mem_used", "labels": {}, "value": 4.0e9}, {"name": "mem_total", "labels": {}, "value": 16.0e9},
    {"name": "gpu_engine_usage_usage", "labels": {"engine": "rcs"}, "value": 12.0},
    {"name": "gpu_engine_usage_usage", "labels": {"engine": "vcs"}, "value": 30.0},
    {"name": "npu_utilization", "labels": {}, "value": 7.0},
    {"name": "temp_temp", "labels": {}, "value": 52},
]}


def test_parse_sources():
    assert parse_sources("a=http://x:1/m, b=http://y:2/m") == {"a": "http://x:1/m", "b": "http://y:2/m"}
    assert parse_sources("") == {}
    with pytest.raises(ValueError):
        parse_sources("no-equals-sign")


def test_parse_event_uses_the_cpu_total_and_the_busiest_gpu_engine():
    r = parse_event(SAMPLE)
    assert r["cpu"] == pytest.approx(60.0)  # 100 - idle of cpu-total, not the single-core 99
    assert r["mem"] == 26.5 and r["gpu"] == 30.0 and r["npu"] == 7.0 and r["temp"] == 52
    assert r["mem_used"] == 4.0e9 and r["mem_total"] == 16.0e9


def test_parse_event_without_idle_falls_back_and_missing_values_are_none():
    payload = {"metrics": [{"name": "cpu_usage_user", "labels": {"cpu": "cpu-total"}, "value": 10.0},
                           {"name": "cpu_usage_system", "labels": {"cpu": "cpu-total"}, "value": 5.0}]}
    r = parse_event(payload)
    assert r["cpu"] == 15.0 and r["mem"] is None and r["gpu"] is None and r["npu"] is None and r["temp"] is None
    assert parse_event({})["cpu"] is None


def test_snapshot_marks_stale_and_never_seen_machines():
    sm = SystemMetrics({"m1": "http://x", "m2": "http://y"}, stale_after_s=5)
    sm.ingest("m1", SAMPLE, now=100.0)
    snap = sm.snapshot(now=102.0)
    assert snap["m1"]["ok"] and snap["m1"]["cpu"] == pytest.approx(60.0) and snap["m1"]["age_s"] == 2.0
    assert not snap["m2"]["ok"] and snap["m2"]["age_s"] is None
    assert not sm.snapshot(now=110.0)["m1"]["ok"]  # no reading for 10 s


def test_reads_a_real_event_stream_over_http():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for _ in range(3):
                self.wfile.write(("data: " + json.dumps(SAMPLE) + "\n\n").encode())
                self.wfile.flush()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sm = SystemMetrics({"m1": f"http://127.0.0.1:{server.server_port}/metrics/stream"})
    sm.start()
    for _ in range(100):
        if sm.snapshot()["m1"]["ok"]:
            break
        threading.Event().wait(0.05)
    sm.stop()
    server.shutdown()
    assert sm.snapshot()["m1"]["ok"] and sm.snapshot()["m1"]["cpu"] == pytest.approx(60.0)


def test_event_without_metrics_is_not_a_reading():
    sm = SystemMetrics({"m1": "http://x"}, stale_after_s=5)
    sm.ingest("m1", SAMPLE, now=100.0)
    sm.ingest("m1", {"timestamp": 1, "metrics": []}, now=101.0)
    snap = sm.snapshot(now=103.0)
    assert snap["m1"]["cpu"] == pytest.approx(60.0) and snap["m1"]["age_s"] == 3.0 and snap["m1"]["error"]
    assert not SystemMetrics({"m2": "http://y"}).snapshot()["m2"]["ok"]
