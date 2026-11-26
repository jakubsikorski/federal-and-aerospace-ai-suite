# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Rolling, process-local performance KPIs for the Panel.

Time to alert, time to spoken answer, per-camera frame rates and drops,
approximate answer speed, peak memory and the Handheld's own view of
service calls (speech recognition, speech synthesis, first words). Every store is bounded and every
call is a few dictionary operations under one lock, so recording never slows
ingest, inference or playback. Targets are thresholds to compare against, not
a qualification: a value with too few samples is never reported as a pass.
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque

VERSION = 1
WINDOW_SECONDS = 300.0
CAMERA_WINDOW_SECONDS = 30.0
FPS_WINDOW_SECONDS = 5.0
OFFLINE_SECONDS = 5.0
MAX_SAMPLES = 2000
MIN_SAMPLES = 5
MAX_CAMERAS = 64
# Health thresholds: a camera that keeps up drops almost nothing.
MAX_DROP_PERCENT = 10.0
MIN_SOURCE_RATIO = 0.9
DEFAULT_TARGETS = {
    "time_to_alert_seconds": 0.4,
    "spoken_answer_seconds": 3.0,
    "smooth_cameras": 3,
    "tokens_per_second": 15.0,
    "memory_percent": 80.0,
}
VOICE_STAGES = ("listening", "hear", "think", "speak")
VOICE_OUTCOMES = ("spoken", "blocked", "failed")


def targets(cfg: dict) -> dict:
    """Configured thresholds; anything missing or not a positive number keeps the default."""
    chosen = dict(DEFAULT_TARGETS)
    for name, value in (cfg.get("performance_targets") or {}).items():
        if (name in chosen and isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value > 0):
            chosen[name] = value
    return chosen


