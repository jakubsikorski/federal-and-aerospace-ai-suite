# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Remote viewer backend for the UAV Vision Analytics stack.

Runs on a machine *separate* from the UAV stack. Given the IP/hostname of a
host already running uav-vision-analytics (pymavlink or UAVSDK, with nginx
publishing its usual ports), this service:

  * Looks up which DL Streamer pipeline/model serves a given camera's RTSP
    stream, via the Pipeline Server's REST API (through nginx). A camera is a
    running pipeline with an RTSP frame destination (stream path `uav-<name>`).
  * Proxies the metrics-manager SSE stream, and streams this ground-station
    machine's own CPU/RAM utilization (`/api/local-metrics-stream`).
  * Bridges MQTT scene captions (`uav/caption/<camera>`) to the browser over SSE.
  * "H.264 Relay" video: whenever a remote pipeline publishes RTSP for a
    camera, the local viz-backend configures a mediamtx sidecar
    (`docker-compose-mediamtx.yml`) to pull that H.264 RTSP stream on demand
    and re-serve it as WebRTC -- pure remux, no transcoding (see `/api/relay-path`).
  * Natural-language video search via a separately deployed Video Search and
    Summarization (VSS) stack -- see vss_search.py.

Only ever makes outbound, read-only requests to the user-supplied host; it
never starts/stops pipelines on the remote stack.
"""

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
import paho.mqtt.client as mqtt
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import caption_log
import uav_naming
import vss_live_sync
from vss_search import router as vss_search_router
from vss_summary import router as vss_summary_router

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("viz-backend")

DEFAULT_REMOTE_HOST = os.environ.get("DEFAULT_REMOTE_HOST", "127.0.0.1")
REMOTE_HTTPS_PORT = int(os.environ.get("REMOTE_HTTPS_PORT", "443"))
REMOTE_MQTT_PORT = int(os.environ.get("REMOTE_MQTT_PORT", "1883"))
REMOTE_RTSP_PORT = int(os.environ.get("REMOTE_RTSP_PORT", "8555"))

# Local mediamtx sidecar used only for "H.264 Relay" mode (see module
# docstring) -- MEDIAMTX_API_BASE is the internal docker-network address this
# backend uses to (re)configure the relay path; the other two are what the
# *browser* needs to reach mediamtx's own WHEP viewer page directly (so they
# default to loopback but should be set to the ground-station box's LAN IP
# in .env if the browser runs on a different machine). None of these matter
# unless docker-compose-mediamtx.yml is also brought up.
MEDIAMTX_API_BASE = os.environ.get("MEDIAMTX_API_BASE", "http://mediamtx:9997")
MEDIAMTX_WEBRTC_HOST = os.environ.get("MEDIAMTX_WEBRTC_HOST", "127.0.0.1")
MEDIAMTX_WEBRTC_PORT = int(os.environ.get("MEDIAMTX_WEBRTC_PORT", "8889"))

# This ground station's own metrics-manager (intel/metrics-manager, same
# image/metric set as the remote host's), container name "gs-metrics-manager"
# -- deliberately distinct from the remote stack's own "metrics-manager"
# container so the two can coexist if someone ever runs both stacks on the
# same machine (same reasoning for the "gs-" prefix wherever it shows up
# below). Internal-only, no host port published; this backend proxies its
# SSE stream over the docker network, same pattern as /api/metrics-stream.
LOCAL_METRICS_MANAGER_BASE = os.environ.get("LOCAL_METRICS_MANAGER_BASE", "http://gs-metrics-manager:9090")


# A camera is identified by its RTSP stream path (for example "uav-front-camera"); the MQTT caption
# topic of a camera is "uav/caption/<name>". Both end up in URLs / subscriptions, so they are validated.
CAMERA_PATTERN = r"^[A-Za-z0-9_-]{1,48}$"
# IPv4 address or DNS hostname; the host ends up in HTTPS/RTSP/MQTT connections built from an f-string
# below.
HOST_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
HOST_PATTERN_RE = re.compile(HOST_PATTERN)
TOPIC_PATTERN = r"^uav/caption/[A-Za-z0-9_-]{1,48}$"
TOPIC_PATTERN_RE = re.compile(TOPIC_PATTERN)
# uav_object_detection_<det>[_<vlm>] (video file) or uav_camera_<det>[_<vlm>] (live camera); the older uav_object_detection_<device>_caption
# naming is still recognised so a UAV host that has not been updated keeps working.
_PIPELINE_NAME = re.compile(r"^uav_(?:object_detection|camera)_(cpu|gpu|npu)(?:_(cpu|gpu)|(_caption))?$")
# Display order of the camera cards: a camera whose name contains one of these
# words (e.g. "uav-front-camera" -> "front") is placed by that word's position;
# any other camera follows, alphabetically.
CAMERA_ORDER = [w.strip().lower() for w in os.environ.get("CAMERA_ORDER", "front,bottom,back").split(",") if w.strip()]

app = FastAPI(title="UAV Vision Analytics - Remote Viewer")


def _remote_base(host: str) -> str:
    return f"https://{host}:{REMOTE_HTTPS_PORT}"


def _dest_frame_for_path(destination: dict, frame_path: str) -> Optional[dict]:
    """Return the destination.frame entry matching frame_path.

    destination.frame is a single dict for RTSP-only pipelines, but a *list*
    of dicts (one per transport) for the combined pipeline's WebRTC-enabled
    variant (see uav-vision-analytics's _build_rtsp_caption_payload) -- this
    normalizes both shapes so callers don't need to know which one a given
    running instance used.
    """
    if not isinstance(destination, dict):
        return None
    frame = destination.get("frame")
    if isinstance(frame, dict):
        return frame if frame.get("path") == frame_path else None
    if isinstance(frame, list):
        for entry in frame:
            if isinstance(entry, dict) and entry.get("path") == frame_path:
                return entry
    return None


def _dest_frame_paths(destination: dict) -> list:
    """All destination paths present in destination.frame, regardless of
    whether it's a single dict (RTSP-only) or a list (RTSP+WebRTC combined
    variant) -- see _dest_frame_for_path for the shape rationale."""
    if not isinstance(destination, dict):
        return []
    frame = destination.get("frame")
    if isinstance(frame, dict):
        return [frame.get("path")]
    if isinstance(frame, list):
        return [entry.get("path") for entry in frame if isinstance(entry, dict)]
    return []


def _pipeline_identity(pipeline: dict) -> Optional[str]:
    """The REST API's request.pipeline.name is always the generic grouping
    name ("user_defined_pipelines"), not the actual pipeline that was
    started -- that's request.pipeline.version instead. Prefer version,
    falling back to name for older/unexpected response shapes.
    """
    return pipeline.get("version") or pipeline.get("name")


def _camera_title(path: str) -> str:
    """Stream path -> display name: "uav-front-camera" -> "front camera"."""
    name = path[4:] if path.startswith("uav-") else path
    return name.replace("-", " ").replace("_", " ")


def _camera_record(inst: dict, request: dict, path: str, frame: dict) -> dict:
    """Everything the dashboard shows about one camera, taken from the start request of its pipeline."""
    name = _pipeline_identity(request.get("pipeline") or {})
    params = request.get("parameters") or {}
    match = _PIPELINE_NAME.match(name or "")
    detection_device = match.group(1) if match else None
    vlm_device = match.group(2) if match else None
    captioning = bool(vlm_device) or bool(match and match.group(3)) or (match is None and "caption" in (name or ""))
    topic = None
    if captioning:
        default_name = (path[4:] if path.startswith("uav-") else path).replace("-", "_")
        topic = params.get("mqtt_topic") or f"uav/caption/{default_name}"
    return {
        "camera": path,
        "title": _camera_title(path),
        "pipeline_name": name,
        "state": inst.get("state"),
        "detection_device": detection_device,
        "detection_model": os.path.basename(params["detection_model"]) if params.get("detection_model") else None,
        "captioning": captioning,
        "vlm_device": vlm_device,
        "vlm_model": os.path.basename(params["captioner_model_path"]) if params.get("captioner_model_path") else None,
        "prompt": params.get("captioner_prompt"),
        "max_new_tokens": params.get("captioner_max_new_tokens"),
        "mqtt_topic": topic,
        "overlay": frame.get("overlay", True) is not False,
        "video": params.get("video"),
        # a live camera (RealSense, USB webcam) has a device node instead of a video file
        "device": params.get("device"),
        "rtsp_url": params.get("rtsp_url"),
        "camera_size": f"{params['camera_width']}x{params['camera_height']}" if params.get("device") and params.get("camera_width") else None,
        "label": params.get("overlay_label"),
        "frame_rate": params.get("frame_rate"),
    }


async def _instances_with_requests(client: httpx.AsyncClient, base: str, running_only: bool) -> list:
    """(instance, request) for the remote pipelines, RUNNING first and newest first, so stale or
    aborted runs on the same stream path never hide the live one."""
    resp = await client.get(f"{base}/pipelines/status")
    resp.raise_for_status()
    instances = resp.json()
    if not isinstance(instances, list):
        return []
    instances = sorted(instances, key=lambda inst: (0 if inst.get("state") == "RUNNING" else 1, -(inst.get("start_time") or 0)))
    found = []
    for inst in instances:
        if running_only and inst.get("state") != "RUNNING":
            continue
        inst_id = inst.get("id")
        if inst_id is None:
            continue
        detail_resp = await client.get(f"{base}/pipelines/{inst_id}")
        if detail_resp.status_code != 200:
            continue
        found.append((inst, (detail_resp.json().get("request") or {})))
    return found


def _cameras_from(instances: list) -> list:
    """One record per RTSP stream path, first (best) instance wins."""
    cameras, seen = [], set()
    for inst, request in instances:
        destination = request.get("destination") or {}
        for path in _dest_frame_paths(destination):
            if not isinstance(path, str) or not re.fullmatch(CAMERA_PATTERN, path) or path in seen:
                continue
            seen.add(path)
            frame = _dest_frame_for_path(destination, path) or {}
            cameras.append(_camera_record(inst, request, path, frame))
    return sorted(cameras, key=_camera_sort_key)


def _camera_sort_key(camera: dict) -> tuple:
    words = _camera_title(camera["camera"]).lower().split()
    rank = next((i for i, w in enumerate(CAMERA_ORDER) if w in words), len(CAMERA_ORDER))
    return (rank, camera["camera"])


class SessionUpsert(BaseModel):
    host: str = Field(..., min_length=1, max_length=253, pattern=HOST_PATTERN)
    label: Optional[str] = Field(None, max_length=64)
    boot_id: Optional[str] = None


class SessionDelete(BaseModel):
    host: str = Field(..., min_length=1, max_length=253, pattern=HOST_PATTERN)


_sessions: dict = {}  # host -> {"label": str|None, "connected_at": iso str}
_active_host: Optional[str] = None

# Random per-process identity, regenerated every time this backend starts
# (container redeploy, restart, crash recovery). The frontend compares
# this against the boot_id it last saw (stored in its own localStorage)
# and, if they differ, treats its saved session list as stale and does
# NOT auto-restore from it. boot_id makes that restart
# observable to the client so it can start clean, matching the operator's
# expectation that a fresh deploy begins with no UAVs connected.
_boot_id = uuid.uuid4().hex


@app.get("/api/sessions")
async def list_sessions():
    return {
        "sessions": [{"host": h, **v} for h, v in _sessions.items()],
        "active_host": _active_host if _active_host in _sessions else None,
        "boot_id": _boot_id,
    }


_SESSION_KEEPWARM_INTERVAL_SECONDS = 5.0


async def _sessions_keepwarm_loop():
    """Keep caption_log's per-(host, camera) MQTT subscribers warm for every
    *connected* UAV, not just whichever one happens to be the active/visible
    tab in someone's browser.
    """
    async with httpx.AsyncClient(verify=False, timeout=5.0, trust_env=False) as client:
        while True:
            try:
                hosts = list(_sessions.keys())
                for host in hosts:
                    try:
                        instances = await _instances_with_requests(client, _remote_base(host), running_only=True)
                    except (httpx.HTTPError, ValueError) as exc:
                        logger.debug("sessions-keepwarm: %s unreachable: %s", host, exc)
                        continue
                    for cam in _cameras_from(instances):
                        if cam.get("captioning") and cam.get("mqtt_topic"):
                            await caption_log.note_active(host, cam["camera"], cam["mqtt_topic"])
            except Exception:  # noqa: BLE001 -- this loop must never die, or every
                # session silently stops being kept warm until the next restart.
                logger.exception("sessions-keepwarm: unexpected error in keep-warm loop")
            await asyncio.sleep(_SESSION_KEEPWARM_INTERVAL_SECONDS)


@app.on_event("startup")
async def _start_sessions_keepwarm():
    asyncio.ensure_future(_sessions_keepwarm_loop())


@app.post("/api/sessions")
async def upsert_session(req: SessionUpsert, active: bool = Query(True)):
    global _active_host
    if req.boot_id != _boot_id:
        # Either this client has never fetched the current boot_id or the case this
        # exists to catch -- it last fetched one from *before* this process started
        # and hasn't refreshed it since. Reject rather than silently registering:
        # letting this through is exactly what would make a fresh redeploy look like
        # it never actually cleared anything.
        return JSONResponse(
            status_code=409,
            content={"ok": False, "error": "boot_id_mismatch", "boot_id": _boot_id},
        )
    existing = _sessions.get(req.host)
    _sessions[req.host] = {
        "label": req.label if req.label is not None else (existing or {}).get("label"),
        "connected_at": (existing or {}).get("connected_at") or datetime.now(timezone.utc).isoformat(),
    }
    if active:
        _active_host = req.host
    return {"ok": True}


@app.delete("/api/sessions")
async def delete_session(req: SessionDelete):
    global _active_host
    _sessions.pop(req.host, None)
    if _active_host == req.host:
        _active_host = next(iter(_sessions), None)
    return {"ok": True}


@app.get("/api/runtime-config")
async def runtime_config():
    return {
        "defaultHost": DEFAULT_REMOTE_HOST,
        "remoteHttpsPort": REMOTE_HTTPS_PORT,
        # Where the browser should reach the local mediamtx sidecar's own
        # WHEP viewer page for "H.264 Relay" mode. Harmless to send even if
        # docker-compose-mediamtx.yml isn't up; the dashboard only uses these
        # once /api/relay-path succeeds.
        "mediamtxWebrtcHost": MEDIAMTX_WEBRTC_HOST,
        "mediamtxWebrtcPort": MEDIAMTX_WEBRTC_PORT,
    }


@app.get("/api/pipeline-info")
async def pipeline_info(host: str = Query(..., pattern=HOST_PATTERN), camera: str = Query(..., pattern=CAMERA_PATTERN)):
    """Report which pipeline, devices and models currently serve the given camera's RTSP stream."""
    info = {
        "reachable": False,
        "camera": camera,
        "rtsp_path": camera,
        "title": _camera_title(camera),
        "pipeline_name": None,
        "state": None,
        "captioning": False,
        "caption_topic": None,
        # True once a remote pipeline instance publishes an RTSP frame destination for this camera's
        # path -- the dashboard then starts the H.264 relay; otherwise it shows a "No video stream" placeholder.
        "rtsp_available": False,
    }
    try:
        # Self-signed TLS on the remote nginx (see configs/nginx/ssl on that host) -- trusted-LAN tool,
        # same posture as the -k curl examples in the main README.
        async with httpx.AsyncClient(verify=False, timeout=5.0, trust_env=False) as client:
            instances = await _instances_with_requests(client, _remote_base(host), running_only=False)
        info["reachable"] = True
        record = next((c for c in _cameras_from(instances) if c["camera"] == camera), None)
        if record:
            info.update(record)
            info["caption_topic"] = record["mqtt_topic"]
            info["rtsp_available"] = True
            if record.get("captioning") and record.get("mqtt_topic"):
                # Keep the Flight Summary feature's caption history warm for
                # this (host, camera) -- cheap to call on every poll; it's a
                # no-op unless the subscriber doesn't exist yet or the topic
                # changed.
                asyncio.ensure_future(caption_log.note_active(host, camera, record["mqtt_topic"]))
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("pipeline-info lookup failed for host=%s camera=%s: %s", host, camera, exc)
    return info


