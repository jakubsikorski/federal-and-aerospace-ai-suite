# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Use case 2 data: camera detections and SceneScape's tracked output -> live HOTA.

Subscribes to the SceneScape broker (TLS) and feeds LiveHota. The camera messages give the frame
number (from the SEI data in the video) and the time; the scene messages give the tracked people.
A scorer thread evaluates the sliding window once a second and keeps a short history for the chart.
"""

import base64
import datetime as dt
import json
import logging
import ssl
import threading
import time
from collections import deque

import paho.mqtt.client as mqtt

from .hota import LiveHota

log = logging.getLogger("scene_source")


def iso_to_epoch(text):
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


class SceneSource:
    def __init__(self, hota, host, port, ca_path, history_s=300, score_interval_s=1.0):
        self.hota = hota
        self.host, self.port, self.ca_path = host, port, ca_path
        self.score_interval_s = score_interval_s
        self.connected = False
        self.camera_msgs = self.scene_msgs = self.bad_msgs = 0
        self.last_camera = self.last_scene = None  # receive times
        self.latest = None  # newest score dict (with 't' and 'seq')
        self._seq = 0  # numbers the scores; stream cursors use it, never timestamps (see points_after)
        self.history = deque(maxlen=int(history_s / score_interval_s))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._rates = {"camera": deque(maxlen=1200), "scene": deque(maxlen=600)}
        self._cam_seen = {}  # camera id -> deque of receive times
        self.frames = {}  # camera id -> (receive time, JPEG bytes): the pipeline's decoded, annotated frame
        self.viewers = 0  # open video streams; frames are only requested while somebody watches
        self.frame_cameras = ()  # cameras to request frames for
        self.frame_fps = 10.0
        self.watchdog = None  # optional PipelineWatchdog, reported in status()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.tls_set(ca_certs=ca_path, cert_reqs=ssl.CERT_REQUIRED)
        self.client.tls_insecure_set(True)  # the certificate is issued for broker.scenescape.intel.com
        self.client.reconnect_delay_set(min_delay=1, max_delay=10)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    # --- MQTT ----------------------------------------------------------------------------------
    def start(self):
        self.client.connect_async(self.host, self.port, keepalive=3600)
        self.client.loop_start()
        threading.Thread(target=self._score_loop, daemon=True).start()
        threading.Thread(target=self._frame_request_loop, daemon=True).start()

    def stop(self):
        self._stop.set()
        self.client.loop_stop()
        self.client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            self.connected = True
            client.subscribe([("scenescape/data/camera/+", 0), ("scenescape/regulated/scene/+", 0),
                              ("scenescape/image/camera/+", 0)])
            log.info("connected to the SceneScape broker %s:%s", self.host, self.port)
        else:
            log.error("scene broker connect failed: %s", reason_code)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = False

    def _on_message(self, client, userdata, message):
        try:
            self.handle(message.topic, message.payload)
        except Exception:  # one malformed message must not kill the MQTT thread
            self.bad_msgs += 1
            log.exception("bad message on %s", message.topic)

    def handle(self, topic, payload):
        """Parse one message (separate from the MQTT callback so tests can call it)."""
        msg = json.loads(payload)
        now = time.time()
        if "/data/camera/" in topic:
            frame = msg.get("frame")
            with self._lock:
                # live cameras (basler) carry no SEI frame number: counted for rates, not for HOTA
                if isinstance(frame, int):
                    self.hota.on_camera(iso_to_epoch(msg["timestamp"]), frame)
                self.camera_msgs += 1
                self.last_camera = now
                self._rates["camera"].append(now)
                self._cam_seen.setdefault(str(msg.get("id", "?")), deque(maxlen=600)).append(now)
        elif "/image/camera/" in topic:
            jpeg = base64.b64decode(msg.get("image", ""))
            if jpeg:
                with self._lock:
                    self.frames[topic.rsplit("/", 1)[1]] = (now, jpeg)
        elif "/regulated/scene/" in topic:
            people = [o for o in msg.get("objects", []) if str(o.get("category", "")).lower() == "person"]
            with self._lock:
                self.hota.on_scene(iso_to_epoch(msg["timestamp"]), [o["id"] for o in people],
                                   [o["translation"] for o in people])
                self.scene_msgs += 1
                self.last_scene = now
                self._rates["scene"].append(now)

    # --- decoded frames ------------------------------------------------------------------------
    def _frame_request_loop(self):
        while not self._stop.wait(1.0 / self.frame_fps):
            self.request_frames()

    def request_frames(self):
        """Ask each camera's pipeline for one annotated frame (the plugin answers one 'getimage' with
        one JPEG on scenescape/image/camera/<id>). Only while a video stream is open."""
        if self.viewers <= 0 or not self.connected:
            return 0
        for cam in self.frame_cameras:
            self.client.publish(f"scenescape/cmd/camera/{cam}", "getimage", qos=0)
        return len(self.frame_cameras)

    def latest_frame(self, cam):
        with self._lock:
            return self.frames.get(cam)

    # --- scoring -------------------------------------------------------------------------------
    def _score_loop(self):
        while not self._stop.wait(self.score_interval_s):
            self.score_once()

    def score_once(self, now=None):
        now = now if now is not None else time.time()
        try:
            with self._lock:
                result = self.hota.score(now)
        except Exception:
            log.exception("HOTA scoring failed")
            return None
        if result is not None:
            self._seq += 1
            result = {"t": now, "seq": self._seq, **result}
            self.latest = result
            self.history.append(result)
        return result

    def rate(self, kind, window_s=5.0, now=None):
        now = now if now is not None else time.time()
        with self._lock:
            return round(sum(1 for t in self._rates[kind] if t >= now - window_s) / window_s, 1)

    def camera_status(self, now=None, window_s=5.0):
        now = now if now is not None else time.time()
        with self._lock:
            out = {}
            for cam, seen in self._cam_seen.items():
                last = seen[-1] if seen else None
                out[cam] = {"fps": round(sum(1 for t in seen if t >= now - window_s) / window_s, 1),
                            "age_s": None if last is None else round(now - last, 1)}
            return out

    def status(self, now=None):
        now = now if now is not None else time.time()
        age = lambda t: None if t is None else round(now - t, 1)  # noqa: E731
        return {
            "connected": self.connected,
            "cameras": self.camera_status(now),
            "camera_fps": round(self.rate("camera", now=now) / max(1, len(self._cam_seen)), 1),
            "pipelines": self.watchdog.status() if self.watchdog else None,
            "scene_hz": self.rate("scene", now=now),
            "camera_age_s": age(self.last_camera),
            "scene_age_s": age(self.last_scene),
            "bad": self.bad_msgs,
        }

    @property
    def latest_seq(self):
        return self._seq

    def points_after(self, seq):
        """(points, newest seq) for scores numbered after 'seq'.

        A score is stamped when scoring STARTS but stored when it ENDS, so a cursor based on time can
        pass a score that is not stored yet and lose it for good (about one in ten at idle, far more under
        load). The sequence number is only assigned at storing time, so nothing is skipped."""
        fresh = [p for p in list(self.history) if p["seq"] > seq]
        return ([(p["t"], p["HOTA"], p["DetA"], p["AssA"], p["covered"]) for p in fresh],
                fresh[-1]["seq"] if fresh else seq)

    def points_since(self, since):
        return [(p["t"], p["HOTA"], p["DetA"], p["AssA"], p["covered"]) for p in list(self.history) if p["t"] > since]
