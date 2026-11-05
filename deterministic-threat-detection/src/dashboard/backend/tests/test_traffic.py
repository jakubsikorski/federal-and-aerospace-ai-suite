# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
import time

from app.controllers.traffic import IperfServer, TrafficController, parse_server_line

INTERVAL = "[  5]   3.00-4.00   sec  11.2 MBytes  94.0 Mbits/sec  0.045 ms  3/8133 (0.037%)  "
SUMMARY = "[  5]   0.00-10.00  sec   112 MBytes  94.0 Mbits/sec  0.040 ms  30/81330 (0.037%)  receiver"
OTHER = "[  5] local srv-noise port 5201 connected to inj-noise port 41234"


def test_parse_server_interval_line():
    rep = parse_server_line(INTERVAL)
    assert rep == {"mbps": 94.0, "jitter_ms": 0.045, "lost": 3, "total": 8133, "loss_pct": 0.037}


def test_summary_and_noise_lines_are_ignored():
    assert parse_server_line(SUMMARY) is None
    assert parse_server_line(OTHER) is None
    assert parse_server_line("") is None


def test_measured_goes_stale():
    server = IperfServer("srv-noise")
    server.last, server.last_time = parse_server_line(INTERVAL), 100.0
    assert server.measured(now=102.0)["mbps"] == 94.0
    assert server.measured(now=105.0) is None


def test_missing_binary_is_reported():
    server = IperfServer("srv-noise", binary="definitely-not-iperf3")
    server.start()
    assert "not found" in server.state()["error"] and not server.state()["running"]


class FakeAgent:
    """Stands in for the load agent: records POSTs, answers like it."""

    def __init__(self, fail=False):
        self.posts, self.fail, self.running = [], fail, False

    def __call__(self, method, path, body=None):
        if self.fail:
            raise OSError("connection refused")
        if method == "POST":
            self.posts.append(body)
            self.running = body["mbps"] > 0
        return {"running": self.running, "returncode": None if self.running else 255}


def make(agent, **kw):
    t = TrafficController("iperf", 900, "http://m4:8099", **kw)
    t._call = agent
    return t


def test_iperf_mode_sends_rates_to_the_agent():
    agent = FakeAgent()
    t = make(agent, ttl_s=30)
    assert t.set_rate(400) == 400 and agent.posts == [{"mbps": 400, "ttl_s": 30}]
    assert t.state()["running"] is True
    assert t.set_rate(2000) == 900 and agent.posts[-1]["mbps"] == 900  # capped
    assert t.set_rate(0) == 0 and agent.posts[-1]["mbps"] == 0
    assert t.state()["running"] is False


def test_unreachable_agent_is_reported_and_load_not_assumed():
    t = make(FakeAgent(fail=True))
    assert t.set_rate(100) == 0
    assert "unreachable" in t.state()["error"]


def test_agent_whose_client_exited_is_reported():
    agent = FakeAgent()
    t = make(agent)
    t.set_rate(100)
    agent.running = False
    t.agent = agent("GET", "/state")  # what the refresh loop does
    state = t.state()
    assert state["running"] is False and "exited with code 255" in state["error"]
