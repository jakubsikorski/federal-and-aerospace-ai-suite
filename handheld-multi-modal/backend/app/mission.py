"""Mission-document lifecycle on top of the stock Chat Q&A document APIs."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Callable, Iterator

import httpx
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .core import connect

log = logging.getLogger("backend.mission")

OPERATOR_GUIDE = Path(__file__).with_name("operator-guide.md").read_text(encoding="utf-8")

BRIEF_FIELDS = (
    "objective", "times", "locations", "people_units", "restrictions",
    "resources", "dependencies", "risks",
)
# Phase 6 anchored this by pasting the whole operator guide into the brief
# prompt. Live generation showed the model answering *about* the guide -- it
# described the Pause control instead of emitting a brief -- so the exclusion
# is stated in one line and the guide text is kept out of the prompt.
BRIEF_EXCLUSION = (
    "Never fill mission fields from the application guide, its controls, camera\n"
    "configuration, or assumed live observations. Do not turn application\n"
    "capabilities into mission resources."
)
NOT_SPECIFIED = "Not specified"
BRIEF_QUESTIONS = {
    "objective": "What is the mission objective?",
    "times": "What times, dates or schedule does the mission specify?",
    "locations": "What locations or areas of operation does the mission specify?",
    "people_units": "Which people or units are assigned to the mission?",
    "restrictions": "What restrictions, prohibitions or rules of engagement apply?",
    "resources": "What equipment or resources are provided for the mission?",
    "dependencies": "What does the mission depend on or coordinate with?",
    "risks": "What risks or hazards does the mission identify?",
}
MAX_FILENAME = 128
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_PDF_PAGES = 200
MAX_PDF_OBJECTS = 100_000
MAX_DOCX_ENTRIES = 2_000
MAX_DOCX_ENTRY_BYTES = 20 * 1024 * 1024
MAX_DOCX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_DOCX_COMPRESSION_RATIO = 1_000
_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_PDF_OBJECT = re.compile(rb"\bendobj\b")


class MissionError(Exception):
    """A typed mission lifecycle error safe to return to an operator."""


class MissionUnavailable(MissionError):
    pass


class MissionValidationError(MissionError):
    pass


def brief_failure_message(exc: BaseException) -> str:
    """Name a brief-generation failure in words an operator can act on.

    MissionError messages are operator-safe by contract and pass through. The
    leak was everything else: a raw decoder string or a bare exception class
    name, which told the operator nothing and read as an unhandled crash.
    """
    if isinstance(exc, MissionError):
        return str(exc)
    text = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timed out" in text:
        return "Brief generation timed out."
    if any(word in text for word in ("connection", "unavailable", "request failed", "invalid json")):
        return "The language service is not responding."
    if isinstance(exc, (ValueError, TypeError)):
        return "The language model returned an unusable answer."
    return "Brief generation failed unexpectedly."


def _utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_filename(value: object) -> str:
    if not isinstance(value, str) or not _FILENAME.fullmatch(value):
        raise MissionValidationError(
            "Document names must be 1-128 characters using letters, numbers, spaces, dot, dash or underscore.")
    suffix = Path(value).suffix.lower()
    if suffix not in {".pdf", ".docx"}:
        raise MissionValidationError("Only PDF and DOCX mission documents are supported.")
    return value


def validate_upload(filename: object, content: object) -> tuple[str, bytes, str]:
    """Accept only bounded, proven document formats before retaining anything."""
    name = _safe_filename(filename)
    if not isinstance(content, bytes) or not content:
        raise MissionValidationError("The document is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise MissionValidationError("Mission documents must be 25 MiB or smaller.")
    suffix = Path(name).suffix.lower()
    if suffix == ".pdf":
        if not content.startswith(b"%PDF-"):
            raise MissionValidationError("The uploaded PDF has an invalid signature.")
        try:
            page_count = len(PdfReader(BytesIO(content), strict=True).pages)
        except (PdfReadError, ValueError, OverflowError) as exc:
            raise MissionValidationError("The uploaded PDF has an invalid structure.") from exc
        if page_count > MAX_PDF_PAGES:
            raise MissionValidationError(f"Mission PDFs may contain at most {MAX_PDF_PAGES} pages.")
        if len(_PDF_OBJECT.findall(content)) > MAX_PDF_OBJECTS:
            raise MissionValidationError("Mission PDF structure exceeds the object limit.")
    else:
        try:
            with zipfile.ZipFile(BytesIO(content)) as archive:
                entries = archive.infolist()
                if len(entries) > MAX_DOCX_ENTRIES:
                    raise MissionValidationError("Mission DOCX has too many archive entries.")
                total_size = 0
                for entry in entries:
                    entry_name = Path(entry.filename)
                    if (entry.is_dir() or entry_name.is_absolute() or ".." in entry_name.parts
                            or entry.file_size > MAX_DOCX_ENTRY_BYTES):
                        raise MissionValidationError("Mission DOCX contains an unsafe archive entry.")
                    total_size += entry.file_size
                    if total_size > MAX_DOCX_UNCOMPRESSED_BYTES:
                        raise MissionValidationError("Mission DOCX exceeds the decompressed-size limit.")
                    if (entry.file_size and not entry.compress_size) or (
                            entry.compress_size and entry.file_size / entry.compress_size
                            > MAX_DOCX_COMPRESSION_RATIO):
                        raise MissionValidationError("Mission DOCX exceeds the compression-ratio limit.")
                names = {entry.filename for entry in entries}
                if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                    raise MissionValidationError("The uploaded DOCX has an invalid structure.")
        except zipfile.BadZipFile as exc:
            raise MissionValidationError("The uploaded DOCX has an invalid signature.") from exc
    return name, content, hashlib.sha256(content).hexdigest()


def _brief_prompt(field: str) -> str:
    """Ask one grounded question per brief field.

    A single request for a whole JSON object failed every time against the
    stock 7B RAG model: it summarised the retrieved context in prose instead of
    emitting an object, so the operator never saw a brief at all. One short
    question per field uses the same path that already answers mission
    questions correctly, and needs no structured decoding.
    """
    return f"""{BRIEF_QUESTIONS[field]}
{BRIEF_EXCLUSION}
Answer with one concise sentence drawn only from the mission documents in the
retrieved context, or exactly "{NOT_SPECIFIED}" when they do not establish it.
Do not infer facts. Do not include citations, filenames, page numbers, markdown,
or any commentary outside the sentence."""


def _mission_prompt(question: str) -> str:
    return f"""Answer the operator's question using only the mission documents in the
