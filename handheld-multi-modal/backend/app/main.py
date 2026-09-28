"""Backend application: services, camera history, telemetry and live UI state.

Phases 1-9 of docs/design/APPLICATION_DESIGN.md, Appendix A.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import alerts as alerting, history, pipelines, reset
from .speech import Arbiter, complete_answer_sentences, early_head
from .voice import MAX_AUDIO_BYTES, silent_wav, validate_audio, transcribe
from .assistant import (
    Assistant,
    InvalidInput,
    detector_labels,
    QueueFull,
    QueryBudgetExceeded,
    ServiceUnavailable,
    load_prompt_template,
    resolve_answer_marker,
)
from .adapters import Probe, Services
from .ingest import Ingest
from .mission import (
    MAX_UPLOAD_BYTES,
    MissionService,
    MissionUnavailable,
    MissionValidationError,
)
from .telemetry import Telemetry
from .placement import Placement
from .core import (
    connect,
    crop_store_busy,
    envelope,
    load_config,
    migrate,
    new_request_id,
    request_id_var,
    resolve_crop_path,
    setup_logging,
    sync_streams,
)

log = logging.getLogger("backend")


class EarlyAnswerSpeech:
    def __init__(self, arbiter: Arbiter, generation: int | None):
        self.arbiter = arbiter
        self.generation = generation
        self.buffer = ""
        self.first: dict | None = None
        self.sent = False

    def feed(self, text: object) -> None:
        if self.generation is None or not isinstance(text, str) or not text:
            return
        self.buffer += text
        sentences, self.buffer = complete_answer_sentences(self.buffer)
        for sentence in sentences:
            self._say(sentence)
        if self.first is None and self.buffer:
            # Do not wait for the whole first sentence: its first words go now.
            head, self.buffer = early_head(self.buffer)
            if head:
                self._say(head)

    def finish(self) -> None:
        if self.generation is not None:
            self._say(self.buffer.strip())
        self.buffer = ""

    def cancel(self) -> None:
        self.buffer = ""
        self.arbiter.cancel(group="answer", reason="cancelled")

    def _say(self, text: str) -> None:
        if not text:
            return
        item = self.arbiter.say_answer(text, self.generation)
        if item and self.first is None:
            self.first = item
        self.sent = self.sent or item is not None


@contextmanager
def read_connection(request: Request):
    """Give each concurrent API read its own SQLite connection."""
    conn = connect(request.app.state.cfg["db_path"])
    try:
        yield conn
    finally:
        conn.close()


def track_classes(value: str | None) -> tuple[str, ...]:
    labels = tuple(dict.fromkeys(
        label.strip() for label in (value or "").split(",") if label.strip()))
    if len(labels) > 20 or any(len(label) > 64 for label in labels):
        raise HTTPException(status_code=422, detail="invalid class filter")
    return labels


def local_checks(cfg: dict, conn: sqlite3.Connection) -> list[Probe]:
    """Prerequisites Backend can verify without any network call."""
    checks: list[Probe] = []

    crop_dir = Path(cfg["crop_dir"])
    if not crop_dir.is_dir():
        checks.append(Probe("crop_mount", False, f"{crop_dir} is not a directory", True))
    else:
        writable = os.access(crop_dir, os.W_OK)
        free_mb = shutil.disk_usage(crop_dir).free // (1024 * 1024)
        checks.append(
            Probe(
                "crop_mount",
                writable,
                f"readable, {'writable' if writable else 'READ-ONLY'}, {free_mb} MiB free",
                True,
                data={"free_mb": free_mb, "writable": writable},
            )
        )

    # H1: a producer that died inside __init__ leaves .lock held with no pipeline
    # running. Reported, never forcibly removed - breaking the single-writer
    # guarantee is worse than the stuck state it would paper over.
    for stream in cfg["streams"]:
        busy = crop_store_busy(cfg["crop_dir"], stream["stream_id"], cfg["takeover_seconds"])
        checks.append(
            Probe(
                f"crop_store:{stream['stream_id']}",
                True,
                "owned by a live producer" if busy else "available",
                False,
                data={"busy": busy},
            )
        )

    script = Path(cfg["crop_cleanup_script"])
    checks.append(
        Probe(
            "cleanup_script",
            script.is_file(),
            f"{script} {'present' if script.is_file() else 'MISSING - clear-history unavailable'}",
            False,
        )
    )

    try:
        count = conn.execute("SELECT count(*) AS n FROM streams").fetchone()["n"]
        checks.append(Probe("database", True, f"schema present, {count} camera(s) registered", True))
    except sqlite3.Error as exc:
        checks.append(Probe("database", False, str(exc), True))

    return checks


def voice_lock(app) -> asyncio.Lock:
    lock = getattr(app.state, "voice_lock", None)
    if lock is None:
        lock = app.state.voice_lock = asyncio.Lock()
    return lock


async def warm_whisper(app) -> None:
    """One silent transcription under the same lock as real ones.

    A real question arriving meanwhile waits for it (0.3-0.8 s warm) instead
    of running beside it on the device.
    """
    lock = voice_lock(app)
    if lock.locked():
        return
    async with lock:
        app.state.voice_warming = True
        try:
            await asyncio.to_thread(transcribe, app.state.services,
                                    app.state.cfg["services"]["audio_analyzer"], silent_wav())
        except Exception as exc:
            log.info("Whisper warm-up failed: %s", type(exc).__name__)
        finally:
            app.state.voice_warming = False


def warm_voice_path(app) -> None:
    """Talk was pressed: wake TTS and Whisper while the operator speaks.

    Both devices idle down between questions and the first request after idle
    measured 4-9 s slower. Skipped within the interval of the last warm-up or
    real use, so an active device never gets dummy work. TTS warm-up runs on
    the arbiter worker only while it is idle; Whisper's takes the voice lock.
    """
    cfg = app.state.cfg
    interval = float(cfg.get("voice_warm_interval_seconds", 20.0))
    now = time.monotonic()
    if now - getattr(app.state, "voice_warmed", float("-inf")) < interval:
        return
    app.state.voice_warmed = now
    if cfg.get("speech_ack_text", "Checking."):
        app.state.arbiter.warm(cfg.get("speech_ack_text", "Checking."), idle_seconds=interval)
    task = asyncio.get_running_loop().create_task(warm_whisper(app))
    app.state.voice_warm_task = task  # keep a reference until it finishes


def warm_chatqna_once(services: Services, timeout: float) -> None:
    def run():
        url = services.urls.get("chatqna")
        if not url:
            return
        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            try:
                health = services.client.get(url + "/health", timeout=min(2.0, timeout))
                if health.status_code == 200:
                    break
            except Exception:
                time.sleep(1.0)
        else:
            log.info("Chat Q&A warm-up skipped: service did not report healthy")
            return
        try:
            started = time.monotonic()
            response = services.client.post(
                url + "/chat", json={"input": "Say ready.", "stream": False}, timeout=timeout)
            response.raise_for_status()
            log.info("Chat Q&A warm-up completed in %d ms", int((time.monotonic() - started) * 1000))
        except Exception as exc:
            log.info("Chat Q&A warm-up failed without blocking startup: %s", exc)

    threading.Thread(target=run, name="chatqna-warmup", daemon=True).start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    setup_logging(cfg["log_level"])
    conn = connect(cfg["db_path"])
    migrate(conn)
    sync_streams(conn, cfg["streams"])
    app.state.cfg = cfg
    app.state.db = conn
    app.state.services = Services(cfg["services"], cfg["timeout_seconds"])
    app.state.telemetry = Telemetry(app.state.services, cfg["telemetry_host"])
    app.state.placement = Placement(app.state.services, cfg)
    # Resolve the two settings that must agree with deployed component config
    # before anything uses them. Both fail the process with a named cause
    # rather than surfacing later as an unstartable camera or a missing
    # response marker on the operator's first question.
    pipelines.configure(pipelines.resolve_pipeline_name(
        override=cfg.get("dlsps_pipeline_name")))
    log.info("DLSPS pipeline definition: %s", pipelines.PIPELINE)
    pipelines.validate_detector_profile(cfg["detector_metadata_path"],
        cfg["detector_profile_path"], name=pipelines.PIPELINE.split("/")[-1])
    cfg["chat_answer_marker"] = resolve_answer_marker(
        load_prompt_template(), cfg.get("chat_answer_marker"))
    app.state.assistant = Assistant(cfg, app.state.services)
    app.state.mission = MissionService(
        cfg, app.state.services,
        generate=app.state.assistant.complete,
        on_corpus_change=app.state.assistant.reset_context,
    )
    app.state.assistant.mission = app.state.mission
    warm_chatqna_once(app.state.services, float(cfg.get("assistant_chat_timeout_seconds", 60)))

    def synthesize(text: str) -> bytes:
        # Stock JSON mode: the WAV arrives as audio_base64 and is handed to the
        # browser as-is inside the speech state, so no clip endpoint is needed.
        body = {"input": text, "response_format": "json"}
        if cfg.get("tts_voice"):
            body["voice"] = cfg["tts_voice"]
        response = app.state.services.client.post(
            cfg["services"]["tts"].rstrip("/") + "/v1/audio/speech", json=body,
            timeout=float(cfg.get("tts_timeout_seconds", 30.0)))
        response.raise_for_status()
        audio = base64.b64decode(response.json()["audio_base64"], validate=True)
        if not audio.startswith(b"RIFF"):
            raise ValueError("TTS response is not WAV audio")
        return audio

    synthesize.cache_identity = json.dumps({
        "service": cfg["services"]["tts"].rstrip("/"),
        "voice": cfg.get("tts_voice") or "",
        "model": cfg.get("tts_model") or "",
    }, sort_keys=True)
    app.state.arbiter = Arbiter(synthesize, alert_floor=float(cfg.get("alert_speech_floor_seconds", 3.0)),
                                trace_path=cfg.get("arbiter_trace_path"))
    app.state.voice_warmed = float("-inf")
    if cfg.get("speech_ack_text", "Checking."):
        app.state.arbiter.warm(cfg.get("speech_ack_text", "Checking."))
    app.state.alerts = alerting.Alerts(
        cfg, cfg["db_path"], app.state.arbiter,
        lambda: detector_labels(cfg["detector_metadata_path"]))
    # Ingest before pipelines: a subscriber that starts second misses the first
    # observations of every camera it is supposed to be watching.
    app.state.ingest = Ingest(cfg, cfg["db_path"])
    app.state.ingest.predicted_confidence = pipelines.PREDICTED_CONFIDENCE
    app.state.ingest.alerts = app.state.alerts
    app.state.alerts.writer = app.state.ingest.write
    # Section 6.4: a previous process's open tracks are closed before anything
    # can query current state, so a restart cannot present them as live.
    app.state.ingest.recover()
    app.state.ingest.start()
    # One clear at a time, process-wide: two concurrent resets would race on
    # the same producer lock and leave one of them reporting a false failure.
    app.state.clear_lock = threading.Lock()
    # A clear interrupted by a crash is finished before the reconcile below,
    # so a half-cleared camera is never restarted with stale history (9.2).
    reset.resume_interrupted(conn, cfg, app.state.services.client, app.state.ingest)
    # Use the same lifecycle for startup and periodic camera recovery.
    stopping = pipelines.shutdown

    # Seeded from the startup reconcile, not left empty: the grace guard below
    # compares against this, and an unseeded camera counts as "started
    # infinitely long ago", so the very first supervisor pass would delete a
    # pipeline that is still prerolling. A cold NPU model compile takes far
    # longer than one supervisor interval.
    last_started: dict[str, float] = dict.fromkeys(
        pipelines.reconcile(
            conn, cfg, app.state.services.client, include_kept=True,
            writer=app.state.ingest.write,
        ),
        time.monotonic(),
    )

    def supervise():
        grace = cfg["feed_stale_seconds"]
        while not stopping.wait(cfg["supervise_seconds"]):
            try:
                now = time.monotonic()
                # A camera is only replaced once it has been silent past the
                # grace period AND its own restart has had that long to produce
                # something, otherwise every pass would restart every camera.
                unhealthy = frozenset(
                    s for s in app.state.ingest.stale(grace)
                    if now - last_started.get(s, float("-inf")) > grace
                )
                if unhealthy:
                    log.warning("replacing silent camera pipelines: %s", sorted(unhealthy))
                for stream_id in pipelines.reconcile(
                    conn, cfg, app.state.services.client, unhealthy,
                    writer=app.state.ingest.write,
                ):
                    last_started[stream_id] = time.monotonic()
            except Exception:
                log.exception("supervisor pass failed")

    supervisor = threading.Thread(target=supervise, name="pipeline-supervisor", daemon=True)
    supervisor.start()
    app.state.telemetry.start()
    app.state.arbiter.start()
    app.state.alerts.start()
    # Stock Chat Q&A keeps the vector store only in memory. Reconciliation is
    # deliberately asynchronous so its degraded state remains visible to the UI.
    app.state.mission.start()
    log.info("backend started with %d camera(s)", len(cfg["streams"]))
    try:
        yield
    finally:
        stopping.set()
        supervisor.join()
        try:
            pipelines.stop_owned(
                conn, cfg, app.state.services.client, writer=app.state.ingest.write)
        finally:
            app.state.alerts.close()
            app.state.arbiter.close()
            app.state.ingest.close()
            app.state.telemetry.close()
            app.state.services.close()
            conn.close()


def create_app() -> FastAPI:
    app = FastAPI(title="handheld-multimodal backend", lifespan=lifespan)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        token = request_id_var.set(request.headers.get("x-request-id") or new_request_id())
        try:
            response = await call_next(request)
            response.headers["x-request-id"] = request_id_var.get()
            return response
        finally:
            request_id_var.reset(token)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse(
            status_code=500,
            content=envelope(error={"code": "internal_error", "message": type(exc).__name__}),
        )

    @app.get("/api/health")
    def health(request: Request):
        cfg = request.app.state.cfg
        with read_connection(request) as conn:
            checks = local_checks(cfg, conn) + request.app.state.services.probe_all()
        blocking = [c for c in checks if c.blocking and not c.ok]
        degraded = [c for c in checks if not c.blocking and not c.ok]
        status = "blocked" if blocking else ("degraded" if degraded else "ready")
        return JSONResponse(
            status_code=200,
            content=envelope(
                {
                    "status": status,
                    "blocking_failures": [c.name for c in blocking],
                    "degraded": [c.name for c in degraded],
                    "checks": [c.as_dict() for c in checks],
                }
            ),
        )

    @app.get("/api/models")
    def models(request: Request):
        with read_connection(request) as conn:
            owned = {row["stream_id"]: row["dlsps_instance_id"] for row in conn.execute(
                "SELECT stream_id, dlsps_instance_id FROM streams WHERE enabled = 1")}
        return envelope(request.app.state.placement.snapshot(
            owned, request.app.state.ingest.snapshot()))

    @app.get("/api/streams")
    def streams(request: Request):
        with read_connection(request) as conn:
            rows = conn.execute(
                "SELECT * FROM streams WHERE enabled = 1 ORDER BY sort_order, stream_id"
            ).fetchall()
        health = request.app.state.ingest.snapshot()
        return envelope(
            [
                {
                    "stream_id": row["stream_id"],
                    "display_name": row["display_name"],
                    "sort_order": row["sort_order"],
                    # Relative on purpose (D15): the UI resolves it against its own
                    # origin, so no host or address is ever baked into the bundle.
                    "whep_path": f"/{row['annotated_path']}/whep",
                    "clear_state": row["clear_state"],
                    "feed": health.get(row["stream_id"], {"ok": None, "reason": "unknown"}),
                    "tracking_session_id": row["tracking_session_id"],
                    "history_generation": row["history_generation"],
                }
                for row in rows
            ]
        )

    def _known(request: Request, stream_id: str) -> None:
        if stream_id not in {s["stream_id"] for s in request.app.state.cfg["streams"]}:
            raise HTTPException(status_code=404, detail="unknown camera")

    @app.get("/api/streams/{stream_id}/current")
    def current(request: Request, stream_id: str):
        """Section 6.5. Never answers "nothing there" on a feed that is not advancing."""
        _known(request, stream_id)
        state = request.app.state
        with read_connection(request) as conn:
            return envelope(history.current(conn, state.ingest, state.cfg, stream_id))

    @app.get("/api/streams/{stream_id}/tracks")
    def tracks(request: Request, stream_id: str, limit: int = 50,
               since_utc: str | None = None, classes: str | None = None,
               has_image: bool = False, prioritize: bool = True):
        _known(request, stream_id)
        state = request.app.state
        with read_connection(request) as conn:
            return envelope(history.recent(conn, state.ingest, state.cfg, stream_id,
                                           limit=limit, since_utc=since_utc,
                                           class_labels=track_classes(classes),
                                           has_image=has_image, prioritize=prioritize))

    @app.get("/api/tracks/{track_id}/candidates")
    def track_candidates(request: Request, track_id: int):
        with read_connection(request) as conn:
            if not conn.execute(
                    "SELECT 1 FROM tracks WHERE track_id = ?", (track_id,)).fetchone():
                raise HTTPException(status_code=404, detail="unknown track")
            return envelope({
                "track_id": track_id,
                "candidates": history.candidates(conn, track_id),
            })

    @app.get("/api/ingest/stats")
    def ingest_stats(request: Request):
        """Drop counters by reason. Silent drops are the ones that mislead."""
        return envelope(request.app.state.ingest.stats())

    @app.get("/api/metrics")
    def metrics(request: Request):
        return envelope(request.app.state.telemetry.snapshot())

    @app.get("/api/alerts")
    def alerts(request: Request, limit: int = 50):
        with read_connection(request) as conn:
            return envelope({"alerts": _alerts(request.app.state, conn, limit),
                             "incidents": _incidents(request.app.state, conn),
                             "incident_counts": history.incident_counts(conn),
                             **request.app.state.alerts.status()})

    @app.post("/api/alerts/{match_id}/retry")
    def retry_alert(request: Request, match_id: str):
        with read_connection(request) as conn:
            if not request.app.state.alerts.retry(conn, match_id):
                return JSONResponse(status_code=409, content=envelope(error={
                    "code": "not_retryable", "message": "Only an undelivered alert can be retried.",
                }))
        return envelope({"match_id": match_id, "dispatch_state": "pending"})

    @app.get("/api/incidents/{incident_id}/tracks")
    def incident_tracks(request: Request, incident_id: str):
        with read_connection(request) as conn:
            tracks = history.incident_tracks(conn, incident_id)
        if tracks is None:
            return JSONResponse(status_code=404, content=envelope(error={
                "code": "not_found", "message": "Unknown alert."}))
        return envelope({"incident_id": incident_id, "tracks": tracks})

    @app.post("/api/incidents/{incident_id}/speak")
    def incident_speak(request: Request, incident_id: str):
        with read_connection(request) as conn:
            item = request.app.state.alerts.speak_test(conn, incident_id)
        if item is None:
            return JSONResponse(status_code=404, content=envelope(error={
                "code": "not_found", "message": "Unknown alert or speech is unavailable."}))
        return envelope({"incident_id": incident_id, "speech_id": item["id"], "text": item["text"]})

    @app.delete("/api/incidents/{incident_id}")
    def incident_remove(request: Request, incident_id: str):
        if not request.app.state.alerts.remove(incident_id):
            return JSONResponse(status_code=404, content=envelope(error={
                "code": "not_found", "message": "Unknown alert."}))
        return envelope({"incident_id": incident_id, "removed": True})

    @app.post("/api/incidents/{incident_id}/{action}")
    async def incident_action(request: Request, incident_id: str, action: str):
        try:
            body = await request.json() if await request.body() else {}
        except json.JSONDecodeError:
            body = None
        if not isinstance(body, dict) or set(body) - {"minutes"}:
            return JSONResponse(status_code=422, content=envelope(error={
                "code": "invalid_action", "message": "Provide an action object with optional minutes."}))
        def apply():
            try:
                changed = request.app.state.alerts.act(incident_id, action, body.get("minutes"))
            except alerting.RuleError as exc:
                return JSONResponse(status_code=422, content=envelope(error={
                    "code": "invalid_action", "message": str(exc)}))
            if not changed:
                return JSONResponse(status_code=409, content=envelope(error={
                    "code": "not_active", "message": "This alert was already handled or closed."}))
            with read_connection(request) as conn:
                incident = next((i for i in _incidents(request.app.state, conn, 100)
                                 if i["incident_id"] == incident_id), None)
            return envelope(incident)
        return await asyncio.to_thread(apply)

    # -- rules (Phase 8) -------------------------------------------------------
    def _rule_error(exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=422, content=envelope(error={
            "code": "invalid_rule", "message": str(exc)}))

    @app.get("/api/rules")
    def rules(request: Request):
        state = request.app.state
        try:
            classes = list(detector_labels(state.cfg["detector_metadata_path"]))
        except ServiceUnavailable:
            classes = []
        with read_connection(request) as conn:
            return envelope({"rules": alerting.list_rules(conn), "classes": classes,
                             "streams": state.alerts.enabled_streams})

    @app.post("/api/rules", status_code=201)
    async def create_rule(request: Request):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = None
        def create():
            with read_connection(request) as conn:
                try:
                    return envelope(request.app.state.alerts.create(conn, body))
                except (alerting.RuleError, ServiceUnavailable) as exc:
                    return _rule_error(exc)
        return await asyncio.to_thread(create)

    @app.patch("/api/rules/{rule_id}")
    async def update_rule(request: Request, rule_id: str):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = None
        def update():
            with read_connection(request) as conn:
                try:
                    rule = request.app.state.alerts.update(conn, rule_id, body)
                except (alerting.RuleError, ServiceUnavailable) as exc:
                    return _rule_error(exc)
            if rule is None:
                raise HTTPException(status_code=404, detail="unknown rule")
            return envelope(rule)
        return await asyncio.to_thread(update)

    @app.delete("/api/rules/{rule_id}")
    def delete_rule(request: Request, rule_id: str, confirm: str = ""):
        if confirm != rule_id:
            return _rule_error(ValueError(f"confirm must equal {rule_id!r}; rule was not deleted"))
        with read_connection(request) as conn:
            if not request.app.state.alerts.delete(conn, rule_id):
                raise HTTPException(status_code=404, detail="unknown rule")
        return envelope({"rule_id": rule_id, "deleted": True})

    # -- speech (Phase 8 audio arbiter) --------------------------------------
    @app.get("/api/speech")
    def speech(request: Request, trace: int = 0):
        arbiter = request.app.state.arbiter
        data = arbiter.snapshot()
        if trace:
            data["trace"] = list(arbiter.trace)[-max(1, min(trace, 500)):]
        return envelope(data)

    @app.post("/api/speech/{speech_id}/playback")
    async def speech_playback(request: Request, speech_id: str):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = None
        event = body.get("event") if isinstance(body, dict) else None
        try:
            current = request.app.state.arbiter.ack(speech_id, event)
        except ValueError as exc:
            return JSONResponse(status_code=422, content=envelope(error={
                "code": "invalid_request", "message": str(exc)}))
        # 200 either way: a report for an utterance that already ended is stale, not an error.
        return envelope({"speech_id": speech_id, "event": event, "current": current})

    @app.post("/api/speech/talk")
    async def speech_talk(request: Request):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = None
        if (not isinstance(body, dict) or "held" not in body or not set(body) <= {"held", "answer_pending"}
                or not all(isinstance(v, bool) for v in body.values())
                or (body.get("answer_pending") and body["held"])):
            return JSONResponse(status_code=422, content=envelope(error={
                "code": "invalid_request",
                "message": "provide held as true or false; answer_pending only when releasing"}))
        request.app.state.arbiter.set_talk(body["held"], answer_pending=body.get("answer_pending", False))
        if body["held"]:
            warm_voice_path(request.app)
        return envelope(request.app.state.arbiter.snapshot())

    @app.post("/api/speech/stop")
    async def stop_speech(request: Request):
        request.app.state.arbiter.stop_speaking()
        return envelope(request.app.state.arbiter.snapshot())

    @app.post("/api/voice/transcribe")
    async def voice_transcribe(request: Request, speak: bool = False):
        # The browser released Talk with answer_pending, so automatic speech is
        # already held for this generation. Every exit without a transcript
        # releases it; the following /api/ask re-arms or ends it otherwise.
        arbiter = getattr(request.app.state, "arbiter", None)
        generation = arbiter.answer_generation if arbiter else None
        text = ""
        try:
            # Read before multipart parsing to enforce the limit even without a
            # Content-Length header. Browser sends WAV; upstream receives multipart.
            data = bytearray()
            try:
                async with asyncio.timeout(20):
                    async for chunk in request.stream():
                        if len(data) + len(chunk) > MAX_AUDIO_BYTES:
                            raise HTTPException(413, "Recording is too large.")
                        data.extend(chunk)
            except TimeoutError as exc:
                raise HTTPException(408, "Recording upload timed out.") from exc
            try:
                validate_audio(bytes(data))
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            lock = voice_lock(request.app)
            if lock.locked() and not getattr(request.app.state, "voice_warming", False):
                raise HTTPException(429, "Transcription is busy. Please try again.")
            if arbiter:
                arbiter.begin_answer(generation, seconds=10.0)
            async with lock:
                try:
                    text = await asyncio.to_thread(transcribe, request.app.state.services,
                        request.app.state.cfg["services"]["audio_analyzer"], bytes(data))
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc
                except Exception as exc:
                    log.warning("Transcription failed: %s", type(exc).__name__)
                    raise HTTPException(503, "Transcription unavailable. Type your question or try again.") from exc
                finally:
                    # Whisper was just used; Talk need not warm it again.
                    request.app.state.voice_warmed = time.monotonic()
        finally:
            if arbiter and not text.strip():
                arbiter.end_answer(generation)
        if arbiter and speak and text.strip() and not await request.is_disconnected():
            arbiter.acknowledge(request.app.state.cfg.get("speech_ack_text", "Checking."), generation)
        return envelope({"text": text})

    @app.post("/api/voice/cancel")
    async def voice_cancel(request: Request):
        """The operator abandoned a voice question: its cue and answer never speak."""
        request.app.state.arbiter.cancel_answer()
        return envelope(request.app.state.arbiter.snapshot())

    @app.get("/api/history/options")
    def history_options(request: Request):
        return envelope(request.app.state.assistant.options())

    # -- mission knowledge (Phase 6) -----------------------------------------
    @app.get("/api/mission/documents")
    def mission_documents(request: Request):
        return envelope(request.app.state.mission.status())

    @app.post("/api/mission/documents", status_code=201)
    async def upload_mission_document(request: Request,
                                      files: list[UploadFile] = File(...)):
        if len(files) != 1:
            return JSONResponse(status_code=422, content=envelope(error={
                "code": "invalid_upload",
                "message": "Upload exactly one PDF or DOCX mission document at a time.",
            }))
        document = files[0]
        try:
            # Read one extra byte so the service never retains an oversized upload.
            content = await document.read(MAX_UPLOAD_BYTES + 1)
            result = await asyncio.to_thread(
                request.app.state.mission.upload, document.filename, content)
        except MissionValidationError as exc:
            return JSONResponse(status_code=422, content=envelope(error={
                "code": "invalid_upload", "message": str(exc),
            }))
        except MissionUnavailable as exc:
            return JSONResponse(status_code=503, content=envelope(error={
                "code": "mission_unavailable", "message": str(exc),
            }))
        finally:
            await document.close()
        return envelope(result)

    @app.delete("/api/mission/documents")
    async def delete_mission_document(request: Request, document: str | None = None,
                                      confirm: str = "", delete_all: bool = False):
        try:
            if delete_all:
                result = await asyncio.to_thread(request.app.state.mission.clear, confirm)
            elif document is not None:
                result = await asyncio.to_thread(
                    request.app.state.mission.delete, document, confirm)
            else:
                raise MissionValidationError("Specify one document or delete_all=true.")
        except MissionValidationError as exc:
            return JSONResponse(status_code=422, content=envelope(error={
                "code": "invalid_mission_mutation", "message": str(exc),
            }))
        except MissionUnavailable as exc:
            return JSONResponse(status_code=503, content=envelope(error={
                "code": "mission_unavailable", "message": str(exc),
            }))
        return envelope(result)

    @app.post("/api/mission/reingest")
    def reingest_mission(request: Request, confirm: str = ""):
        if confirm != "REINGEST MISSION":
            return JSONResponse(status_code=422, content=envelope(error={
                "code": "invalid_mission_mutation",
                "message": "Confirmation must equal 'REINGEST MISSION'.",
            }))
        request.app.state.mission.start(retry_brief=True)
        return envelope(request.app.state.mission.status())

    @app.get("/api/mission/brief")
    def mission_brief(request: Request):
        return envelope(request.app.state.mission.brief())

    @app.post("/api/mission/ask")
    async def ask_mission(request: Request):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = None
        if not isinstance(body, dict) or set(body) != {"text"}:
            return JSONResponse(status_code=422, content=envelope(error={
                "code": "invalid_request", "message": "provide one mission question as text",
            }))
        try:
            answer = await asyncio.to_thread(request.app.state.mission.answer, body["text"])
        except MissionValidationError as exc:
            return JSONResponse(status_code=422, content=envelope(error={
                "code": "invalid_request", "message": str(exc),
            }))
        except MissionUnavailable as exc:
            return JSONResponse(status_code=503, content=envelope(error={
                "code": "mission_unavailable", "message": str(exc),
            }))
        return envelope(answer)

    @app.post("/api/ask")
    async def ask(request: Request):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = None
        if not isinstance(body, dict) or set(body) - {"text", "plan", "selected_stream", "page_token", "timezone", "conversation_id", "mode", "speak"}:
            return JSONResponse(status_code=422, content=envelope(error={
                "code": "invalid_request", "message": "invalid assistant request",
            }))
        if sum(key in body for key in ("text", "plan", "page_token")) != 1:
            return JSONResponse(status_code=422, content=envelope(error={
                "code": "invalid_request",
                "message": "provide exactly one of text, plan or page_token",
            }))
        if "page_token" in body and ({"selected_stream", "timezone"} & body.keys()):
            return JSONResponse(status_code=422, content=envelope(error={
                "code": "invalid_request",
                "message": "selected_stream is not valid with page_token",
            }))
        conversation_id = body.get("conversation_id", uuid.uuid4().hex)
        arbiter = getattr(request.app.state, "arbiter", None)
        speech_generation = arbiter.answer_generation if body.get("speak") is True else None
        if arbiter is not None and speech_generation is None:
            arbiter.end_answer()
        elif speech_generation is not None:
            arbiter.begin_answer(speech_generation)
        def perform(emit=None, early_speech: EarlyAnswerSpeech | None = None):
            try:
                return spoken(answer_for(emit), early_speech)
            finally:
                # Queued answer chunks keep outranking alerts after this point.
                if speech_generation is not None:
                    arbiter.end_answer(speech_generation)
        def spoken(answer, early_speech):
            text = answer.get("spoken_text") or answer.get("answer_text")
            if body.get("speak") is True and isinstance(text, str) and text.strip():
                if early_speech is not None:
                    early_speech.finish()
                    if early_speech.first is not None:
                        answer["speech_id"] = early_speech.first["id"]
                if early_speech is None or not early_speech.sent:
                    item = request.app.state.arbiter.say_answer(text, speech_generation)
                    if item:
                        answer["speech_id"] = item["id"]
            return answer
        def answer_for(emit=None):
            assistant = request.app.state.assistant
            with assistant.conversation(conversation_id):
                if "page_token" in body:
                    return assistant.page(body["page_token"])
                if "plan" in body:
                    return assistant.remember_result(assistant.search(
                        body["plan"], body.get("selected_stream"), emit=emit))
                return assistant.ask(body["text"], body.get("selected_stream"),
                    **({"emit":emit} if emit else {}),
                    **({"timezone":body["timezone"]} if "timezone" in body else {}),
                    **({"mode":body["mode"]} if "mode" in body else {}))
        if "application/x-ndjson" in request.headers.get("accept", ""):
            async def updates():
                loop = asyncio.get_running_loop()
                messages = asyncio.Queue()
                early_speech = EarlyAnswerSpeech(
                    request.app.state.arbiter, speech_generation) if body.get("speak") is True else None
                def emit(event):
                    # Serialize now: the worker may subsequently append warnings.
                    loop.call_soon_threadsafe(messages.put_nowait, json.dumps(event) + "\n")
                    if early_speech is not None and isinstance(event, dict):
                        early_speech.feed(event.get("delta"))
                def run():
                    try:
                        answer = perform(emit, early_speech)
                        emit({"answer": answer, "done": True})
                    except (InvalidInput, QueueFull, QueryBudgetExceeded, ServiceUnavailable) as exc:
                        if early_speech is not None:
                            early_speech.cancel()
                        emit({"error": str(exc), "done": True})
                    except Exception:
                        if early_speech is not None:
                            early_speech.cancel()
                        log.exception("history request failed")
                        emit({"error": "History query failed. Please try again.", "done": True})
                    finally:
                        loop.call_soon_threadsafe(messages.put_nowait, None)
                worker = asyncio.create_task(asyncio.to_thread(run))
                try:
                    while True:
                        event = await messages.get()
                        if event is None:
                            break
                        yield event
                finally:
                    # Stock inference cannot be canceled. It keeps its admission
                    # slot until completion, even if the browser disconnects.
                    await asyncio.shield(worker)
            return StreamingResponse(updates(), media_type="application/x-ndjson",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        try:
            answer = await asyncio.to_thread(perform)
        except InvalidInput as exc:
            return JSONResponse(status_code=422, content=envelope(error={
                "code": "invalid_request", "message": str(exc),
            }))
        except QueueFull as exc:
            return JSONResponse(status_code=429, content=envelope(error={
                "code": "assistant_busy", "message": str(exc),
            }))
        except QueryBudgetExceeded as exc:
            return JSONResponse(status_code=503, content=envelope(error={
                "code": "query_budget_exceeded", "message": str(exc),
            }))
        except ServiceUnavailable as exc:
            return JSONResponse(status_code=503, content=envelope(error={
                "code": "language_service_unavailable", "message": str(exc),
            }))
        return envelope(answer)

    @app.post("/api/assistant/reset")
    def reset_assistant_context(request: Request, conversation_id: str):
        if not re.fullmatch(r"[a-f0-9-]{32,36}", conversation_id):
            raise HTTPException(status_code=422, detail="Invalid conversation_id")
        request.app.state.arbiter.stop_speaking()
        return envelope(request.app.state.assistant.reset_context(conversation_id))

    @app.get("/api/stream")
    async def live_stream(request: Request, classes: str | None = None,
                          has_image: bool = False, prioritize: bool = True):
        """Current-state SSE. Reconnect is a full resync; no replay ring exists."""
        class_labels = track_classes(classes)

        async def events():
            yield "retry: 2000\n\n"
            yield _event("mode", {
                "stream_id": None,
                "video": "live_only",
                "identity": "tracks_not_people",
            })
            last_speech = None
            while not pipelines.shutdown.is_set() and not await request.is_disconnected():
                state = request.app.state
                # SQLite, ingest locks and JSON encoding must not stall other
                # requests or playback acknowledgements on the event loop.
                batch = await asyncio.to_thread(
                    _live_events, request, class_labels, has_image, prioritize)
                for event in batch:
                    yield event
                # Speech still refreshes at 5 Hz, independently of the 1 Hz data.
                for _ in range(5):
                    speech = state.arbiter.snapshot()
                    if speech["version"] != last_speech:
                        last_speech = speech["version"]
                        yield _event("speech", {"stream_id": None, **speech})
                    await asyncio.sleep(0.2)

        return StreamingResponse(events(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })

    # -- images (section 8.3) ----------------------------------------------
    def _serve(request: Request, conn: sqlite3.Connection, row) -> Response:
        cfg = request.app.state.cfg
        if row is None:
            raise HTTPException(status_code=404, detail="unknown candidate")
        if row["image_state"] != "present":
            # Typed body, not a bare 404: the UI must render an explicit state
            # rather than a broken image icon.
            return JSONResponse(status_code=404,
                                content=envelope({"image_state": row["image_state"]}))
        generation = conn.execute(
            "SELECT history_generation FROM streams WHERE stream_id = ?",
            (row["stream_id"],)).fetchone()
        if generation and (generation["history_generation"] or "") != row["history_generation"]:
            # Evidence from a cleared generation is not part of the current
            # application context and is never served, even if the file survives.
            return JSONResponse(status_code=404, content=envelope({"image_state": "superseded"}))
        try:
            path = resolve_crop_path(cfg["crop_dir"], row["stream_id"], row["storage_key"])
        except ValueError:
            log.warning("rejected storage key for candidate %s", row["candidate_id"])
            raise HTTPException(status_code=400, detail="invalid storage key")
        if not path.is_file():
            # The producer owns expiry; a vanished file is a normal state, so it
            # is recorded rather than retried or substituted.
            conn.execute("UPDATE candidates SET image_state = 'missing' WHERE candidate_id = ?",
                         (row["candidate_id"],))
            conn.commit()
            return JSONResponse(status_code=404, content=envelope({"image_state": "missing"}))
        etag = f'"{row["candidate_id"]}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304)
        return FileResponse(path, media_type="image/jpeg", headers={
            "Cache-Control": "private, max-age=60", "ETag": etag})

    @app.get("/api/candidates/{candidate_id}/image")
    def candidate_image(request: Request, candidate_id: str):
        with read_connection(request) as conn:
            row = conn.execute(
                "SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
            return _serve(request, conn, row)

    @app.get("/api/tracks/{track_id}/snip")
    def track_snip(request: Request, track_id: int):
        """The track's selected candidate, else the best one still on disk.

        Crops rotate out from under the index (the producer caps its store), so
        the best-scoring row is routinely a file that no longer exists. Walking
        down to the next surviving candidate turns that into a served image
        instead of a 404 the operator has to retry, and each miss is recorded on
        the way past so the next request skips it immediately.
        """
        with read_connection(request) as conn:
            rows = conn.execute(
                """SELECT c.* FROM candidates c
                   JOIN tracks t ON t.track_id = c.track_id
                   WHERE c.track_id = ? AND c.image_state = 'present'
                   ORDER BY (c.candidate_id = coalesce(t.selected_candidate_id, '')) DESC,
                            c.quality_score DESC, c.candidate_id DESC LIMIT 10""", (track_id,)).fetchall()
            for row in rows:
                response = _serve(request, conn, row)
                if response.status_code != 404:
                    return response
        return JSONResponse(status_code=404, content=envelope({"image_state": "none"}))

    @app.post("/api/streams/{stream_id}/clear")
    def clear_history(request: Request, stream_id: str, confirm: str = ""):
        """Section 9.2. Destructive and scoped to one camera.

        `confirm` must repeat the camera id, so a mis-aimed request clears
        nothing rather than the wrong camera's evidence.
        """
        _known(request, stream_id)
        if confirm != stream_id:
            raise HTTPException(
                status_code=400,
                detail=f"confirm must equal {stream_id!r}; history was not cleared")
        state = request.app.state
        if not state.clear_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="another clear is already running")
        try:
            result = reset.clear_camera(state.db, state.cfg, state.services.client,
                                        state.ingest, stream_id)
        except reset.ResetError as exc:
            # Never reports success it did not achieve.
            return JSONResponse(status_code=503, content=envelope(
                {"stream_id": stream_id, "cleared": False},
                error={"code": "clear_incomplete", "message": str(exc)}))
        finally:
            state.clear_lock.release()
        state.assistant.reset_context()
        return envelope(result)

    static = Path(__file__).parent / "static"
    if static.is_dir():
        app.mount("/", StaticFiles(directory=static, html=True), name="ui")

    return app


app = create_app()


def _live_events(request: Request, class_labels: tuple[str, ...],
                 has_image: bool, prioritize: bool) -> list[str]:
    """Build one SSE batch; the connection is owned and closed by this worker."""
    state = request.app.state
    feeds = state.ingest.snapshot()
    batch = []
    with read_connection(request) as conn:
        for stream in state.cfg["streams"]:
            if not stream.get("enabled", True):
                continue
            stream_id = stream["stream_id"]
            batch.append(_event("feed", {"stream_id": stream_id,
                "feed": feeds.get(stream_id, {"ok": None, "reason": "unknown"})}))
            batch.append(_event("track", {
                "stream_id": stream_id,
                "current": history.current(conn, state.ingest, state.cfg, stream_id),
                "recent": history.recent(conn, state.ingest, state.cfg, stream_id,
                    limit=50, class_labels=class_labels, has_image=has_image,
                    prioritize=prioritize),
            }))
        batch.append(_event("alert", {
            "stream_id": None, "alerts": _alerts(state, conn, 50),
            "incidents": _incidents(state, conn),
            "incident_counts": history.incident_counts(conn),
        }))
    batch.append(_event("telemetry", {"stream_id": None, **state.telemetry.snapshot()}))
    return batch


def _alerts(state, conn: sqlite3.Connection, limit: int) -> list[dict]:
    """SQLite matches plus the process-local speech and agent-observed facts."""
    rows = history.alerts(conn, limit=limit)
    for row in rows:
        row["speech"] = state.arbiter.outcome(row["match_id"])
        row["agent_observed"] = row["match_id"] in state.alerts.confirmed
        row["undelivered"] = (row["dispatch_state"] == "failed"
                              and row["attempts"] >= alerting.MAX_ATTEMPTS)
    return rows


def _incidents(state, conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = history.incidents(conn, limit=limit)
    for row in rows:
        row["speech"] = state.arbiter.outcome(row["head_match_id"])
        row["undelivered"] = (row["dispatch_state"] == "failed"
                              and (row["attempts"] or 0) >= alerting.MAX_ATTEMPTS)
    return rows


def _event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
