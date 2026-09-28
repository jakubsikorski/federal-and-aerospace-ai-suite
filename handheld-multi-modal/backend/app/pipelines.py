"""DLSPS pipeline lifecycle: one fixed instance per enabled camera.

Backend owns start and stop; the DLSPS config ships `auto_start: false`. The
pipeline itself is never edited at runtime (application contract). The source URI
and crop store are request parameters; the stock WebRTC frame destination uses
the camera's MediaMTX peer ID.
"""
from __future__ import annotations

import json
import hashlib
import logging
import re
import shlex
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import httpx
import yaml

from .core import crop_store_busy

log = logging.getLogger("backend.pipelines")

# The CPU, GPU and NPU variants each declare a differently named pipeline, so a
# name hardcoded here works for exactly one of them and silently leaves the
# others unstartable. Take the name from the same config file DLSPS loads.
PIPELINE_CONFIG_PATH = "/config/model-placement/dlsps.json"
DEFAULT_PIPELINE_NAME = "handheld_npu_detect"
PIPELINE = f"user_defined_pipelines/{DEFAULT_PIPELINE_NAME}"


def resolve_pipeline_name(path: str = PIPELINE_CONFIG_PATH, override: str | None = None) -> str:
    """Name of the single pipeline definition the deployed DLSPS config declares."""
    body = json.loads(Path(path).read_text())
    definitions = body["config"]["pipelines"]
    names = [entry["name"] for entry in definitions]
    if not override and len(names) != 1:
        raise ValueError(f"select dlsps_pipeline_name from {names}")
    name = override if override is not None else names[0]
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise ValueError(f"unusable DLSPS pipeline name {name!r}")
    if names.count(name) != 1:
        raise ValueError(f"DLSPS pipeline {name!r} must occur exactly once in {path}; available: {names}")
    definition = next(entry for entry in definitions if entry["name"] == name)
    properties = definition.get("parameters", {}).get("properties", {})
    for required in ("crop-config", "model_instance"):
        if properties.get(required, {}).get("type") != "string":
            raise ValueError(f"DLSPS pipeline {name!r} must accept string parameter {required!r}")
    required = set(definition.get("parameters", {}).get("required", []))
    if required - {"crop-config", "model_instance"}:
        raise ValueError(f"DLSPS pipeline {name!r} requires unsupported parameters: {sorted(required)}")
    return name


# Stock short-term gvatrack writes exactly 1.0 on boxes it predicted without
# inference (phase 8 evidence, tracker_confidence_probe.txt). Only then is 1.0 a
# marker rather than a score; zero-term tracking never predicts, so nothing is
# discarded there. Set by configure() from the selected pipeline definition.
PREDICTED_CONFIDENCE: float | None = None


def configure(name: str, path: str | None = None) -> None:
    """Point Backend at the pipeline definition it owns, before any camera starts."""
    global PIPELINE, PREDICTED_CONFIDENCE
    PIPELINE = f"user_defined_pipelines/{name}"
    PREDICTED_CONFIDENCE = tracker_predicted_confidence(path or PIPELINE_CONFIG_PATH, name)


def tracker_predicted_confidence(path: str, name: str) -> float | None:
    """1.0 when the selected pipeline's tracker is a verified short-term (predicting) type."""
    body = json.loads(Path(path).read_text())
    definition = next(p for p in body["config"]["pipelines"] if p["name"] == name)
    for part in definition["pipeline"].split("!"):
        if "gvatrack " in part:
            properties = dict(t.split("=", 1) for t in shlex.split(part) if "=" in t)
            if properties.get("tracking-type", "").startswith("short-term"):
                return 1.0
    return None


