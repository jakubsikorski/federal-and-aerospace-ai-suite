# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Dashboard backend: MQTT latency feed, SSE stream, traffic and Qbv controls."""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config
from .controllers.switch import create_switch
from .controllers.traffic import IperfServer, TrafficController
from .hota import LiveHota, load_ground_truth
from .latency import LatencyTracker
from .mqtt_source import MqttSource
from .pipeline_watchdog import PipelineWatchdog
from .scene_source import SceneSource
from .system_metrics import SystemMetrics, parse_sources

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
PUSH_INTERVAL_S = 0.5


class TrafficRequest(BaseModel):
    rate_mbps: int = Field(ge=0)


class QbvRequest(BaseModel):
    enabled: bool


def create_app(start_mqtt=True):
    tracker = LatencyTracker(
        history_s=config.HISTORY_S,
        window_s=config.STATS_WINDOW_S,
        stall_after_s=config.STALL_AFTER_S,
        max_latency_s=config.MAX_PLAUSIBLE_LATENCY_S,
    )
    if config.TRAFFIC_MODE == "iperf":
        config.require("TRAFFIC_MODE=iperf", M1_NOISE_IP=config.TRAFFIC_SERVER_IP, M4_LAB_IP=config.TRAFFIC_AGENT_URL)
    server = IperfServer(config.TRAFFIC_SERVER_IP, config.IPERF3_BIN) if config.TRAFFIC_MODE == "iperf" else None
    traffic = TrafficController(
        config.TRAFFIC_MODE,
        config.TRAFFIC_MAX_MBPS,
        config.TRAFFIC_AGENT_URL,
        server=server,
        ttl_s=config.TRAFFIC_TTL_S,
    )
    switch = create_switch(config)
    source = MqttSource(tracker, config.MQTT_HOST, config.MQTT_PORT, config.MQTT_TOPIC)
    system = None
    if config.METRICS_SOURCES and start_mqtt:
        try:
            system = SystemMetrics(parse_sources(config.METRICS_SOURCES))
        except ValueError as exc:
            log.warning("system load disabled: %s", exc)
    scene = None
    if config.SCENE_ENABLED and start_mqtt:
        try:
            hota = LiveHota(load_ground_truth(config.GT_PATH), window_s=config.HOTA_WINDOW_S, lag_s=config.HOTA_LAG_S)
            scene = SceneSource(hota, config.SCENE_MQTT_HOST, config.SCENE_MQTT_PORT, config.SCENE_CA)
            scene.frame_cameras = tuple(config.WATCHDOG_CAMERAS)
            if config.WATCHDOG_ENABLED:
                scene.watchdog = PipelineWatchdog(config.DLSPS_URL, config.WATCHDOG_CAMERAS)
        except (OSError, ValueError, ImportError) as exc:  # use case 1 keeps working without use case 2
            log.warning("use case 2 (HOTA) disabled: %s", exc)

    @asynccontextmanager
    async def lifespan(app):
        if start_mqtt:
            source.start()
        switch.start()
        traffic.start()
        if scene:
            scene.start()
            if scene.watchdog:
                scene.watchdog.start()
        if system:
            system.start()
        yield
        traffic.shutdown()
        switch.stop()
        if scene:
            scene.stop()
            if scene.watchdog:
                scene.watchdog.stop()
        if system:
            system.stop()
        if start_mqtt:
            source.stop()

    app = FastAPI(title="Deterministic Threat Detection", lifespan=lifespan)
    app.state.tracker, app.state.traffic = tracker, traffic
    app.state.switch, app.state.source, app.state.scene = switch, source, scene

    def status():
        return {
            "mqtt_connected": source.connected,
            "bad_messages": source.bad_messages,
            "traffic": traffic.state(),
            "qbv": switch.state(),
            "scene": scene.status() if scene else None,
            "hota": scene.latest if scene else None,
            "system": system.snapshot() if system else None,
        }

    @app.get("/api/config")
    def get_config():
        return {
            "window_s": config.STATS_WINDOW_S,
            "history_s": config.HISTORY_S,
            "traffic_max_mbps": config.TRAFFIC_MAX_MBPS,
            "traffic_mode": config.TRAFFIC_MODE,
            "switch_mode": config.SWITCH_MODE,
            "qbv_port": switch.port,
            "source": config.SCENE_SOURCE,
        }

    @app.get("/api/history")
    def history():
        return {"now": time.time(), "points": tracker.history()}

    @app.get("/api/status")
    def get_status():
        return {"sources": tracker.snapshot(), **status()}

    @app.post("/api/traffic")
    def set_traffic(req: TrafficRequest):
        traffic.set_rate(req.rate_mbps)
        return traffic.state()

    @app.get("/api/hota")
    def get_hota():
        if scene is None:
            raise HTTPException(status_code=404, detail="use case 2 (HOTA) is not enabled")
        return {"now": time.time(), "window_s": config.HOTA_WINDOW_S, "latest": scene.latest,
                "status": scene.status(), "points": scene.points_since(0)}

    @app.get("/api/frames/{cam}.mjpg")
    async def frames(cam: str):
        """The pipeline's own decoded + annotated frames as a motion-JPEG stream. Unlike the browser's
        WebRTC decoder, this shows what the tracker sees, including damage from lost packets."""
        if scene is None or cam not in config.WATCHDOG_CAMERAS:
            raise HTTPException(status_code=404, detail="unknown camera or use case 2 disabled")

        async def stream():
            scene.viewers += 1
            last = 0.0
            try:
                while True:
                    item = scene.latest_frame(cam)
                    if item and item[0] > last:
                        last, jpeg = item
                        yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                               + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
                    await asyncio.sleep(0.04)
            finally:
                scene.viewers -= 1

        return StreamingResponse(stream(), media_type="multipart/x-mixed-replace; boundary=frame",
                                 headers={"Cache-Control": "no-cache"})

    @app.get("/api/qbv")
    def get_qbv():
        return switch.state()

    @app.post("/api/qbv")
    def set_qbv(req: QbvRequest):
        try:
            switch.set_enabled(req.enabled)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return switch.state()

    @app.post("/api/reset")
    def reset():
        tracker.reset()
        return {"ok": True}

    @app.get("/api/stream")
    async def stream():
        async def events():
            last = time.time()
            hota_seq = max(0, scene.latest_seq - 5) if scene else 0  # resend the last few; the page ignores duplicates
            while True:
                now = time.time()
                hota_points = []
                if scene:
                    hota_points, hota_seq = scene.points_after(hota_seq)
                body = {
                    "now": now,
                    "points": tracker.points_since(last),
                    "sources": tracker.snapshot(now),
                    "hota_points": hota_points,
                    **status(),
                }
                last = now
                yield f"data: {json.dumps(body)}\n\n"
                await asyncio.sleep(PUSH_INTERVAL_S)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()
