"""Config, SQLite access and the response envelope."""
from __future__ import annotations

import copy
import fcntl
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("backend.core")

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

DEFAULTS: dict[str, Any] = {
    "db_path": "/data/app.db",
    "mission_sources_dir": "/data/mission_sources",
    "mission_ingest_timeout_seconds": 60.0,
    "mission_ingest_poll_seconds": 0.25,
    "crop_dir": "/crops",
    "crop_cleanup_script": "/opt/handheld-crops/crop_candidates.py",
    "detector_metadata_path": "/config/detector/metadata.yaml",
    "max_streams": 3,  # current operator deployment; configurable in backend.yaml
    "streams": [
        {"stream_id": "cam1", "display_name": "Camera 1", "sort_order": 0, "enabled": True},
    ],
    "services": {
        "chatqna": "http://chatqna:8888",
        "audio_analyzer": "http://audio-analyzer:9999",
        "tts": "http://tts:9998",
        "dlsps": "http://dlsps:8080",
        "mediamtx": "http://mediamtx:9997",
        "alert_agent": "http://alert-agent:8010",
        "metrics": "http://metrics:9090",
    },
    "mqtt": {"host": "mqtt", "port": 1883, "topic": "handheld/observations"},
    # DLSPS publishes every pipeline instance here; cameras are demultiplexed
    # from crop_context.stream_id, not from the topic.
    "detections_topic": "detections/live",
    # The crop producer announces finished snapshots on a separate topic; it is
    # the same demultiplexing rule, so one subscription covers every camera.
    "snips_topic": "detections/snips",
    # Section 6.2: a gap longer than this starts a new visibility interval
    # rather than implying the object was visible across it.
    "absence_split_seconds": 5.0,
    # Section 6.4: no observation for this long closes the track as `timeout`.
    # Design's initial value is 10s of real silence, but observations reach the
    # broker ~3.8s after the producer stamps them (measured), and that delay is
    # inside the stock pipeline, so the threshold carries it to keep the
    # intended ~10s of actual silence.
    "track_timeout_seconds": 14.0,
    # Section 6.5 condition 3: how recently a track must have been seen to count
    # as current. The design's initial 3s is shorter than the measured delivery
    # latency below, which would make every camera permanently "absent"; this is
    # set from measurement, not preference. See docs/application/evidence/phase3.
    "freshness_window_seconds": 8.0,
    # Section 8.1: a crop that arrives before its observation waits this long for
    # a track to attach to. Bounded by age alone.
    "pending_association_seconds": 120.0,
    "detector_profile_path": "/config/detector-profile.json",
    "history_retention_seconds": 604800,
    "assistant_chat_timeout_seconds": 60.0,
    "assistant_queue_depth": 4,
    "rtsp_base": "rtsp://mediamtx:8554",
    "supervise_seconds": 15.0,
    "pipeline_start_timeout_seconds": 180.0,
    "pipeline_start_poll_seconds": 2.0,
    "feed_stale_seconds": 90.0,
    "takeover_seconds": 30.0,  # must match crop config takeover_seconds
    "telemetry_host": "handheld",
    # Section 16: rule dispatch and speech. Tools are stock Alert Agent tools.
    "alert_tools": ["log_alert"],
    "alert_dispatch_timeout_seconds": 15.0,
    "tts_timeout_seconds": 30.0,
    "alert_speech_floor_seconds": 3.0,
    "speech_ack_text": "Checking.",
    "voice_warm_interval_seconds": 20.0,
    "timeout_seconds": 5.0,
    "log_level": "INFO",
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | None = None) -> dict[str, Any]:
    path = path or os.environ.get("BACKEND_CONFIG", "/config/backend.yaml")
    # Deep copy: the derivation below calls setdefault on the stream dicts, which
    # would otherwise permanently edit DEFAULTS for every later call in the
    # process (a file that omits `streams` shares the default list by reference).
    cfg = copy.deepcopy(DEFAULTS)
    file = Path(path)
    if file.is_file():
        cfg = _merge(cfg, yaml.safe_load(file.read_text()) or {})
    # The demo publisher count is authoritative only in the demo Compose override.
    demo_count = os.environ.get("BACKEND_DEMO_CAMERAS")
    if demo_count is not None:
        count = int(demo_count)
        wanted = {f"cam{i}" for i in range(1, count + 1)}
        available = {stream["stream_id"] for stream in cfg["streams"] if stream.get("enabled", True)}
        if count < 1 or not wanted <= available:
            raise ValueError("demo camera count must name enabled cam1…camN sources in backend.yaml")
        for stream in cfg["streams"]:
            stream["enabled"] = stream["stream_id"] in wanted
    for stream in cfg["streams"]:
        stream.setdefault("annotated_path", "annotated" + stream["stream_id"].removeprefix("cam"))
        stream.setdefault("source_uri", f"{cfg['rtsp_base']}/{stream['stream_id']}")
    if len(cfg["streams"]) > cfg["max_streams"]:
        raise ValueError(
            f"{len(cfg['streams'])} cameras configured but max_streams is {cfg['max_streams']}; "
            "raise max_streams only with a measurement behind it (design section 3.4)"
        )
    return cfg