def validate_detector_profile(metadata_path: str, profile_path: str,
                              path: str = PIPELINE_CONFIG_PATH, name: str | None = None) -> None:
    """Bind the selected pipeline to approved weights and ordered class metadata."""
    name = resolve_pipeline_name(path, name)
    body = json.loads(Path(path).read_text())
    definition = next(p for p in body["config"]["pipelines"] if p["name"] == name)
    detectors = [part for part in definition["pipeline"].split("!") if "gvadetect " in part]
    if len(detectors) != 1:
        raise ValueError("detector profile requires exactly one declared gvadetect stage")
    properties = dict(token.split("=", 1) for token in shlex.split(detectors[0]) if "=" in token)
    profile = json.loads(Path(profile_path).read_text())
    if properties.get("model") != profile["pipeline_model"]:
        raise ValueError("selected pipeline model does not match the approved detector profile")
    metadata = yaml.safe_load(Path(metadata_path).read_text())
    names = metadata.get("names")
    if isinstance(names, dict):
        keys = sorted(names, key=lambda key: int(key))
        if [int(key) for key in keys] != list(range(len(keys))):
            raise ValueError("detector class IDs must match the approved zero-based taxonomy")
        names = [names[key] for key in keys]
    if metadata.get("task") != "detect" or names != profile["classes"]:
        raise ValueError("detector class metadata does not match the approved model taxonomy")
    model = Path(profile["pipeline_model"])
    required_files = {model.name, model.with_suffix(".bin").name}
    if not profile["artifact_sets"] or any(set(approved) != required_files for approved in profile["artifact_sets"]):
        raise ValueError("detector profile must pin both IR model and weights")
    directory = Path(metadata_path).parent
    actual = {}
    for filename in {filename for approved in profile["artifact_sets"] for filename in approved}:
        if Path(filename).name != filename:
            raise ValueError("detector profile artifact must be a filename")
        with (directory / filename).open("rb") as handle:
            actual[filename] = hashlib.file_digest(handle, "sha256").hexdigest()
    if not any(approved and all(actual[file] == digest for file, digest in approved.items())
               for approved in profile["artifact_sets"]):
        raise ValueError("detector artifacts do not match approved profile digests")


# Every camera needs its own crop store: the producer holds an exclusive flock
# on <output_dir>/.lock, so a shared directory would fail the second pipeline.
# One template serves every camera; the request's stream_id names the store
# (<output_dir>/<stream_id>), so a camera added at runtime needs no new file.
CROP_CONFIG_DIR = "/opt/handheld-crops-config"

# States in which an instance still holds real resources: its source connection,
# the NPU model instance, the crop lock and the WHIP publisher. BACKOFF_WAIT is
# in this list deliberately - it is not idle, it is actively retrying its source
# on a timer, so a BACKOFF_WAIT instance left behind keeps reconnecting forever
# (observed 2026-09-15: a retired camera's instance still polling a dead RTSP
# path long after that camera was removed from the config).
ACTIVE_STATES = frozenset({"RUNNING", "QUEUED", "BACKOFF_WAIT", "RECONNECTING"})

# Consecutive failed starts per camera, and the earliest time we may try again.
#
# A pipeline can accept the POST and *then* die asynchronously - a bad crop
# config, an unreadable lock, a missing model. The start looks successful, the
# instance lands in ERROR, the next pass sees no running pipeline and starts
# another one. Without a backoff that loop spawns a fresh doomed instance every
# supervisor interval and abandons the previous one, which is how a single
# permission error produced a dozen dead pipelines in two minutes.
_failures: dict[str, int] = {}
_retry_after: dict[str, float] = {}
_accepted_at: dict[str, float] = {}
_cleaned_terminal: set[str] = set()
_lifecycle_lock = threading.RLock()
shutdown = threading.Event()
# Set by a camera change so the supervisor reconciles now, not up to one
# supervise_seconds later.
wake = threading.Event()
MAX_BACKOFF_PASSES = 32
START_TERMINAL_STATES = frozenset({"ERROR", "ABORTED", "COMPLETED"})


class PipelineStartFailed(RuntimeError):
    def __init__(self, state: str):
        super().__init__(f"pipeline entered terminal state {state}")
        self.state = state


@contextmanager
def lifecycle_lock():
    """Serialize supervisor, reset, startup, and shutdown DLSPS operations."""
    with _lifecycle_lock:
        yield


def forget_backoff(stream_id: str) -> None:
    """Drop a camera's backoff so an operator action retries immediately."""
    _failures.pop(stream_id, None)
    _retry_after.pop(stream_id, None)