def _number(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _percentile(ordered: list[float], q: float) -> float:
    """Nearest-rank percentile of an already sorted list."""
    return ordered[max(0, math.ceil(q / 100 * len(ordered)) - 1)]


class Window:
    """Samples from the last `seconds`, at most `size` of them."""

    def __init__(self, seconds: float = WINDOW_SECONDS, size: int = MAX_SAMPLES):
        self.seconds = seconds
        self.samples: deque = deque(maxlen=size)

    def add(self, value: float, now: float) -> None:
        self.samples.append((now, value))

    def values(self, now: float) -> list[float]:
        while self.samples and self.samples[0][0] < now - self.seconds:
            self.samples.popleft()
        return [value for _, value in self.samples]


def summary(values: list[float], target: float | None, digits: int = 3) -> dict:
    """Last, p50, p95 and count. Status compares p95 with a lower-is-better target."""
    if not values:
        return {"last": None, "p50": None, "p95": None, "count": 0,
                "target": target, "status": "no_data"}
    ordered = sorted(values)
    p95 = _percentile(ordered, 95)
    if len(values) < MIN_SAMPLES:
        status = "insufficient"
    elif target is None:
        status = "info"
    else:
        status = "on_target" if p95 < target else "missed"
    return {"last": round(values[-1], digits), "p50": round(_percentile(ordered, 50), digits),
            "p95": round(p95, digits), "count": len(values), "target": target, "status": status}


def _handheld(values: dict, unit: str) -> dict:
    """A target-free summary labelled as measured by the Handheld. Fewer than
    MIN_SAMPLES keeps the summary's 'insufficient' status, never a typical value."""
    return {**values, "unit": unit, "source": "handheld"}


class Camera:
    def __init__(self):
        self.session = None
        self.counts: deque = deque(maxlen=MAX_SAMPLES)
        self.pipeline = Window(CAMERA_WINDOW_SECONDS)
        self.delay = Window(CAMERA_WINDOW_SECONDS)
        self.source_fps = None
        self.last = None


class Performance:
    def __init__(self, cfg: dict, clock=time.monotonic, wall=time.time):
        self.targets = targets(cfg)
        self.clock, self.wall = clock, wall
        self.lock = threading.Lock()
        self.cameras: dict[str, Camera] = {}
        self.alert = Window()
        self.voice = Window()
        self.stages = {name: Window() for name in VOICE_STAGES}
        self.ack = Window()
        self.untimed = Window()
        self.words_per_second = Window()
        self.first_words = Window()
        # Handheld-observed service calls: successful end-to-end durations only.
        self.asr_seconds, self.asr_rtfx = Window(), Window()
        self.tts_seconds, self.tts_rtf = Window(), Window()
        self.memory = Window()
        self.memory_sampled = None

    # -- recording ---------------------------------------------------------
    def frame(self, stream_id: str, context: dict, received_at: float | None = None) -> None:
        """One published observation. Counters are cumulative per tracking session,
        so a lost MQTT message never breaks a rate: rates come from deltas."""
        now = self.clock()
        received_at = self.wall() if received_at is None else received_at
        frames_out = context.get("frame_sequence")
        frames_in = context.get("frames_in")
        if not isinstance(frames_out, int) or isinstance(frames_out, bool):
            return
        if not isinstance(frames_in, int) or isinstance(frames_in, bool):
            frames_in = None
        with self.lock:
            camera = self.cameras.get(stream_id)
            if camera is None:
                if len(self.cameras) >= MAX_CAMERAS:
                    return
                camera = self.cameras[stream_id] = Camera()
            session = context.get("tracking_session_id")
            if camera.session != session or (camera.counts and frames_out < camera.counts[-1][2]):
                # A restarted pipeline counts from zero again.
                camera.session = session
                camera.counts.clear()
            camera.counts.append((now, frames_in, frames_out))
            while len(camera.counts) > 1 and camera.counts[1][0] <= now - FPS_WINDOW_SECONDS:
                camera.counts.popleft()
            camera.last = now
            source_fps = _number(context.get("source_fps"))
            # RTSP caps can report 0/1 or a clock rate; only plausible rates are compared.
            camera.source_fps = source_fps if source_fps and 1 <= source_fps <= 240 else None
            pipeline_ms = _number(context.get("pipeline_ms"))
            if pipeline_ms is not None and pipeline_ms >= 0:
                camera.pipeline.add(pipeline_ms / 1000, now)
            ingress_at = _number(context.get("ingress_at"))
            # Cross-container interval on one host clock; a negative value means
            # the clocks disagree, which is not a latency.
            if ingress_at is not None and 0 <= received_at - ingress_at < 60:
                camera.delay.add(received_at - ingress_at, now)

    def alert_raised(self, seconds: float) -> None:
        """Decoded frame of the object's first sighting to the alert being created."""
        if _number(seconds) is not None and 0 <= seconds < 3600:
            with self.lock:
                self.alert.add(seconds, self.clock())

    def voice_turn(self, outcome: str, spoken_answer_seconds: float | None = None,
                   stages: dict | None = None, ack_seconds: float | None = None) -> None:
        """Browser-measured: end of speech to the first sound of the answer."""
        now = self.clock()
        with self.lock:
            if outcome != "spoken" or spoken_answer_seconds is None:
                self.untimed.add(outcome, now)
                return
            self.voice.add(spoken_answer_seconds, now)
            for name, value in (stages or {}).items():
                if name in self.stages:
                    self.stages[name].add(value, now)
            if ack_seconds is not None:
                self.ack.add(ack_seconds, now)

    def answer_streamed(self, first_text_seconds: float, generation_seconds: float,
                        text: str) -> None:
        """Approximate speed from streamed text. Text chunks are not tokens, so
        this is reported as words per second and never against the token target."""
        now = self.clock()
        words = len(text.split())
        streaming = generation_seconds - first_text_seconds
        with self.lock:
            if first_text_seconds > 0:
                self.first_words.add(first_text_seconds, now)
            if words >= 5 and streaming >= 0.2:
                self.words_per_second.add(words / streaming, now)

    def asr_call(self, seconds: float, audio_seconds: float) -> None:
        """One successful transcription as the Backend saw it: request time
        (includes queue wait and loopback) and the speed factor over real time."""
        self._service_call(self.asr_seconds, self.asr_rtfx, seconds, audio_seconds,
                           lambda: audio_seconds / seconds)

    def tts_call(self, seconds: float, audio_seconds: float) -> None:
        """One successful synthesis (not a cache hit). SpeechT5 returns the whole
        WAV, so request time is also the time to first audio."""
        self._service_call(self.tts_seconds, self.tts_rtf, seconds, audio_seconds,
                           lambda: seconds / audio_seconds)

    def _service_call(self, request_window: Window, ratio_window: Window, seconds, audio_seconds,
                      ratio) -> None:
        seconds, audio_seconds = _number(seconds), _number(audio_seconds)
        if seconds is None or audio_seconds is None or seconds <= 0 or audio_seconds <= 0:
            return
        now = self.clock()
        with self.lock:
            request_window.add(seconds, now)
            ratio_window.add(ratio(), now)

    def _sample_memory(self, telemetry: dict | None, now: float) -> None:
        for metric in (telemetry or {}).get("metrics") or []:
            if metric.get("id") != "memory_used_percent" or metric.get("state") != "ok":
                continue
            value = _number(metric.get("value"))
            sampled = metric.get("sampled_utc")
            if value is not None and sampled != self.memory_sampled:
                self.memory_sampled = sampled
                self.memory.add(value, now)

    # -- reading -----------------------------------------------------------
    def _camera(self, camera: Camera | None, now: float) -> dict:
        if camera is None or camera.last is None:
            return {"health": "no_data"}
        first, last = camera.counts[0], camera.counts[-1]
        span = last[0] - first[0]
        processed = ingress = drop = None
        if span >= 1.0:
            processed = (last[2] - first[2]) / span
            if first[1] is not None and last[1] is not None and last[1] > first[1]:
                ingress = (last[1] - first[1]) / span
                drop = max(0.0, 100.0 * (1 - (last[2] - first[2]) / (last[1] - first[1])))
        pipeline = summary(camera.pipeline.values(now), None)
        delay = summary(camera.delay.values(now), self.targets["time_to_alert_seconds"])
        if now - camera.last > OFFLINE_SECONDS:
            health, reason = "offline", "No frames for over 5 s"
        elif processed is None:
            health, reason = "starting", "Measuring"
        elif drop is not None and drop > MAX_DROP_PERCENT:
            health, reason = "dropping", f"{drop:.0f}% of decoded frames skipped"
        elif camera.source_fps and processed < MIN_SOURCE_RATIO * camera.source_fps:
            health, reason = "dropping", "Processing slower than the camera"
        elif delay["status"] == "missed":
            health, reason = "delayed", "Detection delay above target"
        else:
            health, reason = "smooth", None
        rounded = lambda value: None if value is None else round(value, 1)  # noqa: E731
        return {"health": health, "reason": reason, "source_fps": rounded(camera.source_fps),
                "ingress_fps": rounded(ingress), "processed_fps": rounded(processed),
                "drop_percent": rounded(drop), "pipeline_seconds": pipeline,
                "detection_delay_seconds": delay}

    def snapshot(self, stream_ids: list[str] | None = None, telemetry: dict | None = None) -> dict:
        now = self.clock()
        targets = self.targets
        with self.lock:
            self._sample_memory(telemetry, now)
            ids = list(self.cameras) if stream_ids is None else list(stream_ids)
            cameras = {stream_id: self._camera(self.cameras.get(stream_id), now) for stream_id in ids}
            alert = summary(self.alert.values(now), targets["time_to_alert_seconds"])
            voice = summary(self.voice.values(now), targets["spoken_answer_seconds"])
            stages = {name: summary(window.values(now), None)["p50"]
                      for name, window in self.stages.items()}
            ack = summary(self.ack.values(now), None)["p50"]
            untimed = len(self.untimed.values(now))
            speed = summary(self.words_per_second.values(now), None, digits=1)
            first_words_all = summary(self.first_words.values(now), None)
            first_words = first_words_all["p50"]
            asr = summary(self.asr_seconds.values(now), None), summary(self.asr_rtfx.values(now), None, 2)
            tts = summary(self.tts_seconds.values(now), None), summary(self.tts_rtf.values(now), None, 2)
            memory_values = self.memory.values(now)
        running = [c for c in cameras.values() if c["health"] not in ("no_data", "offline")]
        smooth = sum(c["health"] == "smooth" for c in running)
        wanted = targets["smooth_cameras"]
        if not running or any(c["health"] == "starting" for c in running):
            camera_status = "no_data" if not running else "insufficient"
        elif smooth < len(running):
            camera_status = "missed"
        else:
            # All smooth, but capacity is only shown once enough cameras run.
            camera_status = "on_target" if smooth >= wanted else "insufficient"
        delays = sorted(c["detection_delay_seconds"]["p50"] for c in running
                        if c["detection_delay_seconds"]["p50"] is not None)
        peak = max(memory_values) if memory_values else None
        memory_target = targets["memory_percent"]
        return {
            "version": VERSION, "window_seconds": WINDOW_SECONDS, "generated_at": self.wall(),
            "kpis": {
                "time_to_alert": {**alert, "unit": "s",
                                  "detection_delay_p50": delays[len(delays) // 2] if delays else None},
                "spoken_answer": {**voice, "unit": "s", "stages": stages,
                                  "acknowledged_p50": ack, "untimed": untimed},
                "cameras": {"smooth": smooth, "running": len(running), "target": wanted,
                            "status": camera_status},
                "answer_speed": {**speed, "unit": "words/s", "approximate": True, "target": None,
                                 "first_words_p50": first_words, "tokens_per_second": None,
                                 "token_target": targets["tokens_per_second"]},
                "memory": {"last": memory_values[-1] if memory_values else None, "peak": peak,
                           "count": len(memory_values), "target": memory_target, "unit": "%",
                           "status": ("no_data" if peak is None else "insufficient"
                                      if len(memory_values) < MIN_SAMPLES else
                                      "on_target" if peak < memory_target else "missed")},
                # Observed by the Handheld, so it includes queue wait and loopback.
                # No target: the service-reported series replace these when present.
                "services": {
                    "chat": {"first_words": _handheld(first_words_all, "s")},
                    "asr": {"request_seconds": _handheld(asr[0], "s"),
                            "rtfx": _handheld(asr[1], "x")},
                    "tts": {"request_seconds": _handheld(tts[0], "s"),
                            "rtf": _handheld(tts[1], "s per s")},
                },
            },
            "cameras": cameras,
        }


MAX_MARK_MS = 120_000


def parse_voice_mark(body) -> dict:
    """Validate a browser voice-turn mark; returns voice_turn() keyword arguments.

    {"kind": "voice_turn", "outcome": "spoken"|"blocked"|"failed",
     "spoken_answer_ms": int, "ack_ms": int, "stages": {stage: int}}
    Durations are whole milliseconds on the browser's monotonic clock.
    """
    def duration(value, name):
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_MARK_MS:
            raise ValueError(f"{name} must be whole milliseconds from 0 to {MAX_MARK_MS}")
        return value / 1000

    if (not isinstance(body, dict) or body.get("kind") != "voice_turn"
            or set(body) - {"kind", "outcome", "spoken_answer_ms", "ack_ms", "stages"}):
        raise ValueError("provide kind voice_turn with outcome and optional timings only")
    outcome = body.get("outcome")
    if outcome not in VOICE_OUTCOMES:
        raise ValueError(f"outcome must be one of {', '.join(VOICE_OUTCOMES)}")
    if outcome != "spoken":
        if set(body) - {"kind", "outcome"}:
            raise ValueError("only a spoken turn carries timings")
        return {"outcome": outcome}
    stages = body.get("stages", {})
    if not isinstance(stages, dict) or set(stages) - set(VOICE_STAGES):
        raise ValueError(f"stages may only name {', '.join(VOICE_STAGES)}")
    return {"outcome": outcome,
            "spoken_answer_seconds": duration(body.get("spoken_answer_ms"), "spoken_answer_ms"),
            "stages": {name: duration(value, name) for name, value in stages.items()},
            "ack_seconds": duration(body["ack_ms"], "ack_ms") if "ack_ms" in body else None}
