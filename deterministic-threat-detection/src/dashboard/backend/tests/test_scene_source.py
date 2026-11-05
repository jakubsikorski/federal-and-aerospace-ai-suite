# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
import datetime
import json
import threading
from collections import deque

import numpy as np
import pytest

pytest.importorskip("trackeval")

from app.hota import LiveHota  # noqa: E402
from app.scene_source import SceneSource, iso_to_epoch  # noqa: E402


def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def make_source():
    """A SceneSource without the TLS client: only the message handling and scoring are under test."""
    gt = [(np.array([0]), np.array([[1.0 + 0.01 * f, 2.0, 0.0]])) for f in range(300)]
    src = SceneSource.__new__(SceneSource)
    src.hota = LiveHota(gt, window_s=5, lag_s=0.0)
    src.connected = False
    src.camera_msgs = src.scene_msgs = src.bad_msgs = 0
    src.last_camera = src.last_scene = src.latest = None
    src._seq = 0
    src.history = deque(maxlen=100)
    src._lock = threading.Lock()
    src._rates = {"camera": deque(maxlen=120), "scene": deque(maxlen=120)}
    src._cam_seen = {}
    src.watchdog = None
    return src


def test_iso_parsing():
    assert iso_to_epoch("2026-10-23T19:24:56.229Z") == pytest.approx(1792783496.229, abs=1e-3)


def test_camera_and_scene_messages_feed_the_score():
    src = make_source()
    base = 1_000_000.0
    for f in range(120):
        t = base + f / 30
        src.handle("scenescape/data/camera/Cam_x1_0", json.dumps({"timestamp": iso(t), "frame": f}))
        src.handle("scenescape/regulated/scene/abc", json.dumps({
            "timestamp": iso(t),
            "objects": [
                {"category": "person", "id": "u1", "translation": [1.0 + 0.01 * f, 2.0, 0.0]},
                {"category": "FW190D", "id": "plane", "translation": [9.0, 9.0, 0.0]},  # not a person: ignored
            ]}))
    assert src.camera_msgs == 120 and src.scene_msgs == 120
    result = src.score_once(now=base + 119 / 30)
    assert result is not None and result["HOTA"] > 0.9 and result["covered"] == 1.0
    assert src.points_since(0)[0][1] == result["HOTA"]


def test_message_without_a_frame_number_counts_for_rates_not_hota():
    src = make_source()
    src.handle("scenescape/data/camera/Cam_x1_0", json.dumps({"id": "Cam_x1_0", "timestamp": "2026-10-23T19:24:56.229Z"}))
    assert src.bad_msgs == 0 and src.camera_msgs == 1 and "Cam_x1_0" in src.status()["cameras"]


def test_per_camera_status_and_rates():
    src = make_source()
    for f in range(30):
        t = 1_000_000.0 + f / 30
        src.handle("scenescape/data/camera/Cam_x1_0", json.dumps({"timestamp": iso(t), "frame": f, "id": "Cam_x1_0"}))
    src.handle("scenescape/data/camera/Cam_x2_0", json.dumps({"timestamp": iso(1_000_000.5), "frame": 15, "id": "Cam_x2_0"}))
    status = src.camera_status(now=src._cam_seen["Cam_x1_0"][-1] + 1.0)
    assert set(status) == {"Cam_x1_0", "Cam_x2_0"}
    assert status["Cam_x1_0"]["fps"] > status["Cam_x2_0"]["fps"] > 0


def test_decoded_frames_are_stored_and_requested_only_while_watched():
    import base64

    src = make_source()
    src.frames, src.viewers, src.frame_cameras, src.connected = {}, 0, ("Cam_x1_0", "Cam_x2_0"), True
    sent = []

    class FakeClient:
        def publish(self, topic, payload, qos=0):
            sent.append((topic, payload))

    src.client = FakeClient()
    jpeg = b"\xff\xd8\xff\xe0fakejpeg\xff\xd9"
    src.handle("scenescape/image/camera/Cam_x1_0", json.dumps({"image": base64.b64encode(jpeg).decode()}))
    assert src.latest_frame("Cam_x1_0")[1] == jpeg and src.latest_frame("Cam_x2_0") is None
    assert src.request_frames() == 0 and sent == []  # nobody is watching: no requests
    src.viewers = 1
    assert src.request_frames() == 2
    assert sent == [("scenescape/cmd/camera/Cam_x1_0", "getimage"), ("scenescape/cmd/camera/Cam_x2_0", "getimage")]


def test_stream_cursor_never_skips_a_score_that_finishes_late():
    """A score is stamped when scoring starts and stored when it ends. A time-based cursor loses a score
    stored after the stream already passed its timestamp; the sequence cursor must not."""
    src = make_source()
    src.history.append({"t": 100.0, "seq": 1, "HOTA": 0.5, "DetA": 0.5, "AssA": 0.5, "covered": 1.0})
    src._seq = 1
    pts, cursor = src.points_after(0)
    assert len(pts) == 1 and cursor == 1
    # score 2 started at t=100.9 but is stored only now, after the stream polled at t=101.0
    src.history.append({"t": 100.9, "seq": 2, "HOTA": 0.6, "DetA": 0.6, "AssA": 0.6, "covered": 1.0})
    src._seq = 2
    pts, cursor = src.points_after(cursor)
    assert [p[1] for p in pts] == [0.6] and cursor == 2   # delivered although its time is older than the last poll
    assert src.points_after(cursor) == ([], 2)