def _request_body(stream: dict) -> dict:
    return {
        "source": {"uri": stream["source_uri"], "type": "uri"},
        "destination": {
            "frame": {
                "type": "webrtc",
                "peer-id": stream["annotated_path"],
                "overlay": True,
                # Live display prefers the newest frame over a deep backlog.
                "cache-length": 1,
                "sync-with-source": False,
                "sync-with-destination": False,
            }
        },
        "parameters": {
            # JSON *text*, not an object: gvapython's kwarg property parses the
            # value itself and DLSPS validates the schema entry as a string.
            "crop-config": json.dumps(
                {"config": f"{CROP_CONFIG_DIR}/template.json",
                 "stream_id": stream["stream_id"]}
                | ({"predicted_confidence": PREDICTED_CONFIDENCE}
                   if PREDICTED_CONFIDENCE is not None else {})
            ),
            # Unique per start, not per camera and never shared. A shared
            # model-instance-id loads the model on the NPU once, but disposing
            # any one pipeline then tears that instance down and aborts every
            # camera using it; reusing a camera's own id is just as bad, because
            # the leaked pipeline it is replacing still holds that instance and
            # the replacement hangs in preroll waiting for it. Measured
            # 2026-09-14, docs/application/evidence/phase2/model_instance_isolation.txt.
            "model_instance": f"inst_{stream['stream_id']}_{uuid.uuid4().hex[:8]}",
        },
    }


def evict_stale_media(client: httpx.Client, mediamtx: str, stream: dict) -> None:
    """Drop sessions left behind by a leaked pipeline, before starting its replacement.

    gvapython in the pinned image has no teardown hook, so an aborted pipeline
    keeps its WebRTC publisher and its RTSP reader alive. MediaMTX allows one
    publisher per path, so the zombie holds `annotatedN` and the replacement
    never leaves QUEUED; the orphaned readers also keep decoding. Both were
    observed 2026-09-14 (five stale readers on cam1) - see
    docs/application/evidence/phase2/model_instance_isolation.txt.

    Only this camera's own sessions are touched: its annotated publisher, and
    readers of its source. The source publisher itself is the camera and is
    never kicked.
    """
    def kick(kind: str, session_id: str) -> None:
        try:
            client.post(f"{mediamtx}/v3/{kind}/kick/{session_id}", timeout=5.0)
            log.info("evicted stale %s %s for camera %s", kind, session_id[:8],
                     stream["stream_id"])
        except httpx.HTTPError as exc:
            log.warning("could not evict %s %s: %s", kind, session_id[:8], exc)

    try:
        webrtc = client.get(f"{mediamtx}/v3/webrtcsessions/list", timeout=5.0).json()
        rtsp = client.get(f"{mediamtx}/v3/rtspsessions/list", timeout=5.0).json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("MediaMTX session list unavailable, skipping eviction: %s", exc)
        return

    for session in webrtc.get("items", []):
        if session.get("path") == stream["annotated_path"] and session.get("state") == "publish":
            kick("webrtcsessions", session["id"])
    for session in rtsp.get("items", []):
        if session.get("path") == stream["stream_id"] and session.get("state") == "read":
            kick("rtspsessions", session["id"])


def running_instances(client: httpx.Client, base: str) -> dict[str, str]:
    """instance_id -> state, for everything DLSPS currently holds."""
    reply = client.get(f"{base}/pipelines/status", timeout=10.0)
    reply.raise_for_status()
    return {row["id"]: row["state"] for row in reply.json()}


def start(client: httpx.Client, base: str, stream: dict) -> str:
    reply = client.post(f"{base}/pipelines/{PIPELINE}",
                        json=_request_body(stream), timeout=30.0)
    reply.raise_for_status()
    return reply.json()


