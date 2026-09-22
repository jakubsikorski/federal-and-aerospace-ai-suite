"""Backend application: services, camera history, telemetry and live UI state.

Phases 1-5 of docs/design/APPLICATION_DESIGN.md, Appendix A. Rule evaluation
and speech remain later phases.
"""
from __future__ import annotations

import asyncio
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

from . import history, pipelines, reset
from .assistant import (
    Assistant,
    InvalidInput,
    QueueFull,
    QueryBudgetExceeded,
    ServiceUnavailable,
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
    app.state.assistant = Assistant(cfg, app.state.services)
    app.state.mission = MissionService(
        cfg, app.state.services,
        generate=app.state.assistant.complete,
        on_corpus_change=app.state.assistant.reset_context,
    )
    app.state.assistant.mission = app.state.mission
    # Ingest before pipelines: a subscriber that starts second misses the first
    # observations of every camera it is supposed to be watching.
    app.state.ingest = Ingest(cfg, cfg["db_path"])
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
               has_image: bool = False):
        _known(request, stream_id)
        state = request.app.state
        with read_connection(request) as conn:
            return envelope(history.recent(conn, state.ingest, state.cfg, stream_id,
                                           limit=limit, since_utc=since_utc,
                                           class_labels=track_classes(classes),
                                           has_image=has_image))

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
            return envelope({"alerts": history.alerts(conn, limit=limit)})

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
        if not isinstance(body, dict) or set(body) - {"text", "plan", "selected_stream", "page_token", "timezone", "conversation_id", "mode"}:
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
        def perform(emit=None):
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
                def emit(event):
                    # Serialize now: the worker may subsequently append warnings.
                    loop.call_soon_threadsafe(messages.put_nowait, json.dumps(event) + "\n")
                def run():
                    try:
                        answer = perform(emit)
                        emit({"answer": answer, "done": True})
                    except (InvalidInput, QueueFull, QueryBudgetExceeded, ServiceUnavailable) as exc:
                        emit({"error": str(exc), "done": True})
                    except Exception:
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
        return envelope(request.app.state.assistant.reset_context(conversation_id))

    @app.get("/api/stream")
    async def live_stream(request: Request, classes: str | None = None,
                          has_image: bool = False):
        """Current-state SSE. Reconnect is a full resync; no replay ring exists."""
        class_labels = track_classes(classes)

        async def events():
            yield "retry: 2000\n\n"
            yield _event("mode", {
                "stream_id": None,
                "video": "live_only",
                "identity": "tracks_not_people",
            })
            with read_connection(request) as conn:
                while not pipelines.shutdown.is_set() and not await request.is_disconnected():
                    state = request.app.state
                    feeds = state.ingest.snapshot()
                    for stream in state.cfg["streams"]:
                        stream_id = stream["stream_id"]
                        feed = feeds.get(stream_id, {"ok": None, "reason": "unknown"})
                        yield _event("feed", {"stream_id": stream_id, "feed": feed})
                        yield _event("track", {
                            "stream_id": stream_id,
                            "current": history.current(
                                conn, state.ingest, state.cfg, stream_id),
                            "recent": history.recent(
                                conn, state.ingest, state.cfg, stream_id, limit=50,
                                class_labels=class_labels, has_image=has_image),
                        })
                    yield _event("alert", {
                        "stream_id": None,
                        "alerts": history.alerts(conn, limit=50),
                    })
                    yield _event("telemetry", {
                        "stream_id": None,
                        **state.telemetry.snapshot(),
                    })
                    await asyncio.sleep(1.0)

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
                            c.quality_score DESC LIMIT 10""", (track_id,)).fetchall()
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


def _event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
