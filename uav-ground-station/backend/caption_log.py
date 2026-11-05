# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""In-memory ring buffer of MQTT scene captions, feeding the summary report.

main.py's /api/captions-stream bridges one live SSE connection at a time and
keeps nothing once the browser disconnects. vss_summary.py needs the *history*
of what a camera has seen over a window (e.g. "last 15 minutes") to produce a
report, so this module runs its own small set of background MQTT subscribers
-- one per (host, camera) pair currently known to be captioning (as reported
by main.py's pipeline-info polling, the same detector vss_live_sync.py uses)
-- and keeps their messages in a bounded deque. Nothing is persisted to disk;
a restart of this container loses history, same as every other piece of
ephemeral state in viz-backend.

Cameras here are identified the same way the rest of viz-backend identifies
them: by their RTSP stream path (e.g. "uav-front-camera"), *not* by compute
device -- two cameras can both run detection/captioning on "gpu" and still be
reported completely separately. The MQTT topic a camera's captions arrive on
is whatever main.py's pipeline-info lookup resolved for it (usually
"uav/caption/<camera-derived-name>", but can be a custom topic set via the
pipeline's mqtt_topic parameter) -- callers must pass that resolved topic in,
rather than this module trying to re-derive it from the camera name.

Routes: none (this module is a library used by vss_summary.py); call
``note_active(host, camera, topic)`` whenever a caller observes a camera is
running with captioning on, to (re)start its subscriber. Idle (host, camera)
pairs are garbage-collected after ``IDLE_TTL_SECONDS`` of no ``note_active``
calls, so switching to a different UAV host or a camera going offline doesn't
leak MQTT connections forever.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

import paho.mqtt.client as mqtt

logger = logging.getLogger("viz-backend.caption-log")

REMOTE_MQTT_PORT = int(os.environ.get("REMOTE_MQTT_PORT", "1883"))
CAPTION_BUFFER_SIZE = max(50, int(os.environ.get("CAPTION_BUFFER_SIZE", "1000")))
IDLE_TTL_SECONDS = max(30.0, float(os.environ.get("CAPTION_LOG_IDLE_TTL_SECONDS", "120")))
GC_INTERVAL_SECONDS = 30.0


@dataclass
class CaptionEntry:
    ts: float  # client-side receipt time (unix seconds) -- captions don't
    # reliably carry their own timestamp across every pipeline version, and
    # receipt time is accurate enough for a "last N minutes" report.
    text: str
    raw: dict


class _Subscriber:
    def __init__(self, host: str, camera: str, topic: str):
        self.host = host
        self.camera = camera
        self.topic = topic
        self.buffer: deque = deque(maxlen=CAPTION_BUFFER_SIZE)
        self.last_seen_active = time.monotonic()
        self._client: Optional[mqtt.Client] = None
        self._loop = asyncio.get_event_loop()

    def start(self) -> None:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        try:
            client.connect(self.host, REMOTE_MQTT_PORT, keepalive=30)
        except OSError as exc:
            logger.warning("caption-log: cannot reach MQTT broker %s:%s for %s/%s: %s",
                            self.host, REMOTE_MQTT_PORT, self.host, self.camera, exc)
            return
        client.loop_start()
        self._client = client

    def stop(self) -> None:
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()
            self._client = None

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties=None):
        if reason_code == 0:
            client.subscribe(self.topic)
            logger.info("caption-log: subscribed to %s on %s (camera=%s)", self.topic, self.host, self.camera)
        else:
            logger.warning("caption-log: MQTT connect failed (%s) for %s on %s", reason_code, self.topic, self.host)

    def _on_message(self, _client, _userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        text = None
        if isinstance(payload, dict):
            # Real gvagenai/mqtt_publisher payloads look like:
            #   {"metadata": {"result": "<caption text>"}, "metrics": {...}}
            # Older/synthetic test payloads used flatter shapes; keep those as
            # fallbacks so this stays compatible with both.
            metadata = payload.get("metadata")
            if isinstance(metadata, dict):
                text = metadata.get("result") or metadata.get("caption") or metadata.get("text")
            if not text:
                text = payload.get("caption") or payload.get("text") or payload.get("message")
        if not text:
            logger.debug("caption-log: dropped message on %s with no recognizable caption text: %s",
                         self.topic, str(payload)[:200])
            return
        entry = CaptionEntry(ts=time.time(), text=str(text), raw=payload if isinstance(payload, dict) else {})

        def append():
            self.buffer.append(entry)

        self._loop.call_soon_threadsafe(append)


_subscribers: dict = {}
_lock: Optional[asyncio.Lock] = None
_gc_task: Optional[asyncio.Task] = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def note_active(host: str, camera: str, topic: str) -> None:
    """Tell caption_log that (host, camera) is currently running with
    captioning on, on the given MQTT topic -- (re)starts its subscriber if
    needed (or restarts it if the topic changed, e.g. a pipeline was
    restarted with a different mqtt_topic override) and refreshes its idle
    TTL. Cheap to call on every pipeline-info poll."""
    key = (host, camera)
    async with _get_lock():
        sub = _subscribers.get(key)
        if sub is None or sub.topic != topic:
            if sub is not None:
                sub.stop()
            sub = _Subscriber(host, camera, topic)
            sub.start()
            _subscribers[key] = sub
        sub.last_seen_active = time.monotonic()
        _ensure_gc_task()


def get_captions(host: str, camera: str, since_ts: float) -> list:
    """Return CaptionEntry objects for (host, camera) with ts >= since_ts,
    oldest first. Empty list if no subscriber has ever been started for this
    pair (e.g. captioning was never observed running)."""
    sub = _subscribers.get((host, camera))
    if sub is None:
        return []
    return [e for e in sub.buffer if e.ts >= since_ts]


def known_cameras(host: str) -> list:
    return sorted(camera for (h, camera) in _subscribers if h == host)


def _ensure_gc_task() -> None:
    global _gc_task
    if _gc_task is None or _gc_task.done():
        _gc_task = asyncio.ensure_future(_gc_loop())


async def _gc_loop() -> None:
    while True:
        await asyncio.sleep(GC_INTERVAL_SECONDS)
        now = time.monotonic()
        async with _get_lock():
            stale = [key for key, sub in _subscribers.items() if now - sub.last_seen_active > IDLE_TTL_SECONDS]
            for key in stale:
                _subscribers.pop(key).stop()
                logger.info("caption-log: dropped idle subscriber %s/%s", key[0], key[1])
            if not _subscribers:
                return  # let the task end; note_active() will restart it later