@app.get("/api/detect-pipelines")
async def detect_pipelines(host: str = Query(..., pattern=HOST_PATTERN)):
    """Every camera (RUNNING pipeline with an RTSP stream) on the host, so the UI can show one card per
    camera and notice, by re-polling, when a pipeline is started, stopped or replaced."""
    return await _detect_running_cameras(host)


async def _detect_running_cameras(host: str) -> dict:
    """Shared by /api/detect-pipelines and the VSS live-stream sync."""
    result = {"reachable": False, "cameras": []}
    try:
        async with httpx.AsyncClient(verify=False, timeout=5.0, trust_env=False) as client:
            instances = await _instances_with_requests(client, _remote_base(host), running_only=True)
        result["reachable"] = True
        result["cameras"] = _cameras_from(instances)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("detect-pipelines lookup failed for host=%s: %s", host, exc)
    return result


_prev_cpu_times: Optional[tuple] = None

METRICS_RECONNECT_DELAY_S = 3


@app.get("/api/local-metrics-stream")
async def local_metrics_stream():
    """Proxy this ground station's own gs-metrics-manager SSE stream to the
    browser -- same metric set (CPU/RAM/GPU/NPU) and wire format as
    /api/metrics-stream, just reached directly over the docker network
    (internal-only, no host port) instead of via a remote host's nginx."""
    url = f"{LOCAL_METRICS_MANAGER_BASE}/metrics/stream"

    async def relay():
        try:
            async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        yield f"event: error\ndata: {json.dumps({'status': resp.status_code})}\n\n"
                        return
                    async for chunk in resp.aiter_bytes():
                        if chunk:
                            yield chunk
        except httpx.HTTPError as exc:
            logger.warning("local-metrics-stream proxy failed: %s", exc)
            yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(relay(), media_type="text/event-stream")