retrieved context. If the documents do not establish the answer, reply exactly:
That information is not available in the mission material.
Do not use general knowledge, do not invent citations or page numbers, and keep
the answer concise.

Operator question: {question}"""


class MissionService:
    """Own retained originals; leave all retrieval/vector work to stock Chat Q&A."""

    def __init__(
        self,
        cfg: dict,
        services,
        *,
        generate: Callable[[str], str] | None = None,
        on_corpus_change: Callable[[], object] | None = None,
    ):
        self.cfg = cfg
        self.services = services
        self.generate = generate
        self.on_corpus_change = on_corpus_change
        self.db_path = cfg["db_path"]
        self.source_dir = Path(cfg["mission_sources_dir"])
        self.source_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = float(cfg.get("mission_ingest_timeout_seconds", 60))
        self.poll_seconds = float(cfg.get("mission_ingest_poll_seconds", 0.25))
        self._lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._mode = "starting"
        self._detail = "Waiting to reconcile the stock mission corpus."
        self._worker: threading.Thread | None = None
        self._brief_state_lock = threading.Lock()
        self._brief_running = False
        self._brief_queued = False

    @contextmanager
    def _connection(self) -> Iterator:
        conn = connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _set_mode(self, mode: str, detail: str) -> None:
        with self._state_lock:
            self._mode, self._detail = mode, detail

    def _state(self) -> tuple[str, str]:
        with self._state_lock:
            return self._mode, self._detail

    def _version(self, conn) -> int:
        row = conn.execute("SELECT value FROM app_state WHERE key = 'mission_corpus_version'").fetchone()
        return int(row["value"]) if row else 0

    def _bump_version(self, conn) -> int:
        version = self._version(conn) + 1
        conn.execute(
            """INSERT INTO app_state (key, value) VALUES ('mission_corpus_version', ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (str(version),),
        )
        conn.execute("UPDATE mission_brief SET stale = 1 WHERE stale = 0")
        self._set_brief_error(conn, None)
        return version

    def _notify_corpus_change(self) -> None:
        if self.on_corpus_change is not None:
            self.on_corpus_change()

    def _brief_error(self, conn) -> str | None:
        row = conn.execute("SELECT value FROM app_state WHERE key = 'mission_brief_error'").fetchone()
        return row["value"] if row else None

    def _set_brief_error(self, conn, message: str | None) -> None:
        if message is None:
            conn.execute("DELETE FROM app_state WHERE key = 'mission_brief_error'")
            return
        conn.execute(
            """INSERT INTO app_state (key, value) VALUES ('mission_brief_error', ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (message[:500],),
        )

    def _brief_is_stale(self, conn) -> bool:
        version = self._version(conn)
        row = conn.execute(
            """SELECT corpus_version, stale FROM mission_brief
               ORDER BY brief_id DESC LIMIT 1""").fetchone()
        return not row or row["corpus_version"] != version or bool(row["stale"])

    def _source_path(self, digest: str) -> Path:
        if not _SHA256.fullmatch(digest):
            raise MissionError("stored mission source has an invalid digest")
        path = (self.source_dir / digest).resolve()
        if not path.is_relative_to(self.source_dir.resolve()):
            raise MissionError("stored mission source escapes its directory")
        return path

    def _documents(self) -> set[str]:
        try:
            response = self.services.client.get(
                self.services.urls["chatqna"] + "/documents", timeout=self.timeout)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MissionUnavailable("Mission service is unavailable; corpus state cannot be verified.") from exc
        metadata = body.get("metadata") if isinstance(body, dict) else None
        documents = metadata.get("documents") if isinstance(metadata, dict) else None
        if (not isinstance(documents, list) or any(
                not isinstance(name, str) or len(name) > MAX_FILENAME for name in documents)):
            raise MissionUnavailable("Mission service returned an invalid document inventory.")
        if len(documents) != len(set(documents)):
            raise MissionUnavailable("Mission service returned duplicate document names.")
        return set(documents)

    def _post_document(self, filename: str, content: bytes) -> None:
        try:
            response = self.services.client.post(
                self.services.urls["chatqna"] + "/documents",
                files={"files": (filename, content, "application/octet-stream")},
                timeout=self.timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise MissionUnavailable("Mission document ingestion failed at the stock service.") from exc

    def _wait_for_document(self, filename: str) -> None:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if filename in self._documents():
                return
            time.sleep(self.poll_seconds)
        raise MissionUnavailable("Mission document ingestion did not become visible before the timeout.")

    def _store_original(self, digest: str, content: bytes) -> None:
        target = self._source_path(digest)
        if target.is_file():
            if hashlib.sha256(target.read_bytes()).hexdigest() == digest:
                return
            raise MissionError("retained mission source digest does not match its contents")
        temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_bytes(content)
            temporary.chmod(0o600)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    def _delete_original(self, digest: str) -> None:
        self._source_path(digest).unlink(missing_ok=True)

    def _sources(self, conn) -> list[dict]:
        return [
            dict(row) for row in conn.execute(
                """SELECT filename, sha256, uploaded_utc, status, corpus_version
                   FROM mission_sources ORDER BY uploaded_utc, filename""")
        ]

    def _queue_brief(self) -> None:
        with self._brief_state_lock:
            if self._brief_running:
                self._brief_queued = True
                return
            self._brief_running = True
        worker = threading.Thread(target=self._brief_worker, name="mission-brief", daemon=True)
        worker.start()

    def _brief_active(self) -> bool:
        with self._brief_state_lock:
            return self._brief_running

    def _brief_worker(self) -> None:
        try:
            self._generate_brief()
        except Exception as exc:
            log.exception("mission brief worker failed")
            try:
                with self._lock, self._connection() as conn:
                    self._set_brief_error(conn, brief_failure_message(exc))
                    conn.commit()
            except Exception:
                log.exception("could not persist mission brief worker failure")
        finally:
            with self._brief_state_lock:
                rerun = self._brief_queued
                self._brief_running = self._brief_queued = False
            if rerun:
                self._queue_brief()

    def _complete(self, prompt: str, generate: Callable[[str], str] | None = None) -> str:
        callback = generate or self.generate
        if callback is None:
            raise MissionUnavailable("Mission answer generation is not configured.")
        try:
            completion = callback(prompt)
        except MissionError:
            raise
        except Exception as exc:
            log.warning("mission generation failed: %s", exc)
            raise MissionUnavailable("Mission text generation is unavailable.") from exc
        if not isinstance(completion, str) or not completion.strip():
            raise MissionUnavailable("Mission service returned an empty answer.")
        return completion.strip()

    def _inventory_ready(self, conn) -> list[dict]:
        sources = self._sources(conn)
        if not sources:
            raise MissionUnavailable("No mission documents are ready.")
        remote = self._documents()
        missing = [source for source in sources if source["filename"] not in remote]
        pending = [source for source in sources if source["status"] != "ready"]
        if missing or pending:
            self.start()
            raise MissionUnavailable("Mission knowledge is reingesting before it can answer safely.")
        return sources

    def start(self, *, retry_brief: bool = False) -> None:
        """Reconcile asynchronously so a restart visibly degrades mission mode."""
        with self._lock:
            if self._worker and self._worker.is_alive():
                return
            self._set_mode("reingesting", "Reconciling retained mission sources with the stock corpus.")
            self._worker = threading.Thread(
                target=self._reconcile, args=(retry_brief,), name="mission-reconcile", daemon=True)
            self._worker.start()

    def _reconcile(self, retry_brief: bool = False) -> None:
        try:
            needs_brief = False
            # This lock covers the stock service mutation and poll. A clear or
            # delete cannot interleave and orphan an uploaded stock document.
            with self._lock, self._connection() as conn:
                sources = self._sources(conn)
                remote = self._documents()
                missing = [source for source in sources if source["filename"] not in remote]
                if missing:
                    conn.execute(
                        "UPDATE mission_sources SET status = 'reingesting' WHERE filename IN (%s)"
                        % ",".join("?" for _ in missing),
                        [source["filename"] for source in missing],
                    )
                    conn.commit()
                    for source in missing:
                        content = self._source_path(source["sha256"]).read_bytes()
                        self._post_document(source["filename"], content)
                        self._wait_for_document(source["filename"])
                    version = self._bump_version(conn)
                    conn.execute(
                        "UPDATE mission_sources SET status = 'ready', corpus_version = ?", (version,))
                else:
                    conn.execute(
                        "UPDATE mission_sources SET status = 'ready' WHERE status != 'ready'")
                conn.commit()
                self._set_mode(
                    "ready",
                    "Mission corpus was recovered from retained originals." if missing
                    else "Mission corpus is reconciled.",
                )
                if retry_brief:
                    self._set_brief_error(conn, None)
                needs_brief = (bool(sources) and self._brief_is_stale(conn)
                               and self._brief_error(conn) is None
                               and not self._brief_active())
            if missing:
                self._notify_corpus_change()
            if needs_brief:
                self._queue_brief()
        except Exception as exc:
            # Anything escaping here used to kill the worker thread and strand
            # the panel in "reingesting" with no way back. A visible degraded
            # state with a Retry action is always better than a silent hang.
            log.warning("mission reconciliation failed: %s", exc, exc_info=not isinstance(
                exc, (MissionError, OSError)))
            message = str(exc) if isinstance(exc, MissionError) else (
                "Mission recovery failed. Select Retry Brief / Re-ingest to try again.")
            try:
                with self._lock, self._connection() as conn:
                    conn.execute(
                        "UPDATE mission_sources SET status = 'failed' WHERE status = 'reingesting'")
                    conn.commit()
                    self._set_mode("degraded", message)
            except Exception:
                log.exception("mission reconciliation could not record its own failure")
                self._set_mode("degraded", message)

    def status(self) -> dict:
        needs_brief = False
        with self._connection() as conn:
            sources = self._sources(conn)
            try:
                remote = self._documents()
            except MissionUnavailable as exc:
                remote = None
                self._set_mode("degraded", str(exc))
            else:
                missing = [source for source in sources if source["filename"] not in remote]
                if missing:
                    if not any(source["status"] in {"ingesting", "reingesting"} for source in missing):
                        self.start()
                elif all(source["status"] == "ready" for source in sources):
                    self._set_mode("ready", "Mission corpus is reconciled.")
                    needs_brief = (bool(sources) and self._brief_is_stale(conn)
                                   and self._brief_error(conn) is None
                                   and not self._brief_active())
            mode, detail = self._state()
            result = {
                "mode": mode,
                "detail": detail,
                "corpus_version": self._version(conn),
                "sources": [
                    {**source, "in_corpus": remote is not None and source["filename"] in remote}
                    for source in sources
                ],
                "stock_documents": sorted(remote) if remote is not None else None,
                "answer_citations_available": False,
                "citation_notice": (
                    "This stock interface does not provide answer-level citations. "
                    "The source list is an inventory, not proof for an individual answer."
                ),
            }
        if needs_brief:
            self._queue_brief()
        return result

    def upload(self, filename: object, content: object) -> dict:
        filename, content, digest = validate_upload(filename, content)
        needs_brief = False
        with self._lock, self._connection() as conn:
            remote = self._documents()
            existing = conn.execute(
                "SELECT sha256 FROM mission_sources WHERE filename = ?", (filename,)).fetchone()
            if existing:
                if existing["sha256"] == digest:
                    raise MissionValidationError("A document with this name and content is already retained.")
                raise MissionValidationError("A retained mission document already uses this filename.")
            if filename in remote:
                raise MissionValidationError(
                    "The stock corpus already uses this filename; delete it explicitly before uploading a replacement.")
            version = self._version(conn)
            self._store_original(digest, content)
            conn.execute(
                """INSERT INTO mission_sources
                   (filename, sha256, uploaded_utc, status, corpus_version)
                   VALUES (?, ?, ?, 'ingesting', ?)""",
                (filename, digest, _utc(), version),
            )
            conn.commit()
            self._set_mode("ingesting", f"Ingesting {filename}.")
            try:
                self._post_document(filename, content)
                self._wait_for_document(filename)
            except MissionError:
                conn.execute("UPDATE mission_sources SET status = 'failed' WHERE filename = ?", (filename,))
                conn.commit()
                self._set_mode("degraded", f"Ingestion failed for {filename}; the retained original is available to retry.")
                raise
            version = self._bump_version(conn)
            conn.execute(
                "UPDATE mission_sources SET status = 'ready', corpus_version = ? WHERE filename = ?",
                (version, filename),
            )
            conn.commit()
            self._set_mode("ready", "Mission corpus is reconciled.")
            needs_brief = True
        if needs_brief:
            self._notify_corpus_change()
            self._queue_brief()
        return self.status()

    def delete(self, filename: object, confirmation: object) -> dict:
        filename = _safe_filename(filename)
        if confirmation != filename:
            raise MissionValidationError("Confirmation must exactly match the document filename.")
        with self._lock, self._connection() as conn:
            source = conn.execute(
                "SELECT sha256 FROM mission_sources WHERE filename = ?", (filename,)).fetchone()
            if not source:
                raise MissionValidationError("Unknown retained mission document.")
            try:
                response = self.services.client.delete(
                    self.services.urls["chatqna"] + "/documents",
                    params={"document": filename}, timeout=self.timeout)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise MissionUnavailable("Mission document deletion failed at the stock service.") from exc
            if filename in self._documents():
                raise MissionUnavailable("Mission service still reports the document after deletion.")
            conn.execute("DELETE FROM mission_sources WHERE filename = ?", (filename,))
            remaining = conn.execute(
                "SELECT count(*) AS n FROM mission_sources WHERE sha256 = ?", (source["sha256"],)
            ).fetchone()["n"]
            if not remaining:
                self._delete_original(source["sha256"])
            self._bump_version(conn)
            conn.commit()
            self._set_mode("ready", "Mission corpus is reconciled.")
        self._notify_corpus_change()
        self._queue_brief()
        return self.status()

    def clear(self, confirmation: object) -> dict:
        if confirmation != "DELETE MISSION":
            raise MissionValidationError("Confirmation must equal 'DELETE MISSION'.")
        with self._lock, self._connection() as conn:
            try:
                response = self.services.client.delete(
                    self.services.urls["chatqna"] + "/documents",
                    params={"delete_all": "true"}, timeout=self.timeout)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise MissionUnavailable("Mission corpus deletion failed at the stock service.") from exc
            if self._documents():
                raise MissionUnavailable("Mission service still reports documents after clear.")
            for source in self._sources(conn):
                self._delete_original(source["sha256"])
            conn.execute("DELETE FROM mission_sources")
            self._bump_version(conn)
            conn.commit()
            self._set_mode("ready", "Mission corpus is empty.")
        self._notify_corpus_change()
        return self.status()

    def _generate_brief(self) -> None:
        with self._lock, self._connection() as conn:
            version = self._version(conn)
            try:
                sources = self._inventory_ready(conn)
            except MissionUnavailable:
                return
        try:
            brief = {}
            for field in BRIEF_FIELDS:
                answer = self._complete(_brief_prompt(field)).strip()
                # Preserve mixed known/unknown facts and the complete generated text.
                # Only an exact absence answer is safe to normalize.
                if answer.rstrip(".").casefold() == NOT_SPECIFIED.casefold():
                    answer = NOT_SPECIFIED
                brief[field] = answer or NOT_SPECIFIED
        except (MissionError, ValueError, TypeError) as exc:
            log.warning("mission brief generation failed: %s", exc)
            with self._lock, self._connection() as conn:
                conn.execute(
                    "UPDATE mission_brief SET stale = 1 WHERE corpus_version = ?", (version,))
                self._set_brief_error(conn, brief_failure_message(exc))
                conn.commit()
            return
        with self._lock, self._connection() as conn:
            if self._version(conn) != version:
                log.info("discarded mission brief for obsolete corpus version %s", version)
                self._queue_brief()
                return
            conn.execute(
                """INSERT INTO mission_brief (corpus_version, generated_utc, brief_json, stale)
                   VALUES (?, ?, ?, 0)""",
                (version, _utc(), json.dumps({
                    "fields": brief,
                    "source_inventory": [source["filename"] for source in sources],
                    "answer_citations_available": False,
                }, separators=(",", ":"))),
            )
            self._set_brief_error(conn, None)
            conn.commit()

    def brief(self) -> dict:
        needs_brief = False
        with self._connection() as conn:
            version = self._version(conn)
            mode, _ = self._state()
            row = conn.execute(
                """SELECT corpus_version, generated_utc, brief_json, stale
                   FROM mission_brief ORDER BY brief_id DESC LIMIT 1""").fetchone()
            if not row or row["corpus_version"] != version or row["stale"]:
                result = {
                    "corpus_version": version,
                    "brief": None,
                    "stale": True,
                    "status": mode,
                    "source_inventory": [source["filename"] for source in self._sources(conn)],
                    "error": self._brief_error(conn),
                }
                needs_brief = (bool(result["source_inventory"]) and mode == "ready"
                               and result["error"] is None
                               and not self._brief_active())
            else:
                try:
                    body = json.loads(row["brief_json"])
                except json.JSONDecodeError:
                    body = None
                if not isinstance(body, dict):
                    result = {
                        "corpus_version": version, "brief": None, "stale": True,
                        "status": mode, "error": "Stored Mission Brief is invalid.",
                    }
                    needs_brief = True
                else:
                    result = {
                        "corpus_version": version,
                        "generated_utc": row["generated_utc"],
                        "brief": body.get("fields"),
                        "source_inventory": body.get("source_inventory", []),
                        "answer_citations_available": False,
                        "stale": False,
                        "status": mode,
                        "error": self._brief_error(conn),
                    }
        if needs_brief:
            self._queue_brief()
        return result

    def answer(self, question: object, *, generate: Callable[[str], str] | None = None) -> dict:
        if not isinstance(question, str) or not question.strip() or len(question) > 500:
            raise MissionValidationError("Mission questions must contain 1 to 500 characters.")
        mode, _ = self._state()
        if mode in {"ingesting", "reingesting"}:
            raise MissionUnavailable("Mission knowledge is reingesting before it can answer safely.")
        with self._lock, self._connection() as conn:
            version = self._version(conn)
            sources = self._inventory_ready(conn)
        answer = self._complete(_mission_prompt(question.strip()), generate)
        with self._lock, self._connection() as conn:
            if self._version(conn) != version:
                raise MissionUnavailable("Mission corpus changed while the answer was generated; ask again.")
            self._inventory_ready(conn)
        return {
            "intent": "mission_knowledge",
            "answer_text": answer.strip(),
            "spoken_text": None,
            "source_inventory": sources,
            "answer_citations_available": False,
            "citation_notice": "Answer-level citations are unavailable from the stock mission interface.",
            "errors": [],
        }
