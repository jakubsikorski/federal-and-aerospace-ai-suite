# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Restarts a camera's DL Streamer pipeline when it has ended.

Under heavy congestion the RTSP connection from the relay to a camera can reset; the pipeline then
reaches the end of its stream, goes to COMPLETED and nothing starts it again (auto_start only runs
at container start). This watchdog asks the pipeline server which instances are running and starts
a new one for any camera that has none. It only looks at instance state, so a pipeline that is
running but starved is left alone (starting a second one would publish every detection twice).
"""

import json
import logging
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("watchdog")

ACTIVE_STATES = {"RUNNING", "QUEUED"}


def default_http(method, url, body=None, timeout=5):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"content-type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # local server, never via a proxy
    with opener.open(req, timeout=timeout) as resp:
        text = resp.read().decode()
    return json.loads(text) if text else None


class PipelineWatchdog:
    def __init__(self, base_url, cameras, interval_s=3.0, cooldown_s=10.0, http=default_http):
        """cameras: {camera id: pipeline name}, e.g. {'Cam_x1_0': 'hota-cam1'}."""
        self.base_url = base_url.rstrip("/")
        self.cameras = dict(cameras)
        self.interval_s, self.cooldown_s = interval_s, cooldown_s
        self._http = http
        self.restarts = {cam: 0 for cam in cameras}
        self.error = None
        self._camera_of = {}  # instance id -> camera id (looked up once per instance)
        self._last_start = {}
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(self.interval_s):
            self.check()

    def _camera_for(self, instance_id):
        if instance_id not in self._camera_of:
            info = self._http("GET", f"{self.base_url}/pipelines/{instance_id}") or {}
            name = (info.get("params") or {}).get("version")  # the pipeline name, e.g. hota-cam1
            cam = next((c for c, n in self.cameras.items() if n == name), None)
            self._camera_of[instance_id] = cam
        return self._camera_of[instance_id]

    def check(self, now=None):
        """One pass; returns the cameras for which a pipeline was (re)started."""
        now = now if now is not None else time.time()
        started = []
        try:
            instances = self._http("GET", f"{self.base_url}/pipelines/status") or []
            active = {self._camera_for(i["id"]) for i in instances if i.get("state") in ACTIVE_STATES}
            for cam, name in self.cameras.items():
                if cam in active or now - self._last_start.get(cam, 0) < self.cooldown_s:
                    continue
                self._http("POST", f"{self.base_url}/pipelines/user_defined_pipelines/{name}", {})
                self._last_start[cam] = now
                self.restarts[cam] += 1
                started.append(cam)
                log.warning("pipeline %s for %s was not running; started a new instance", name, cam)
            self.error = None
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            self.error = f"pipeline server not reachable: {exc}"
        return started

    def status(self):
        return {"restarts": dict(self.restarts), "error": self.error}