@app.get("/api/metrics-stream")
async def metrics_stream(host: str = Query(..., pattern=HOST_PATTERN)):
    """Proxy metrics-manager's SSE stream (via the remote nginx) to the browser."""
    base = _remote_base(host)
    url = f"{base}/metrics/stream"

    async def relay():
        try:
            async with httpx.AsyncClient(verify=False, timeout=None, trust_env=False) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        yield f"event: error\ndata: {json.dumps({'status': resp.status_code})}\n\n"
                        return
                    async for chunk in resp.aiter_bytes():
                        if chunk:
                            yield chunk
        except httpx.HTTPError as exc:
            logger.warning("metrics-stream proxy failed for host=%s: %s", host, exc)
            yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(relay(), media_type="text/event-stream")


@app.get("/api/metrics-stream-multi")
async def metrics_stream_multi(hosts: str = Query(...)):
    """Proxy *all* of several hosts' metrics-manager SSE streams over a
    single downstream SSE connection, each frame tagged with which host it
    came from (`{"host": "...", "timestamp": ..., "metrics": [...]}`).

    Replaces one /api/metrics-stream connection per connected UAV -- with
    the per-camera captions streams and this endpoint, the number of
    long-lived SSE connections the dashboard holds open no longer scales
    with the number of connected UAVs/cameras.
    """
    host_list = [h for h in hosts.split(",") if h]
    if not host_list:
        raise HTTPException(status_code=400, detail="hosts must be a non-empty comma-separated list")
    for h in host_list:
        if not HOST_PATTERN_RE.match(h):
            raise HTTPException(status_code=400, detail=f"invalid host: {h}")

    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    loop = asyncio.get_event_loop()

    async def pump_one(host: str):
        # Keep reconnecting: the remote metrics-manager goes away whenever its stack is
        # restarted (e.g. make pymav-down/up). The browser's SSE connection to this
        # endpoint stays open, so it never retries itself and the graphs would freeze.
        base = _remote_base(host)
        url = f"{base}/metrics/stream"
        while True:
            try:
                async with httpx.AsyncClient(verify=False, timeout=None, trust_env=False) as client:
                    async with client.stream("GET", url) as resp:
                        if resp.status_code != 200:
                            await queue.put({"host": host, "error": f"status {resp.status_code}"})
                        else:
                            async for line in resp.aiter_lines():
                                if not line.startswith("data:"):
                                    continue
                                try:
                                    payload = json.loads(line[len("data:"):].strip())
                                except json.JSONDecodeError:
                                    continue
                                payload["host"] = host
                                await queue.put(payload)
            except httpx.HTTPError as exc:
                logger.warning("metrics-stream-multi proxy failed for host=%s: %s", host, exc)
                await queue.put({"host": host, "error": str(exc)})
            await asyncio.sleep(METRICS_RECONNECT_DELAY_S)

    tasks = [loop.create_task(pump_one(h)) for h in host_list]

    async def relay():
        try:
            while True:
                item = await queue.get()
                yield f"data: {json.dumps(item)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            for t in tasks:
                t.cancel()

    return StreamingResponse(relay(), media_type="text/event-stream")


