# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
from app.latency import LatencyTracker, percentile
from app.mqtt_source import extract, source_name


def test_percentile():
    assert percentile([5], 95) == 5
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile(list(range(101)), 95) == 95


def test_snapshot_window_and_gaps():
    t = LatencyTracker(history_s=60, window_s=10, stall_after_s=2)
    for i, (age, lat) in enumerate([(30, 0.5), (5, 0.010), (4, 0.020), (3, 0.030)]):
        t.add("cam", lat, seq=i if i < 3 else 5, recv_time=100 - age)
    snap = t.snapshot(now=100)["cam"]
    assert snap["count"] == 4
    assert snap["gaps"] == 2  # seq 3 and 4 never arrived
    assert snap["mean_ms"] == 20.0  # the 30 s old sample is outside the window
    assert snap["max_ms"] == 30.0
    assert snap["rate_hz"] == 0.3
    assert snap["stalled"]  # last message was 3 s ago
    assert not t.snapshot(now=98)["cam"]["stalled"]


def test_implausible_latency_is_counted_not_plotted():
    t = LatencyTracker(max_latency_s=60)
    assert not t.add("cam", -3.0, recv_time=1)
    assert not t.add("cam", 3600.0, recv_time=2)
    assert t.add("cam", 0.01, recv_time=3)
    snap = t.snapshot(now=3)["cam"]
    assert snap["rejected"] == 2 and snap["count"] == 3
    assert [p[1] for p in t.history()["cam"]] == [10.0]


def test_history_is_trimmed_and_points_since():
    t = LatencyTracker(history_s=10)
    for i in range(30):
        t.add("cam", 0.01, recv_time=float(i))
    assert len(t.history()["cam"]) == 11
    assert [p[0] for p in t.points_since(27)["cam"]] == [28.0, 29.0]


def test_extract_and_source_name():
    assert extract({"metadata": {"ptp_timestamp": 1.5, "send_timestamp": 1.6, "seq": 7}}) == (1.5, 1.6, 7)
    assert extract({"ptp_timestamp": 2}) == (2.0, None, None)
    assert extract({"metadata": {}}) == (None, None, None)
    assert extract({"metadata": {"ptp_timestamp": "x"}}) == (None, None, None)
    assert source_name("tsn_demo/Cam_x1_0/inference") == "Cam_x1_0"


def test_processing_time_is_tracked_separately():
    t = LatencyTracker(window_s=10)
    t.add("cam", 0.002, recv_time=100.0, processing_s=0.040)
    t.add("cam", 0.004, recv_time=101.0, processing_s=0.050)
    t.add("old", 0.010, recv_time=101.0)  # old message format: no processing time
    snap = t.snapshot(now=102)
    assert snap["cam"]["mean_ms"] == 3.0 and snap["cam"]["proc_ms"] == 45.0
    assert snap["old"]["proc_ms"] is None
    assert t.history()["cam"][0] == (100.0, 2.0, 40.0)