class _JsonLog(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z",
            "level": record.levelname,
            "logger": record.name,
            "request_id": request_id_var.get(),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonLog())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # One line per outbound request, times seven probes every 5 s, drowns out
    # everything worth reading. Failures still surface: adapters log their own.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def envelope(data: Any = None, *, error: dict | None = None) -> dict:
    """The single response shape. `error` is None on success, never both set."""
    return {
        "request_id": request_id_var.get(),
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "data": data,
        "error": error,
    }


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Continuous track ingestion from every camera keeps the write lock busy.
    # Five seconds was not enough for the slower mission and history writers,
    # which surfaced as "database is locked" during corpus reconciliation.
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript((Path(__file__).parent / "schema.sql").read_text())
    if "incident_id" not in {r[1] for r in conn.execute("PRAGMA table_info(rule_matches)")}:
        conn.execute("ALTER TABLE rule_matches ADD COLUMN incident_id TEXT")
    incident_columns = {r[1] for r in conn.execute("PRAGMA table_info(alert_incidents)")}
    if "counted_from_utc" not in incident_columns:
        conn.execute("ALTER TABLE alert_incidents ADD COLUMN counted_from_utc TEXT")
    if "counted_after" not in incident_columns:
        conn.execute("ALTER TABLE alert_incidents ADD COLUMN counted_after INTEGER NOT NULL DEFAULT 0")
    conn.commit()


def sync_streams(conn: sqlite3.Connection, streams: list[dict]) -> None:
    """Config is the source of truth for the camera list; run state is not touched."""
    for stream in streams:
        conn.execute(
            """INSERT INTO streams (stream_id, display_name, source_uri, annotated_path,
                                    enabled, sort_order)
               VALUES (:stream_id, :display_name, :source_uri, :annotated_path,
                       :enabled, :sort_order)
               ON CONFLICT(stream_id) DO UPDATE SET
                 display_name=excluded.display_name, source_uri=excluded.source_uri,
                 annotated_path=excluded.annotated_path, enabled=excluded.enabled,
                 sort_order=excluded.sort_order""",
            {**stream, "enabled": int(stream.get("enabled", True))},
        )
    # A camera dropped from the config is disabled, never deleted: reconcile()
    # then sweeps its pipeline as an orphan and /api/streams stops offering it,
    # while its recorded history stays queryable. Deleting the row here would
    # destroy evidence as a side effect of an edit to a config file.
    configured = [s["stream_id"] for s in streams]
    holes = ",".join("?" * len(configured)) or "''"
    retired = conn.execute(
        f"UPDATE streams SET enabled = 0 WHERE enabled = 1 AND stream_id NOT IN ({holes})",
        configured).rowcount
    if retired:
        log.warning("%d camera(s) removed from config and disabled; history retained", retired)
    conn.commit()


def crop_store_busy(crop_dir: str, stream_id: str, takeover_seconds: float = 30.0) -> bool:
    """True while starting a pipeline for this camera would fail on the store.

    The producer holds fcntl.LOCK_EX on <output_dir>/.lock for its whole life and
    refreshes status.json every report tick. gvapython in the pinned image has no
    teardown hook, so an aborted pipeline leaks the lock forever; the producer
    treats a store whose status.json has gone stale as abandoned and takes it
    over. Backend uses the same rule, so it defers only while a start would
    actually be refused.
    """
    path = Path(crop_dir) / stream_id / ".lock"
    if not path.exists():
        return False
    try:
        handle = open(path, "a")
    except OSError:
        return True
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    except OSError:
        pass
    finally:
        handle.close()
    try:
        idle = time.time() - (Path(crop_dir) / stream_id / "status.json").stat().st_mtime
    except FileNotFoundError:
        return True
    return idle < takeover_seconds


# Producer-generated names only: uuid4().hex + ".jpg". Anything else is either a
# bug or an attempt to escape the store, and both deserve the same refusal.
STORAGE_KEY = re.compile(r"^[a-f0-9]{32}\.jpg$")


def resolve_crop_path(crop_dir: str, stream_id: str, storage_key: str) -> Path:
    """Map (camera, storage_key) to a real file, or raise ValueError.

    Section 8.3. The regex alone would be enough today, but it is the *pair* that
    holds: the pattern rejects separators and traversal, and the realpath check
    below still refuses a symlink planted inside the store that points out of it.
    Defence is cheap here and the blast radius of getting it wrong is the host
    filesystem, so both run rather than trusting either alone.
    """
    if not STORAGE_KEY.match(storage_key or ""):
        raise ValueError("malformed storage key")
    root = Path(crop_dir).resolve()
    # stream_id is never interpolated raw: only a registered camera reaches here,
    # but resolving and re-checking costs nothing and survives a future caller.
    target = (root / stream_id / storage_key).resolve()
    if not target.is_relative_to(root):
        raise ValueError("storage key escapes the crop root")
    return target