@app.get("/api/captions-stream")
async def captions_stream(host: str = Query(..., pattern=HOST_PATTERN), topic: str = Query(..., pattern=TOPIC_PATTERN)):
    """Bridge a caption topic (uav/caption/<name>, from pipeline-info's caption_topic) of the remote MQTT broker to the browser over SSE.

    With several cameras per UAV and several UAVs connected, one persistent
    MQTT connection per host is preferred over one per camera.
    """
    return await _captions_stream_impl(host, [topic])


@app.get("/api/captions-stream-multi")
async def captions_stream_multi(host: str = Query(..., pattern=HOST_PATTERN), topics: str = Query(...)):
    """Bridge *all* of one host's caption topics over a single MQTT
    connection / single SSE stream, each event tagged with which topic it
    came from. `topics` is a comma-separated list of `uav/caption/<name>`
    topics (validated individually against TOPIC_PATTERN, same as the
    single-topic endpoint)."""
    topic_list = [t for t in topics.split(",") if t]
    if not topic_list:
        raise HTTPException(status_code=400, detail="topics must be a non-empty comma-separated list")
    for t in topic_list:
        if not TOPIC_PATTERN_RE.match(t):
            raise HTTPException(status_code=400, detail=f"invalid topic: {t}")
    return await _captions_stream_impl(host, topic_list)


