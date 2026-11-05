# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
from app.pipeline_watchdog import PipelineWatchdog

CAMS = {"Cam_x1_0": "hota-cam1", "Cam_x2_0": "hota-cam2"}


class FakeServer:
    """Pretends to be the pipeline server's REST API."""

    def __init__(self, instances):
        self.instances = instances  # id -> {"state": ..., "camera": ...}
        self.calls = []

    def __call__(self, method, url, body=None, timeout=5):
        self.calls.append((method, url.split("8082")[-1], body))
        if url.endswith("/pipelines/status"):
            return [{"id": i, "state": v["state"]} for i, v in self.instances.items()]
        if "/pipelines/user_defined_pipelines/" in url and method == "POST":
            cam = next(c for c, n in CAMS.items() if n == url.rsplit("/", 1)[1])
            self.instances[f"new-{cam}"] = {"state": "RUNNING", "camera": cam}
            return f"new-{cam}"
        if method == "GET" and "/pipelines/" in url:
            inst = self.instances[url.rsplit("/", 1)[1]]
            return {"params": {"version": CAMS[inst["camera"]]}}
        raise AssertionError((method, url))


def test_nothing_is_restarted_while_both_cameras_run():
    srv = FakeServer({"a": {"state": "RUNNING", "camera": "Cam_x1_0"}, "b": {"state": "RUNNING", "camera": "Cam_x2_0"}})
    wd = PipelineWatchdog("http://x:8082", CAMS, http=srv)
    assert wd.check(now=100) == []
    assert not any(c[0] == "POST" for c in srv.calls)


def test_completed_pipeline_is_restarted_once_by_pipeline_name():
    srv = FakeServer({"a": {"state": "COMPLETED", "camera": "Cam_x1_0"}, "b": {"state": "RUNNING", "camera": "Cam_x2_0"}})
    wd = PipelineWatchdog("http://x:8082", CAMS, cooldown_s=10, http=srv)
    assert wd.check(now=100) == ["Cam_x1_0"]
    post = [c for c in srv.calls if c[0] == "POST"]
    assert post == [("POST", "/pipelines/user_defined_pipelines/hota-cam1",
                     {})]
    assert wd.restarts == {"Cam_x1_0": 1, "Cam_x2_0": 0}
    assert wd.check(now=101) == []  # the new instance runs now: nothing to do


def test_cooldown_prevents_a_restart_loop():
    srv = FakeServer({})  # nothing running at all
    wd = PipelineWatchdog("http://x:8082", CAMS, cooldown_s=10, http=srv)
    assert sorted(wd.check(now=100)) == ["Cam_x1_0", "Cam_x2_0"]
    srv.instances.clear()  # the new instances died immediately
    assert wd.check(now=105) == []  # still inside the cooldown
    assert sorted(wd.check(now=111)) == ["Cam_x1_0", "Cam_x2_0"]
    assert wd.restarts["Cam_x1_0"] == 2


def test_unreachable_server_is_reported_not_raised():
    import urllib.error

    def down(*a, **k):
        raise urllib.error.URLError("connection refused")

    wd = PipelineWatchdog("http://x:8082", CAMS, http=down)
    assert wd.check(now=1) == []
    assert "not reachable" in wd.status()["error"]


def test_camera_id_is_read_from_the_start_request_when_parameters_are_empty():
    srv = FakeServer({"a": {"state": "RUNNING", "camera": "Cam_x1_0", "request_only": True},
                      "b": {"state": "RUNNING", "camera": "Cam_x2_0", "request_only": True}})
    wd = PipelineWatchdog("http://x:8082", CAMS, http=srv)
    assert wd.check(now=100) == []
    assert not any(c[0] == "POST" for c in srv.calls)
