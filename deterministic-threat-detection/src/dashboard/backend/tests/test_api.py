# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client():
    with TestClient(create_app(start_mqtt=False)) as c:
        yield c


def test_status_and_config(client):
    assert client.get("/api/config").json()["qbv_port"] == 1
    body = client.get("/api/status").json()
    assert body["sources"] == {} and body["qbv"]["enabled"] is False


def test_traffic_rate_is_clamped(client):
    assert client.post("/api/traffic", json={"rate_mbps": 250}).json()["rate_mbps"] == 250
    assert client.post("/api/traffic", json={"rate_mbps": 99999}).json()["rate_mbps"] == 1000
    assert client.post("/api/traffic", json={"rate_mbps": -1}).status_code == 422
    assert client.post("/api/traffic", json={"rate_mbps": 0}).json()["running"] is False


def test_qbv_applies_asynchronously_and_rejects_overlap(client):
    client.app.state.switch.apply_delay_s = 0.2
    assert client.post("/api/qbv", json={"enabled": True}).json()["busy"] is True
    assert client.post("/api/qbv", json={"enabled": False}).status_code == 409
    time.sleep(0.4)
    state = client.get("/api/status").json()["qbv"]
    assert state["enabled"] is True and state["busy"] is False


def test_history_shows_added_samples(client):
    client.app.state.tracker.add("Cam_x1_0", 0.012, recv_time=time.time())
    points = client.get("/api/history").json()["points"]
    assert len(points["Cam_x1_0"]) == 1
    assert client.get("/api/status").json()["sources"]["Cam_x1_0"]["count"] == 1
    client.post("/api/reset")
    assert client.get("/api/status").json()["sources"] == {}


def test_missing_lab_entries_are_named_at_startup():
    import pytest

    from app import config

    with pytest.raises(ValueError, match="M2_LAB_IP.*lab.env"):
        config.require("SWITCH_MODE=moxa", SSH_USER="u", M2_LAB_IP="", SWITCH_IP="x")
    config.require("anything", A="x")  # nothing missing: no error


def test_metrics_sources_default_is_built_from_the_lab_entries(monkeypatch):
    import importlib

    from app import config

    monkeypatch.setenv("M2_LAB_IP", "cam2-lab")
    monkeypatch.setenv("M3_LAB_IP", "cam3-lab")
    monkeypatch.setenv("METRICS_PORT", "12345")
    monkeypatch.delenv("METRICS_SOURCES", raising=False)
    try:
        cfg = importlib.reload(config)
        assert cfg.METRICS_SOURCES == ("machine-1=http://127.0.0.1:12345/metrics/stream,"
                                       "machine-2=http://cam2-lab:12345/metrics/stream,"
                                       "machine-3=http://cam3-lab:12345/metrics/stream")
    finally:
        monkeypatch.undo()
        importlib.reload(config)