async def _captions_stream_impl(host: str, topics: list):
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)

    def on_message(_client, _userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        # Tag with the topic it arrived on so a multi-topic (multi-camera)
        # subscription can be demuxed back to the right card client-side.
        loop.call_soon_threadsafe(queue.put_nowait, {"topic": msg.topic, "payload": payload})

    def on_connect(client, _userdata, _flags, reason_code, _properties=None):
        if reason_code == 0:
            for t in topics:
                client.subscribe(t)
            logger.info("captions-stream: subscribed to %s on %s", topics, host)
        else:
            logger.warning("captions-stream: MQTT connect failed (%s) for %s", reason_code, host)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(host, REMOTE_MQTT_PORT, keepalive=30)
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach MQTT broker at {host}:{REMOTE_MQTT_PORT}: {exc}")

    client.loop_start()

    async def relay():
        try:
            yield f"event: connected\ndata: {json.dumps({'topics': topics})}\n\n"
            while True:
                item = await queue.get()
                yield f"data: {json.dumps(item)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            client.loop_stop()
            client.disconnect()

    return StreamingResponse(relay(), media_type="text/event-stream")


@app.post("/api/relay-path")
async def relay_path(host: str = Query(..., pattern=HOST_PATTERN), camera: str = Query(..., pattern=CAMERA_PATTERN)):
    """(Re)configure the local mediamtx sidecar's "relay-<host_tag>-<camera>"
    path to pull the remote host's H.264 RTSP stream on demand, for "H.264
    Relay" video mode.

    Namespaced by host, not just camera name: different UAVs commonly reuse
    the same camera names, and without the host in the path, a second UAV
    would silently repoint the first UAV's already-viewed mediamtx path.

    Idempotent/cheap to call on every connect -- mediamtx's config API
    accepts repeat PATCHes to the same path. Returns the path name for the
    dashboard's WHEP viewer iframe; raises 502 if mediamtx isn't reachable.
    """
    path_name = f"relay-{uav_naming.host_tag(host)}-{camera}"
    source = f"rtsp://{host}:{REMOTE_RTSP_PORT}/{camera}"
    # rtspTransport=tcp: the UAV host's nginx only forwards RTSP over TCP, so
    # "automatic" mode wastes several seconds trying UDP first on every connect.
    #
    # sourceOnDemandStartTimeout/CloseAfter raised from mediamtx's 10s default:
    # the UAV's RTSP encoder is rebuilt on every (re)connect and can take
    # longer than 10s under concurrent GPU/NPU load, which was tripping a
    # reconnect-storm seen as a stuck "source has timed out" error. Set here
    # per-path in addition to configs/mediamtx.yml's matching global default.
    body = {
        "source": source,
        "sourceOnDemand": True,
        "rtspTransport": "tcp",
        "sourceOnDemandStartTimeout": "60s",
        "sourceOnDemandCloseAfter": "60s",
    }
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            resp = await client.patch(f"{MEDIAMTX_API_BASE}/v3/config/paths/patch/{path_name}", json=body)
            if resp.status_code == 404:
                # Path doesn't exist yet -- create it instead of patching.
                resp = await client.post(f"{MEDIAMTX_API_BASE}/v3/config/paths/add/{path_name}", json=body)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("relay-path config failed for host=%s camera=%s: %s", host, camera, exc)
        raise HTTPException(status_code=502, detail=f"Cannot configure local mediamtx relay: {exc}")
    return {"path": path_name}


@app.get("/api/relay-status")
async def relay_status(host: str = Query(..., pattern=HOST_PATTERN), camera: str = Query(..., pattern=CAMERA_PATTERN)):
    """Lightweight health check for a "H.264 Relay" path, polled by the
    dashboard, so a dead WHEP session can be recovered automatically
    instead of only on a manual refresh click.
    """
    path_name = f"relay-{uav_naming.host_tag(host)}-{camera}"
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            resp = await client.get(f"{MEDIAMTX_API_BASE}/v3/paths/get/{path_name}")
            if resp.status_code == 404:
                # Path was never configured (e.g. this camera has never been
                # viewed in relay mode yet) -- not an error, just "no status".
                return {"path": path_name, "exists": False, "ready": False}
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("relay-status check failed for host=%s camera=%s: %s", host, camera, exc)
        raise HTTPException(status_code=502, detail=f"Cannot reach local mediamtx: {exc}")
    return {"path": path_name, "exists": True, "ready": bool(data.get("ready"))}


# VSS natural-language search proxy (separate VSS Compose stack) -- see vss_search.py.
vss_live_sync.configure(_detect_running_cameras, REMOTE_RTSP_PORT)
app.include_router(vss_search_router)
app.include_router(vss_live_sync.router)
# Flight Summary report -- built from the caption history, keeps
# warm via the note_active() call in pipeline_info() above.
app.include_router(vss_summary_router)


class RevalidateStaticAssets:
    """Make browsers revalidate the dashboard's HTML/CSS/JS on every load
    (cheap 304s via ETag) so a rebuilt image is never paired with stale
    cached assets. Pure ASGI so /api/* streams (SSE, video) are untouched."""

    def __init__(self, asgi_app):
        self.app = asgi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return

        async def send_with_cache_header(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                headers.append((b"cache-control", b"no-cache"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_cache_header)


app.add_middleware(RevalidateStaticAssets)

app.mount("/", StaticFiles(directory="static", html=True), name="static")