def wait_running(client: httpx.Client, base: str, instance_id: str,
                 timeout_seconds: float, poll_seconds: float) -> None:
    """Wait until asynchronous construction has consumed this request's parameters."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        if shutdown.is_set():
            raise TimeoutError("application is stopping")
        reply = client.get(f"{base}/pipelines/{instance_id}/status", timeout=10.0)
        reply.raise_for_status()
        state = reply.json()["state"]
        if state == "RUNNING":
            return
        if state in START_TERMINAL_STATES:
            raise PipelineStartFailed(state)
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"pipeline {instance_id} did not reach RUNNING within {timeout_seconds}s"
            )
        shutdown.wait(poll_seconds)


def stop(client: httpx.Client, base: str, instance_id: str) -> None:
    # DELETE /pipelines/{instance_id}, NOT the name/version form: the
    # /pipelines/{name}/{version}/{instance_id} handler exists in this image but
    # is hardcoded to return 400 NOT_IMPLEMENTED.
    reply = client.delete(f"{base}/pipelines/{instance_id}", timeout=10.0)
    reply.raise_for_status()
    _cleaned_terminal.add(instance_id)


def _write(conn: sqlite3.Connection, writer, operation):
    if writer is not None:
        return writer(operation)
    try:
        result = operation(conn)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def reconcile(conn: sqlite3.Connection, cfg: dict, client: httpx.Client,
              unhealthy: frozenset[str] = frozenset(),
              include_kept: bool = False, writer=None) -> set[str]:
    with lifecycle_lock():
        return _reconcile(conn, cfg, client, unhealthy, include_kept, writer)


def _reconcile(conn: sqlite3.Connection, cfg: dict, client: httpx.Client,
               unhealthy: frozenset[str], include_kept: bool, writer) -> set[str]:
    """Make DLSPS match the enabled camera list, then record what we own.

    Called at startup, so it also covers crash recovery: a Backend that died
    leaves its pipelines running, and the instance IDs it had persisted are the
    only way to tell those apart from someone else's.
    """
    if shutdown.is_set():
        return set()
    base = cfg["services"]["dlsps"]
    rows = conn.execute(
        "SELECT * FROM streams WHERE enabled = 1 ORDER BY sort_order, stream_id"
    ).fetchall()
    try:
        live = running_instances(client, base)
    except (httpx.HTTPError, ValueError) as exc:
        log.error("DLSPS unreachable, no pipeline started: %s", exc)
        if include_kept:
            # Startup may lose one aggregate status request after a crash.
            # Persisted ownership still earns one grace window; the next pass
            # will verify actual state before keeping or replacing it.
            now = time.monotonic()
            adopted = {
                row["stream_id"] for row in rows if row["dlsps_instance_id"]
            }
            for stream_id in adopted:
                _accepted_at.setdefault(stream_id, now)
            return adopted
        return set()

    now = time.monotonic()
    grace = cfg["feed_stale_seconds"]
    recently_accepted = {
        stream_id for stream_id, accepted_at in _accepted_at.items()
        if now - accepted_at <= grace
    }
    keep = {
        row["dlsps_instance_id"] for row in rows
        if live.get(row["dlsps_instance_id"]) in ACTIVE_STATES
        and (row["stream_id"] not in unhealthy
             or row["stream_id"] in recently_accepted)
    }
    if include_kept:
        for row in rows:
            if row["dlsps_instance_id"] in keep:
                _accepted_at.setdefault(row["stream_id"], now)

    # Sweep before starting, not after: each producer holds an exclusive lock on
    # its crop store, so a replacement pipeline started while the old one is
    # still alive dies immediately on that lock.
    #
    # One rule, applied to everything DLSPS still lists: if we are not keeping
    # it, we stop it. Replacing a camera's pipeline without deleting the one it
    # replaces is what let instances accumulate - the old instance is exactly
    # the thing holding the resources the replacement needs.
    _cleaned_terminal.intersection_update(live)
    for instance_id, state in live.items():
        if instance_id in keep or (
            state not in ACTIVE_STATES and instance_id in _cleaned_terminal
        ):
            # EOS/error ends GStreamer, but its publisher still needs DELETE.
            # DLSPS retains status records after cleanup; delete only once.
            continue
        log.warning("deleting pipeline %s (state %s) that no camera owns",
                    instance_id, state)
        try:
            stop(client, base, instance_id)
        except httpx.HTTPError as exc:
            log.error("could not delete %s: %s", instance_id, exc)

    started = {
        row["stream_id"] for row in rows
        if include_kept and row["dlsps_instance_id"] in keep
    }
    for row in rows:
        if shutdown.is_set():
            break
        stream = dict(row)
        if row["dlsps_instance_id"] in keep:
            # Debug, not info: this is the steady state and fires for every
            # camera on every supervisor pass, which buries real events.
            log.debug("camera %s already running as %s", row["stream_id"],
                      row["dlsps_instance_id"])
            forget_backoff(row["stream_id"])
            continue
        if time.monotonic() < _retry_after.get(row["stream_id"], 0.0):
            log.debug("camera %s: waiting out start backoff (%d consecutive failures)",
                      row["stream_id"], _failures.get(row["stream_id"], 0))
            continue
        if crop_store_busy(cfg["crop_dir"], row["stream_id"], cfg["takeover_seconds"]):
            # A live producer still owns the store. Waiting one supervisor pass
            # is free; starting now would just fail on the lock.
            log.info("camera %s: crop store still owned, deferring start",
                     row["stream_id"])
            continue
        # Do not create an endlessly reconnecting stock pipeline for an absent
        # MediaMTX source. Retry when the publisher actually arrives.
        try:
            reply = client.get(f"{cfg['services']['mediamtx']}/v3/paths/list", timeout=5.0)
            reply.raise_for_status()
            if not any(p.get("name") == row["stream_id"] and p.get("ready")
                       for p in reply.json().get("items", [])):
                continue
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("source readiness unavailable: %s", exc)
            continue
        evict_stale_media(client, cfg["services"]["mediamtx"], stream)
        if shutdown.is_set():
            break
        # Counted before the attempt, and cleared only on a later supervisor
        # pass. A POST only allocates an ID; DLSPS constructs asynchronously.
        fails = _failures[row["stream_id"]] = _failures.get(row["stream_id"], 0) + 1
        _retry_after[row["stream_id"]] = time.monotonic() + cfg["supervise_seconds"] * min(
            2 ** (fails - 1), MAX_BACKOFF_PASSES)
        try:
            instance_id = start(client, base, stream)
        except httpx.HTTPError as exc:
            # A camera that will not start is a visible degraded state, not a
            # reason to refuse to serve the ones that did.
            log.error("camera %s failed to start: %s", row["stream_id"], exc)
            _write(conn, writer, lambda db: db.execute(
                "UPDATE streams SET dlsps_instance_id = NULL WHERE stream_id = ?",
                (row["stream_id"],)))
            continue
        _write(conn, writer, lambda db: db.execute(
            "UPDATE streams SET dlsps_instance_id = ? WHERE stream_id = ?",
            (instance_id, row["stream_id"])))
        _accepted_at[row["stream_id"]] = time.monotonic()
        try:
            wait_running(
                client,
                base,
                instance_id,
                cfg.get("pipeline_start_timeout_seconds", 180.0),
                cfg.get("pipeline_start_poll_seconds", 2.0),
            )
        except PipelineStartFailed as exc:
            log.error("camera %s pipeline %s failed during construction: %s",
                      row["stream_id"], instance_id, exc)
            _write(conn, writer, lambda db: db.execute(
                "UPDATE streams SET dlsps_instance_id = NULL WHERE stream_id = ?",
                (row["stream_id"],),
            ))
            _accepted_at.pop(row["stream_id"], None)
            break
        except (httpx.HTTPError, ValueError, KeyError, TimeoutError) as exc:
            # Keep the instance ID on status/timeout failures: it may still own
            # resources, and the next reconcile must inspect or replace it
            # rather than launch another request beside it.
            log.error("camera %s pipeline %s start status unresolved: %s",
                      row["stream_id"], instance_id, exc)
            break
        log.info("camera %s started as %s", row["stream_id"], instance_id)
        started.add(row["stream_id"])
    return started


def stop_owned(conn: sqlite3.Connection, cfg: dict, client: httpx.Client,
               writer=None) -> None:
    with lifecycle_lock():
        _stop_owned(conn, cfg, client, writer)


def _stop_owned(conn: sqlite3.Connection, cfg: dict, client: httpx.Client,
                writer) -> None:
    base = cfg["services"]["dlsps"]
    rows = conn.execute("SELECT * FROM streams").fetchall()
    owned = {row["dlsps_instance_id"] for row in rows if row["dlsps_instance_id"]}
    try:
        live = running_instances(client, base)
        pending = {i for i, state in live.items()
                   if state in ACTIVE_STATES or i not in _cleaned_terminal}
    except (httpx.HTTPError, ValueError):
        log.exception("cannot enumerate DLSPS; attempting persisted pipeline IDs")
        pending = owned
    for instance_id in pending:
        try:
            stop(client, base, instance_id)
        except httpx.HTTPError:
            log.exception("could not stop pipeline %s", instance_id)
    # DELETE is asynchronous. Keep ownership if the service cannot confirm stop.
    deadline = time.monotonic() + 15.0
    while True:
        try:
            live = running_instances(client, base)
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError("DLSPS shutdown unverified; stop/restart DLSPS") from exc
        remaining = {i for i, state in live.items() if state in ACTIVE_STATES}
        if not remaining or time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    for row in rows:
        instance_id = row["dlsps_instance_id"]
        if instance_id not in remaining:
            _write(conn, writer, lambda db, sid=row["stream_id"]: db.execute(
                "UPDATE streams SET dlsps_instance_id = NULL WHERE stream_id = ?", (sid,)))
            evict_stale_media(client, cfg["services"]["mediamtx"], dict(row))
    _accepted_at.clear()
    if remaining:
        raise RuntimeError(f"DLSPS pipelines still active: {sorted(remaining)}; stop/restart DLSPS")
    log.info("DLSPS shutdown verified: no active pipelines")
