#!/usr/bin/env python3
"""Self-check for Backend logic that can fail silently. Run: python3 backend/test_backend.py

Deliberately framework-free and offline: it covers config derivation, the
max_streams guard, schema migration, stream sync and observation demultiplexing.
Service adapters and the pipeline lifecycle are not mocked here - they are proven
against the real pinned containers via /api/health and the Phase 2 evidence,
which are the only assertions that actually mean anything for them.
"""
from contextlib import nullcontext
import hashlib
import json
import sqlite3
import sys
import tempfile
import threading
import time
import types
import zipfile
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))

# Enough distinct pipeline metadata field names to exceed the described subset.
CATALOG_OVERFLOW = 130

from app.core import connect, envelope, load_config, migrate, request_id_var, sync_streams


def _pdf_bytes(page_count=1, escaped_page_name=False):
    page_type = b"/Pa#67e" if escaped_page_name else b"/Page"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + b" ".join(
            f"{index} 0 R".encode() for index in range(3, page_count + 3)
        ) + f"] /Count {page_count} >>".encode(),
    ] + [b"<< /Type " + page_type + b" /Parent 2 0 R /MediaBox [0 0 1 1] >>"] * page_count
    output, offsets = bytearray(b"%PDF-1.7\n"), []
    for index, body in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    output.extend(b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets))
    output.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(output)


def test_config_derives_paths():
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write("streams:\n  - {stream_id: cam3, display_name: Rear, sort_order: 2}\n")
    cfg = load_config(fh.name)
    stream = cfg["streams"][0]
    assert stream["annotated_path"] == "annotated3", stream
    assert stream["source_uri"] == "rtsp://mediamtx:8554/cam3", stream


def test_loading_config_twice_does_not_edit_defaults():
    """load_config derives stream fields in place; DEFAULTS must survive it."""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write("rtsp_base: rtsp://elsewhere:8554\n")  # no `streams`: uses the default list
    first = load_config(fh.name)
    assert first["streams"][0]["source_uri"] == "rtsp://elsewhere:8554/cam1", first["streams"]
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write("{}\n")
    second = load_config(fh.name)
    assert second["streams"][0]["source_uri"] == "rtsp://mediamtx:8554/cam1", (
        "the first call leaked its derived values into DEFAULTS")


def test_max_streams_guard():
    cams = "\n".join(
        f"  - {{stream_id: cam{i}, display_name: C{i}, sort_order: {i}}}" for i in range(1, 7)
    )
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(f"max_streams: 4\nstreams:\n{cams}\n")
    try:
        load_config(fh.name)
    except ValueError as exc:
        assert "max_streams" in str(exc)
    else:
        raise AssertionError("6 cameras with max_streams=4 must be refused")


def test_migrate_is_idempotent_and_sync_preserves_run_state():
    with tempfile.TemporaryDirectory() as tmp:
        conn = connect(str(Path(tmp) / "app.db"))
        migrate(conn)
        migrate(conn)  # a restart must not fail or wipe anything

        streams = [
            {
                "stream_id": "cam1",
                "display_name": "Camera 1",
                "source_uri": "rtsp://mediamtx:8554/cam1",
                "annotated_path": "annotated1",
                "enabled": True,
                "sort_order": 0,
            }
        ]
        sync_streams(conn, streams)
        conn.execute(
            "UPDATE streams SET history_generation='abc', dlsps_instance_id='i-1', "
            "clear_state='DRAINING' WHERE stream_id='cam1'"
        )
        conn.commit()

        streams[0]["display_name"] = "Front door"
        sync_streams(conn, streams)
        row = conn.execute("SELECT * FROM streams WHERE stream_id='cam1'").fetchone()

        assert row["display_name"] == "Front door", "config must win for descriptive fields"
        # D16: a config reload must never lose pipeline ownership or an in-flight
        # clear, or crash recovery cannot tell what it owns.
        assert row["history_generation"] == "abc", row["history_generation"]
        assert row["dlsps_instance_id"] == "i-1", row["dlsps_instance_id"]
        assert row["clear_state"] == "DRAINING", row["clear_state"]
        conn.close()


def _observation(stream_id, tracker_id, label="person", observed_at=1000.0, session="s1",
                 generation="g1"):
    return json.dumps({"metadata": {
        "crop_context": {"stream_id": stream_id, "tracking_session_id": session,
                         "history_generation": generation, "observed_at": observed_at,
                         "frame_sequence": 1},
        "objects": [{"id": tracker_id, "detection": {"label": label, "confidence": 0.9}}],
    }, "blob": ""}).encode()


def _ingest(tmp, cameras=("cam1", "cam2")):
    """Real Ingest, real SQLite, no broker: only _on_message is exercised."""
    from app.ingest import Ingest

    crops = Path(tmp) / "crops"
    for camera in cameras:
        (crops / camera).mkdir(parents=True, exist_ok=True)
    cfg = {"streams": [{"stream_id": c} for c in cameras],
           "mqtt": {"host": "unused", "port": 0}, "detections_topic": "detections/live",
           "snips_topic": "detections/snips", "crop_dir": str(crops),
           "track_timeout_seconds": 10.0, "freshness_window_seconds": 3.0,
           "absence_split_seconds": 5.0, "pending_association_seconds": 120.0}
    db = str(Path(tmp) / "ingest.db")
    conn = connect(db)
    migrate(conn)
    sync_streams(conn, [{"stream_id": c, "display_name": c, "source_uri": "u",
                         "annotated_path": "a", "enabled": True, "sort_order": 0}
                        for c in cameras])
    conn.close()
    return Ingest(cfg, db)


def test_colliding_tracker_ids_stay_per_camera():
    """Each pipeline runs its own tracker from 1, so ID 1 means nothing globally."""
    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp)
        for stream_id in ("cam1", "cam2"):
            ing._on_message(None, None, types.SimpleNamespace(
                topic="detections/live", payload=_observation(stream_id, 1)))
        ing.conn.commit()
        rows = ing.conn.execute(
            "SELECT stream_id, tracker_id FROM tracks ORDER BY stream_id").fetchall()
        assert [(r["stream_id"], r["tracker_id"]) for r in rows] == [("cam1", 1), ("cam2", 1)], rows
        ing.conn.close()


def test_replayed_observation_is_rejected():
    """QoS-1 redelivery must not resurrect a retired generation after a clear."""
    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp)
        msg = types.SimpleNamespace(topic="detections/live",
                                    payload=_observation("cam1", 1, observed_at=2000.0))
        ing._on_message(None, None, msg)
        stale = types.SimpleNamespace(topic="detections/live", payload=_observation(
            "cam1", 1, observed_at=1000.0, session="old", generation="g0"))
        ing._on_message(None, None, stale)
        ing.conn.commit()
        rows = ing.conn.execute("SELECT history_generation FROM tracks").fetchall()
        assert [r["history_generation"] for r in rows] == ["g1"], rows
        ing.conn.close()


def test_unknown_camera_and_missing_context_are_dropped():
    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp, cameras=("cam1",))
        ing._on_message(None, None, types.SimpleNamespace(
            topic="t", payload=_observation("cam9", 1)))
        ing._on_message(None, None, types.SimpleNamespace(
            topic="t", payload=json.dumps({"metadata": {"objects": []}}).encode()))
        ing.conn.commit()
        assert ing.conn.execute("SELECT count(*) n FROM tracks").fetchone()["n"] == 0
        ing.conn.close()


def test_envelope_shape():
    token = request_id_var.set("req-1")
    body = envelope({"n": 1})
    assert set(body) == {"request_id", "generated_utc", "data", "error"}, body
    assert body["request_id"] == "req-1" and body["error"] is None
    assert envelope(error={"code": "x", "message": "y"})["data"] is None
    request_id_var.reset(token)


def test_telemetry_preserves_zero_unavailable_and_real_units():
    from app.telemetry import normalize

    def metric(name, value, **tags):
        return {"name": name, "tags": {"host": "handheld", **tags},
                "fields": {"value": value}, "timestamp": 2_000_000_000_000_000_000}

    payload = {"metrics": {
        "u": metric("cpu_usage_user", 10, cpu="cpu-total"),
        "s": metric("cpu_usage_system", 5, cpu="cpu-total"),
        "f": metric("cpu_frequency_avg_frequency", 1_250_000),
        "g": metric("gpu_frequency", 450, type="cur_freq"),
        "p": metric("gpu_power", 12.5, type="pkg_cur_power"),
        "n": metric("npu_utilization", 0),
        "m": metric("npu_memory_mb", -1),
    }}
    answer = normalize(payload, "handheld", now=2_000_000_000)
    metrics = {item["id"]: item for item in answer["metrics"]}
    assert metrics["cpu_load_percent"]["value"] == 15
    assert metrics["cpu_frequency_mhz"]["value"] == 1250
    assert metrics["cpu_frequency_mhz"]["unit"] == "MHz"
    assert metrics["gpu_frequency_mhz"]["source"] == "gpu_frequency"
    assert metrics["package_power_w"]["value"] == 12.5
    assert metrics["npu_utilization_percent"]["value"] == 0
    assert metrics["npu_utilization_percent"]["state"] == "ok"
    assert metrics["npu_utilization_percent"]["sampled_utc"] == "2033-05-18T03:33:20Z"
    assert metrics["npu_memory_mib"]["value"] is None
    assert metrics["npu_memory_mib"]["state"] == "unavailable"

    payload["metrics"]["n"]["timestamp"] = 1_999_999_990_000_000_000
    answer = normalize(payload, "handheld", now=2_000_000_000)
    metrics = {item["id"]: item for item in answer["metrics"]}
    assert answer["status"] == "ok", "one stale series must not stale the whole snapshot"
    assert metrics["npu_utilization_percent"]["state"] == "stale"
    assert metrics["cpu_load_percent"]["state"] == "ok"

    mismatch = normalize(payload, "other-host", now=2_000_000_000)
    assert mismatch["status"] == "host_mismatch"
    assert mismatch["metrics"] == []


def test_alert_feed_is_global_and_camera_labelled():
    from app import history

    with tempfile.TemporaryDirectory() as tmp:
        conn = connect(str(Path(tmp) / "app.db"))
        migrate(conn)
        sync_streams(conn, [{"stream_id": "cam1", "display_name": "cam1",
                             "source_uri": "u", "annotated_path": "a",
                             "enabled": True, "sort_order": 0}])
        cursor = conn.execute(
            """INSERT INTO tracks (stream_id, tracking_session_id, history_generation,
                 tracker_id, class_label, first_observed_utc, last_observed_utc,
                 confidence_freshness)
               VALUES ('cam1','s','g',1,'person','t','t','unverified')""")
        track_id = cursor.lastrowid
        conn.execute(
            """INSERT INTO rules (rule_id, name, enabled, spec_json)
               VALUES ('r1','entry',1,'{}')""")
        conn.execute(
            """INSERT INTO rule_matches
               (match_id, rule_id, track_id, matched_utc, dispatch_state)
               VALUES ('m1','r1',?,'2026-01-01T00:00:00Z','pending')""", (track_id,))
        conn.commit()
        answer = history.alerts(conn)
        assert answer[0]["stream_id"] == "cam1"
        assert answer[0]["class_label"] == "person"
        assert answer[0]["rule_name"] == "entry"
        conn.close()


def _crop(stream_id, tracker_id, candidate_id, *, session="s1", generation="g1",
          observed_at=1000.0, expires_in=3600.0):
    """A producer sidecar, verbatim: the same flat dict it publishes and fsyncs."""
    return json.dumps({
        "event": "crop_ready", "schema_version": 1, "stream_id": stream_id,
        "tracking_session_id": session, "generation": generation,
        "observed_at": observed_at, "frame_sequence": 7, "source_frame_id": 7,
        "tracker_id": tracker_id, "region_id": 1, "label": "person", "confidence": 0.9,
        "detection_rect": [1, 2, 30, 40], "crop_rect": [0, 0, 60, 80],
        "frame_width": 640, "frame_height": 480, "image_width": 60, "image_height": 80,
        "quality_score": 12.5, "sharpness": 3.0, "object_touches_frame": False,
        "aspect_limited": False, "candidate_id": candidate_id,
        "storage_key": candidate_id + ".jpg", "candidate_number": 1,
        "expires_at": observed_at + expires_in,
        "confidence_freshness": "tracker_metadata_unverified",
    }).encode()


def _send(ing, payload, topic="detections/snips"):
    ing._on_message(None, None, types.SimpleNamespace(topic=topic, payload=payload))


def test_crop_arriving_before_its_observation_is_parked_then_attached():
    """Section 8.1: the crop-first race must not lose evidence or invent a track."""
    cid = "a" * 32
    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp, cameras=("cam1",))
        _send(ing, _crop("cam1", 1, cid))
        row = ing.conn.execute("SELECT track_id FROM candidates").fetchone()
        assert row is not None and row["track_id"] is None, "crop-first must park, not drop"
        assert ing.conn.execute("SELECT count(*) n FROM tracks").fetchone()["n"] == 0, (
            "a parked crop must not fabricate a track")

        _send(ing, _observation("cam1", 1), topic="detections/live")
        row = ing.conn.execute("SELECT track_id FROM candidates").fetchone()
        assert row["track_id"] is not None, "the observation must adopt the parked crop"
        assert ing.counters["crop_attached"] == 1, dict(ing.counters)
        ing.conn.close()


def test_duplicate_candidate_is_ingested_once():
    """The producer republishes after a broker ack, and reconciliation replays."""
    cid = "b" * 32
    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp, cameras=("cam1",))
        for _ in range(3):
            _send(ing, _crop("cam1", 1, cid))
        assert ing.conn.execute("SELECT count(*) n FROM candidates").fetchone()["n"] == 1
        assert ing.counters["crop_duplicate"] == 2, dict(ing.counters)
        ing.conn.close()


def test_crop_from_a_retired_generation_is_rejected():
    """Step 9 of the reset handshake, on the crop path as well as observations."""
    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp, cameras=("cam1",))
        _send(ing, _observation("cam1", 1), topic="detections/live")   # adopts g1
        _send(ing, _crop("cam1", 1, "c" * 32, generation="g0"))
        assert ing.conn.execute("SELECT count(*) n FROM candidates").fetchone()["n"] == 0
        assert ing.counters["stale_generation_rejected"] == 1, dict(ing.counters)
        ing.conn.close()


def test_sidecar_reconciliation_recovers_undelivered_crops():
    """Mosquitto runs with persistence false, so the sidecars are the record."""
    cid = "d" * 32
    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp, cameras=("cam1",))
        directory = Path(ing.cfg["crop_dir"]) / "cam1"
        (directory / (cid + ".json")).write_bytes(_crop("cam1", 1, cid))
        # status.json lives in the same directory and is not a candidate.
        (directory / "status.json").write_text('{"stats": {}}')
        assert ing.reconcile_sidecars() == 1
        assert ing.reconcile_sidecars() == 0, "a second scan must not re-ingest"
        assert ing.conn.execute("SELECT count(*) n FROM candidates").fetchone()["n"] == 1
        ing.conn.close()


def test_expired_candidates_are_reflected_not_deleted():
    """Section 9.1: the track fact outlives its image."""
    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp, cameras=("cam1",))
        _send(ing, _observation("cam1", 1), topic="detections/live")
        _send(ing, _crop("cam1", 1, "e" * 32, observed_at=time.time(), expires_in=-1))
        ing._reflect_expiry()
        row = ing.conn.execute("SELECT image_state FROM candidates").fetchone()
        assert row["image_state"] == "expired", row["image_state"]
        assert ing.conn.execute("SELECT count(*) n FROM tracks").fetchone()["n"] == 1, (
            "expiring an image must not remove the track it belonged to")
        from app import history
        recent = history.recent(ing.conn, ing, ing.cfg, "cam1")
        assert recent["tracks"][0]["snip_state"] == "expired", recent
        assert recent["tracks"][0]["snip_url"] is None, recent
        ing.conn.close()


def test_history_filters_before_limit_and_reports_scope():
    """Furniture in the newest page must not hide older matching person tracks."""
    from app import history

    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp, cameras=("cam1",))
        ing.conn.execute(
            "UPDATE streams SET tracking_session_id='s1', history_generation='g1' "
            "WHERE stream_id='cam1'")
        base = time.time() - 3600
        for index in range(60):
            label = "chair" if index >= 5 else "person"
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(base + index))
            ing.conn.execute(
                """INSERT INTO tracks (
                     stream_id, tracking_session_id, history_generation, tracker_id,
                     class_label, first_observed_utc, last_observed_utc,
                     observation_count, confidence_freshness, closed_at)
                   VALUES ('cam1', 's1', 'g1', ?, ?, ?, ?, 1, 'unverified', ?)""",
                (index + 1, label, stamp, stamp, stamp))
        person_id = ing.conn.execute(
            "SELECT track_id FROM tracks WHERE class_label='person' ORDER BY track_id LIMIT 1"
        ).fetchone()["track_id"]
        ing.conn.execute(
            """INSERT INTO candidates (
                 candidate_id, track_id, stream_id, tracking_session_id,
                 history_generation, tracker_id, observed_at_utc, storage_key,
                 expires_at_utc, image_state)
               VALUES ('person-crop', ?, 'cam1', 's1', 'g1', 1,
                       '2026-01-01T00:00:00Z', 'person-crop.jpg',
                       '2099-01-01T00:00:00Z', 'present')""",
            (person_id,))
        ing.conn.commit()

        answer = history.recent(
            ing.conn, ing, ing.cfg, "cam1", limit=3, class_labels=("person",))
        assert len(answer["tracks"]) == 3, answer
        assert {track["class_label"] for track in answer["tracks"]} == {"person"}, answer
        assert answer["matched_count"] == 5 and answer["truncated"] is True, answer
        assert answer["class_counts"] == {"chair": 55, "person": 5}, answer
        assert answer["scope"] == {
            "kind": "tracking_session", "since_utc": None, "limit": 3}, answer

        with_image = history.recent(
            ing.conn, ing, ing.cfg, "cam1", limit=3,
            class_labels=("person",), has_image=True)
        assert with_image["matched_count"] == 1, with_image
        assert [track["track_id"] for track in with_image["tracks"]] == [person_id], with_image
        ing.conn.close()


def test_tracks_are_closed_on_timeout_and_startup():
    """Section 6.4: a restart must not present a dead process's tracks as live."""
    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp, cameras=("cam1",))
        _send(ing, _observation("cam1", 1, observed_at=time.time() - 3600),
              topic="detections/live")
        ing._close_timed_out()
        row = ing.conn.execute("SELECT closed_at, closure_reason FROM tracks").fetchone()
        assert row["closure_reason"] == "timeout", dict(row)

        # A still-open track from a previous process, closed before serving.
        ing.conn.execute("UPDATE tracks SET closed_at = NULL, closure_reason = NULL")
        ing.conn.commit()
        assert ing.recover() == 1
        row = ing.conn.execute("SELECT closure_reason FROM tracks").fetchone()
        assert row["closure_reason"] == "startup_recovery", dict(row)
        ing.conn.close()


def test_current_is_unknown_when_the_feed_is_not_advancing():
    """Section 6.5: a successful query on a dead feed never says "nobody there"."""
    from app import history

    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp, cameras=("cam1",))
        _send(ing, _observation("cam1", 1, observed_at=time.time()), topic="detections/live")
        ing.conn.commit()

        answer = history.current(ing.conn, ing, ing.cfg, "cam1")
        assert answer["current_presence"] == "present", answer
        assert answer["feed_ok"] is True and answer["data_ok"] is True, answer

        ing.health["cam1"] = (False, "no_signal")
        answer = history.current(ing.conn, ing, ing.cfg, "cam1")
        assert answer["data_ok"] is True, "SQLite succeeded; that is still true"
        assert answer["feed_ok"] is False, answer
        assert answer["current_presence"] == "unknown", (
            "a stale feed must never be reported as absence")
        assert answer["reason"] == "no_signal", answer
        ing.conn.close()


def test_a_track_is_queryable_before_it_is_committed():
    """Section 6.3: reads merge the in-memory map, so no query waits for a commit."""
    from app import history

    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp, cameras=("cam1",))
        _send(ing, _observation("cam1", 1, observed_at=time.time()), topic="detections/live")
        # Deliberately NOT committed: a separate reader connection cannot see the
        # row yet, which is exactly the window the live overlay exists to cover.
        reader = connect(ing.conn.execute("PRAGMA database_list").fetchone()["file"])
        answer = history.current(reader, ing, ing.cfg, "cam1")
        assert answer["current_presence"] == "present", answer
        assert [t["state_source"] for t in answer["tracks"]] == ["live"], answer["tracks"]
        assert answer["tracks"][0]["track_id"] is None, "an uncommitted track has no id yet"

        ing.conn.commit()
        answer = history.current(reader, ing, ing.cfg, "cam1")
        assert [t["state_source"] for t in answer["tracks"]] == ["committed"], answer["tracks"]
        assert answer["tracks"][0]["track_id"] is not None
        reader.close()
        ing.conn.close()


def test_storage_keys_cannot_escape_the_crop_root():
    """Section 8.3. The regex and the realpath check both have to hold."""
    from app.core import resolve_crop_path

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "crops"
        (root / "cam1").mkdir(parents=True)
        good = "f" * 32 + ".jpg"
        assert resolve_crop_path(str(root), "cam1", good) == (root / "cam1" / good).resolve()

        for bad in ("../../etc/passwd", "../" + "f" * 32 + ".jpg", "f" * 32 + ".jpg\x00.png",
                    "/etc/passwd", "F" * 32 + ".jpg", "f" * 31 + ".jpg", "", None):
            try:
                resolve_crop_path(str(root), "cam1", bad)
            except ValueError:
                continue
            raise AssertionError(f"storage key {bad!r} must be refused")

        # A symlink planted inside the store still points outside it; the name
        # is valid, so only the realpath check can catch this one.
        outside = Path(tmp) / "secret.jpg"
        outside.write_text("x")
        link = "a" * 32 + ".jpg"
        (root / "cam1" / link).symlink_to(outside)
        try:
            resolve_crop_path(str(root), "cam1", link)
        except ValueError:
            pass
        else:
            raise AssertionError("a symlink escaping the crop root must be refused")


def test_removing_a_camera_disables_it_and_keeps_its_history():
    """Dropping a camera from config must not delete what it recorded."""
    with tempfile.TemporaryDirectory() as tmp:
        conn = connect(str(Path(tmp) / "app.db"))
        migrate(conn)
        both = [{"stream_id": c, "display_name": c, "source_uri": "u",
                 "annotated_path": "a", "enabled": True, "sort_order": 0}
                for c in ("cam1", "cam2")]
        sync_streams(conn, both)
        conn.execute(
            """INSERT INTO tracks (stream_id, tracking_session_id, history_generation,
                 tracker_id, class_label, first_observed_utc, last_observed_utc,
                 confidence_freshness) VALUES ('cam2','s','g',1,'person','t','t','f')""")
        conn.commit()

        sync_streams(conn, both[:1])
        row = conn.execute("SELECT enabled FROM streams WHERE stream_id='cam2'").fetchone()
        assert row is not None, "a removed camera's row must not be deleted"
        assert row["enabled"] == 0, "a removed camera must be disabled"
        assert conn.execute(
            "SELECT count(*) n FROM tracks WHERE stream_id='cam2'").fetchone()["n"] == 1, (
            "removing a camera from config must never destroy its history")
        conn.close()


class FakeDlsps:
    """Minimal DLSPS stand-in: records deletes, and lets a start be doomed."""

    def __init__(self, doomed=False, queued_polls=0, status_failures=0,
                 list_failures=0):
        self.instances = {}
        self.deleted = []
        self.starts = []
        self.requests = []
        self.events = []
        self.doomed = doomed
        self.queued_polls = queued_polls
        self.status_failures = status_failures
        self.list_failures = list_failures
        self.remaining_polls = {}
        self.counter = 0

    def get(self, url, timeout=None):
        if url.endswith("/pipelines/status"):
            if self.list_failures:
                self.list_failures -= 1
                raise httpx.ReadError("temporary list failure")
            body = [{"id": i, "state": st} for i, st in self.instances.items()]
        elif "/pipelines/" in url and url.endswith("/status"):
            instance = url.rsplit("/", 2)[-2]
            if self.status_failures:
                self.status_failures -= 1
                self.instances[instance] = "RUNNING"
                raise httpx.ReadError("temporary status failure")
            state = self.instances[instance]
            self.events.append(f"status:{instance}:{state}")
            remaining = self.remaining_polls.get(instance, 0)
            if remaining:
                self.remaining_polls[instance] = remaining - 1
                if remaining == 1:
                    self.instances[instance] = "RUNNING"
            body = {"state": state}
        elif url.endswith("/v3/paths/list"):
            body = {"items": [{"name": f"cam{i}", "ready": True} for i in range(1, 4)]}
        else:  # MediaMTX session lists
            body = {"items": []}
        return types.SimpleNamespace(json=lambda: body, raise_for_status=lambda: None)

    def post(self, url, json=None, timeout=None):
        if "/kick/" in url:
            return types.SimpleNamespace(json=lambda: {}, raise_for_status=lambda: None)
        self.counter += 1
        instance = f"i{self.counter}"
        self.starts.append(instance)
        self.requests.append(json)
        self.events.append(f"post:{instance}")
        # A doomed pipeline accepts the POST and *then* dies, which is the whole
        # point: the caller cannot tell from this reply that it failed.
        self.instances[instance] = (
            "ERROR" if self.doomed
            else "QUEUED" if self.queued_polls
            else "RUNNING"
        )
        self.remaining_polls[instance] = self.queued_polls
        return types.SimpleNamespace(json=lambda: instance, raise_for_status=lambda: None)

    def delete(self, url, timeout=None):
        instance = url.rsplit("/", 1)[-1]
        self.deleted.append(instance)
        self.instances.pop(instance, None)
        return types.SimpleNamespace(raise_for_status=lambda: None)


def _pipeline_fixture(tmp):
    from app import pipelines
    pipelines.shutdown.clear()
    pipelines._failures.clear()
    pipelines._retry_after.clear()
    pipelines._accepted_at.clear()
    pipelines._cleaned_terminal.clear()
    conn = connect(str(Path(tmp) / "app.db"))
    migrate(conn)
    sync_streams(conn, [{"stream_id": "cam1", "display_name": "cam1", "source_uri": "u",
                         "annotated_path": "annotated1", "enabled": True, "sort_order": 0}])
    cfg = {"services": {"dlsps": "http://d", "mediamtx": "http://m"},
           "crop_dir": tmp, "takeover_seconds": 1.0,
           "supervise_seconds": 10.0, "pipeline_start_timeout_seconds": 1.0,
           "pipeline_start_poll_seconds": 0.0, "feed_stale_seconds": 90.0}
    return pipelines, conn, cfg


def test_shutdown_sweeps_orphans_and_preserves_unverified_ownership():
    with tempfile.TemporaryDirectory() as tmp:
        pipelines, conn, cfg = _pipeline_fixture(tmp)
        client = FakeDlsps()
        pipelines.reconcile(conn, cfg, client)
        client.instances["orphan"] = "RECONNECTING"
        pipelines.shutdown.set()
        assert not pipelines.reconcile(conn, cfg, client)
        pipelines.stop_owned(conn, cfg, client)
        assert set(client.deleted) == {"i1", "orphan"}
        assert conn.execute("SELECT dlsps_instance_id FROM streams").fetchone()[0] is None
        conn.execute("UPDATE streams SET dlsps_instance_id = 'uncertain'")
        conn.commit()
        client.list_failures = 2
        try:
            pipelines.stop_owned(conn, cfg, client)
            assert False, "unverified stop must fail"
        except RuntimeError:
            pass
        assert conn.execute("SELECT dlsps_instance_id FROM streams").fetchone()[0] == "uncertain"
        pipelines.shutdown.clear()
        conn.close()


def test_absent_source_is_not_started():
    with tempfile.TemporaryDirectory() as tmp:
        pipelines, conn, cfg = _pipeline_fixture(tmp)
        client = FakeDlsps()
        original = client.get
        def get(url, timeout=None):
            if url.endswith("/v3/paths/list"):
                return types.SimpleNamespace(json=lambda: {"items": []}, raise_for_status=lambda: None)
            return original(url, timeout)
        client.get = get
        assert not pipelines.reconcile(conn, cfg, client)
        assert not client.starts
        conn.close()


def test_a_retired_pipeline_is_stopped_not_merely_abandoned():
    """BACKOFF_WAIT is a live instance: it keeps retrying its source forever."""
    with tempfile.TemporaryDirectory() as tmp:
        pipelines, conn, cfg = _pipeline_fixture(tmp)
        client = FakeDlsps()
        # An instance no enabled camera owns, still actively reconnecting.
        client.instances["orphan"] = "BACKOFF_WAIT"
        pipelines.reconcile(conn, cfg, client)
        assert "orphan" in client.deleted, (
            "a BACKOFF_WAIT orphan still holds its source and model instance")

        # Steady state: the camera is RUNNING, so nothing further is started.
        before = len(client.starts)
        pipelines.reconcile(conn, cfg, client)
        assert len(client.starts) == before, "a RUNNING camera must not be restarted"
        assert client.deleted.count("orphan") == 1, "deletes must not be re-issued every pass"


def test_a_doomed_pipeline_backs_off_instead_of_spawning_replacements():
    """A start can return 200 and then die; without backoff that loops forever."""
    with tempfile.TemporaryDirectory() as tmp:
        pipelines, conn, cfg = _pipeline_fixture(tmp)
        client = FakeDlsps(doomed=True)
        for _ in range(5):
            pipelines.reconcile(conn, cfg, client)
        assert len(client.starts) == 1, (
            f"one failure must not spawn {len(client.starts)} instances; back off instead")
        assert client.starts[0] not in client.instances or (
            client.instances[client.starts[0]] == "ERROR"), (
            "a terminal instance is DLSPS's to keep; we must not spam deletes at it")

        # An operator action clears the backoff and retries at once.
        pipelines.forget_backoff("cam1")
        client.doomed = False
        pipelines.reconcile(conn, cfg, client)
        assert len(client.starts) == 2, "forget_backoff must allow an immediate retry"
        conn.close()


def test_terminal_pipeline_publishers_are_cleaned_once_and_retried_on_failure():
    with tempfile.TemporaryDirectory() as tmp:
        pipelines, conn, cfg = _pipeline_fixture(tmp)
        client = FakeDlsps()
        client.instances.update(done="COMPLETED", failed="ERROR", aborted="ABORTED")
        def delete(url, timeout=None):
            instance = url.rsplit("/", 1)[-1]
            client.deleted.append(instance)
            if instance == "failed" and client.deleted.count(instance) == 1:
                raise httpx.ConnectError("temporary failure")
            # Stock service retains terminal status after DELETE.
            return types.SimpleNamespace(raise_for_status=lambda: None)
        client.delete = delete
        for _ in range(3):
            pipelines.reconcile(conn, cfg, client)
        assert client.deleted.count("done") == client.deleted.count("aborted") == 1
        assert client.deleted.count("failed") == 2
        pipelines.stop_owned(conn, cfg, FakeDlsps())
        conn.close()


def test_camera_starts_are_serialized_until_previous_is_running():
    """DLSPS parameter substitution races if another POST arrives during construction."""
    with tempfile.TemporaryDirectory() as tmp:
        pipelines, conn, cfg = _pipeline_fixture(tmp)
        sync_streams(conn, [
            {"stream_id": "cam1", "display_name": "cam1", "source_uri": "u1",
             "annotated_path": "annotated1", "enabled": True, "sort_order": 0},
            {"stream_id": "cam2", "display_name": "cam2", "source_uri": "u2",
             "annotated_path": "annotated2", "enabled": True, "sort_order": 1},
        ])
        client = FakeDlsps(queued_polls=1)
        started = pipelines.reconcile(conn, cfg, client)
        assert started == {"cam1", "cam2"}, started
        assert client.events.index("status:i1:RUNNING") < client.events.index("post:i2"), (
            "cam2 POST arrived before cam1 finished consuming its parameters")
        for index, request in enumerate(client.requests, 1):
            frame = request["destination"]["frame"]
            assert frame["type"] == "webrtc"
            assert frame["peer-id"] == f"annotated{index}"
            assert "annotated_whip" not in request["parameters"]
        conn.close()


def test_pipeline_ownership_writes_use_the_single_writer():
    """Pipeline lifecycle may read separately, but must not become writer two."""
    with tempfile.TemporaryDirectory() as tmp:
        pipelines, writer_conn, cfg = _pipeline_fixture(tmp)
        db_path = writer_conn.execute("PRAGMA database_list").fetchone()["file"]
        reader = connect(db_path)
        reader.execute("PRAGMA query_only=ON")
        writes = []

        def write(operation):
            writes.append(True)
            result = operation(writer_conn)
            writer_conn.commit()
            return result

        started = pipelines.reconcile(
            reader, cfg, FakeDlsps(), writer=write)
        assert started == {"cam1"}, started
        assert writes, "pipeline ownership was not routed through the single writer"
        row = writer_conn.execute(
            "SELECT dlsps_instance_id FROM streams WHERE stream_id='cam1'").fetchone()
        assert row["dlsps_instance_id"] == "i1", dict(row)
        reader.close()
        writer_conn.close()


def test_concurrent_reconciles_share_one_start_sequence():
    """Supervisor and reset must not interleave per-camera parameter substitution."""
    with tempfile.TemporaryDirectory() as tmp:
        pipelines, conn, cfg = _pipeline_fixture(tmp)
        sync_streams(conn, [
            {"stream_id": "cam1", "display_name": "cam1", "source_uri": "u1",
             "annotated_path": "annotated1", "enabled": True, "sort_order": 0},
            {"stream_id": "cam2", "display_name": "cam2", "source_uri": "u2",
             "annotated_path": "annotated2", "enabled": True, "sort_order": 1},
        ])
        client = FakeDlsps(queued_polls=1)
        gate = threading.Barrier(3)
        errors = []

        def run_reconcile():
            try:
                gate.wait()
                pipelines.reconcile(conn, cfg, client)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run_reconcile) for _ in range(2)]
        for thread in threads:
            thread.start()
        gate.wait()
        for thread in threads:
            thread.join()
        assert not errors, errors
        assert client.starts == ["i1", "i2"], client.starts
        conn.close()


def test_status_poll_failure_preserves_recent_healthy_start():
    """Accepted instance gets feed grace even if its first status request fails."""
    with tempfile.TemporaryDirectory() as tmp:
        pipelines, conn, cfg = _pipeline_fixture(tmp)
        cfg["feed_stale_seconds"] = 90.0
        client = FakeDlsps(status_failures=1)
        assert pipelines.reconcile(conn, cfg, client) == set()
        assert client.starts == ["i1"]
        pipelines.reconcile(conn, cfg, client, frozenset({"cam1"}))
        assert client.starts == ["i1"], "recent accepted pipeline was replaced"
        assert client.deleted == [], "recent accepted pipeline was deleted"
        conn.close()


def test_startup_adopts_persisted_active_pipeline_with_fresh_grace():
    """Crash recovery must not delete a retained QUEUED instance on first supervision."""
    with tempfile.TemporaryDirectory() as tmp:
        pipelines, conn, cfg = _pipeline_fixture(tmp)
        conn.execute(
            "UPDATE streams SET dlsps_instance_id = 'old' WHERE stream_id = 'cam1'"
        )
        conn.commit()
        client = FakeDlsps()
        client.instances["old"] = "QUEUED"
        adopted = pipelines.reconcile(conn, cfg, client, include_kept=True)
        assert adopted == {"cam1"}, adopted
        assert "cam1" in pipelines._accepted_at
        assert client.starts == []
        assert client.deleted == []
        conn.close()


def test_startup_status_failure_still_grants_persisted_instance_grace():
    """One failed aggregate listing must not erase crash-recovery ownership."""
    with tempfile.TemporaryDirectory() as tmp:
        pipelines, conn, cfg = _pipeline_fixture(tmp)
        conn.execute(
            "UPDATE streams SET dlsps_instance_id = 'old' WHERE stream_id = 'cam1'"
        )
        conn.commit()
        client = FakeDlsps(list_failures=1)
        client.instances["old"] = "QUEUED"
        adopted = pipelines.reconcile(conn, cfg, client, include_kept=True)
        assert adopted == {"cam1"}, adopted
        pipelines.reconcile(conn, cfg, client, frozenset({"cam1"}))
        assert client.starts == []
        assert client.deleted == []
        conn.close()


def test_a_crop_that_never_finds_a_track_is_reaped():
    """Parked crops are stored track-less; without a reap they accumulate forever."""
    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp, cameras=("cam1",))
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(time.time() - ing.pending_ttl * 4))
        fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for at in (old, fresh):
            ing.conn.execute(
                """INSERT INTO candidates (candidate_id, track_id, stream_id,
                     tracking_session_id, history_generation, tracker_id, observed_at_utc,
                     storage_key, image_state, expires_at_utc)
                   VALUES (?, NULL, 'cam1', 's', 'g', 1, ?, 'k.jpg', 'present',
                           '2099-01-01T00:00:00Z')""",
                (f"c-{at}", at))
        ing.conn.commit()
        ing._reflect_expiry()
        ing.conn.commit()
        left = [r["observed_at_utc"] for r in ing.conn.execute(
            "SELECT observed_at_utc FROM candidates WHERE track_id IS NULL")]
        assert left == [fresh], (
            f"only the crop past the parking window may be reaped, kept {left}")
        ing.conn.close()


def test_model_placement_separates_configuration_runtime_and_availability():
    from app.adapters import Services
    from app.placement import Placement
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "chatqna.yaml").write_text(json.dumps({
            "model_settings": {"LLM_MODEL_ID": "Intel/neural-chat-7b-v3-3", "EMBEDDING_MODEL_ID": "embedding", "RERANKER_MODEL_ID": "reranker"},
            "device_settings": {"LLM_DEVICE": "GPU", "EMBEDDING_DEVICE": "CPU", "RERANKER_DEVICE": "CPU"}}))
        (root / "tts.yaml").write_text(json.dumps({"models": {"tts": {"name": "microsoft/speecht5_tts", "device": "GPU"}}}))
        (root / "audio-analyzer.yaml").write_text(json.dumps({"models": {"asr": {"name": "whisper-medium", "device": "GPU", "hf_token": "never-expose-this"}}}))
        (root / "dlsps.json").write_text(json.dumps({"config": {"pipelines": [{"name": "handheld_npu_detect",
            "pipeline": "vah264dec ! gvadetect model=/models/INT8/yolo11s.xml device=NPU ! gvatrack name=tracking ! appsink"}]}}))
        calls = []
        replies = {"/model": {"llm_model": "Intel/neural-chat-7b-v3-3"},
                   "/v1/model-info": {"model": "microsoft/speecht5_tts", "device": "GPU"},
                   "/health": {"status": "ok"}, "/pipelines/status": [{"id": "owned", "state": "RUNNING"}]}
        def handle(request):
            calls.append(request.url.path)
            body = replies[request.url.path]
            if body is None:
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(200, json=body)
        services = Services({name: "http://" + name for name in ("dlsps", "chatqna", "tts", "audio_analyzer")})
        services.client.close()
        services.client = httpx.Client(transport=httpx.MockTransport(handle))
        placement = Placement(services, {}, tmp)
        result = placement.snapshot({"cam1": "owned"}, {"cam1": {"ok": True}})
        rows = {m["id"]: m for m in result["models"]}
        assert rows["dlsps"]["status"] == "ready"
        assert rows["dlsps"]["runtime_device"] is None
        assert rows["dlsps"]["configured_device"] == "NPU"
        assert rows["tts"]["runtime_device"] == "GPU"
        assert rows["chatqna"]["runtime_device"] is None
        assert rows["chatqna"]["stages"][0]["device"] == "CPU"
        assert "never-expose-this" not in json.dumps(result)
        rows["tts"]["runtime_device"] = "corrupt-client-copy"
        again = placement.snapshot({"cam1": "unrelated"}, {"cam1": {"ok": True}})
        assert len(calls) == 4, "probes must be cached across clients"
        assert again["models"][0]["status"] == "unavailable", "unowned pipelines cannot establish readiness"
        assert again["models"][3]["runtime_device"] == "GPU", "caller mutation must not corrupt the cache"
        replies["/v1/model-info"] = None
        replies["/model"] = {"llm_model": "unexpected-model"}
        placement.expires = 0
        result = placement.snapshot({}, {})
        assert result["models"][1]["status"] == "mismatch"
        assert result["models"][3]["status"] == "unavailable"
        assert result["models"][3]["configured_device"] == "GPU"
        assert result["models"][3]["runtime_device"] is None
        replies["/pipelines/status"] = None
        placement.expires = 0
        result = placement.snapshot({"cam1": "owned"}, {"cam1": {"ok": True}})
        assert result["models"][0]["status"] == "unknown", "failed control-plane probes cannot claim a live pipeline stopped"
        assert "fresh observations" in result["models"][0]["note"]
        (root / "audio-analyzer.yaml").unlink()
        replies["/model"] = {"llm_model": {"invalid": True}}
        placement.expires = 0
        result = placement.snapshot({}, {})
        assert result["models"][1]["status"] == "unknown"
        assert result["models"][2]["configured_device"] is None
        services.close()


def _phase5_fixture(root: Path):
    from app.assistant import _utc

    db = root / "phase5.db"
    conn = connect(str(db))
    migrate(conn)
    conn.execute(
        """INSERT INTO streams (stream_id, display_name, source_uri, annotated_path,
                                enabled, sort_order, tracking_session_id, history_generation)
           VALUES ('cam1','Camera 1','rtsp://cam1','annotated1',1,0,'session-1','generation-1')"""
    )
    metadata = root / "metadata.yaml"
    metadata.write_text("names:\n  0: person\n  2: car\n  28: suitcase\n")
    cutoff = datetime.now(UTC).replace(microsecond=0)

    def track(track_id, label, first_minutes, last_minutes, intervals):
        conn.execute(
            """INSERT INTO tracks (
                 track_id, stream_id, tracking_session_id, history_generation, tracker_id,
                 class_label, first_observed_utc, last_observed_utc, observation_count,
                 peak_confidence, confidence_freshness, closed_at, closure_reason)
               VALUES (?, 'cam1', 'session-1', 'generation-1', ?, ?, ?, ?, 5, .9,
                       'tracker_metadata_unverified', ?, 'timeout')""",
            (track_id, track_id, label, _utc(cutoff - timedelta(minutes=first_minutes)),
             _utc(cutoff - timedelta(minutes=last_minutes)),
             _utc(cutoff - timedelta(minutes=last_minutes) + timedelta(seconds=15))),
        )
        for index, (start_minutes, end_minutes) in enumerate(intervals):
            conn.execute(
                """INSERT INTO track_intervals (
                     track_id, interval_index, stream_id, started_utc, ended_utc,
                     observation_count) VALUES (?, ?, 'cam1', ?, ?, 5)""",
                (track_id, index, _utc(cutoff - timedelta(minutes=start_minutes)),
                 _utc(cutoff - timedelta(minutes=end_minutes))),
            )

    track(1, "person", 12, 8, [(12, 8)])
    conn.execute("""INSERT INTO candidates(candidate_id, track_id, stream_id, tracking_session_id,
        history_generation, tracker_id, observed_at_utc, storage_key, expires_at_utc, image_state)
        VALUES ('crop1',1,'cam1','session-1','generation-1',1,'2026-09-21T00:00:00Z','fixture.jpg','2099-01-01T00:00:00Z','present')""")
    # Envelope overlaps the ten-minute window, but its only interval does not.
    track(2, "car", 30, 0, [(30, 25)])
    conn.executemany(
        """INSERT INTO feed_health (
             stream_id, started_utc, ended_utc, ok, reason, observed_frames,
             untracked_detections) VALUES ('cam1', ?, ?, ?, ?, 1, 0)""",
        [
            (_utc(cutoff - timedelta(minutes=20)), _utc(cutoff - timedelta(minutes=5)),
             1, None),
            (_utc(cutoff - timedelta(minutes=5)), _utc(cutoff - timedelta(minutes=3)),
             0, "no_signal"),
            (_utc(cutoff - timedelta(minutes=3)), None, 1, None),
        ],
    )
    conn.commit()
    return conn, cutoff, metadata


def test_phase5_plan_validation_retries_then_fails_safely():
    from app.assistant import Assistant, InvalidPlan, validate_route

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        conn, cutoff, metadata = _phase5_fixture(root)
        labels = ("person", "car", "suitcase")
        valid = {
            "intent": "video_history",
            "plan": {"window": {"kind": "relative", "minutes": 10},
                     "classes": ["person"], "page": 0, "page_size": 20},
        }
        route = validate_route(
            valid, labels=labels, known_streams=("cam1",), selected_stream="cam1",
            cutoff=cutoff, retention=timedelta(days=7))
        assert route.plan["streams"] == ["cam1"]
        for streams in (None, ["cam1", "cam2"]):
            widened = json.loads(json.dumps(valid))
            widened["plan"]["streams"] = streams
            try:
                validate_route(
                    widened, labels=labels, known_streams=("cam1", "cam2"),
                    selected_stream="cam1", cutoff=cutoff, retention=timedelta(days=7))
            except InvalidPlan:
                pass
            else:
                raise AssertionError("model widened the operator-selected camera scope")
        all_cameras = validate_route(
            {**valid, "plan": {**valid["plan"], "streams": None}},
            labels=labels, known_streams=("cam1", "cam2"), selected_stream=None,
            cutoff=cutoff, retention=timedelta(days=7))
        assert all_cameras.plan["streams"] == ["cam1", "cam2"]

        invalid = [
            {"intent": "video_history", "plan": {
                "window": {"kind": "relative", "minutes": 10}, "extra": True}},
            {"intent": "invented"},
            {"intent": "video_history", "plan": {
                "window": {"kind": "relative", "minutes": 10}, "classes": ["weapon"]}},
            {"intent": "video_history", "plan": {
                "window": {"kind": "relative", "minutes": 10}, "streams": ["cam9"]}},
            {"intent": "video_history", "plan": {
                "window": {"kind": "relative", "minutes": 0}}},
            {"intent": "video_history", "plan": {
                "window": {"kind": "relative", "minutes": 10}, "page_size": 51}},
        ]
        for payload in invalid:
            try:
                validate_route(
                    payload, labels=labels, known_streams=("cam1",),
                    selected_stream="cam1", cutoff=cutoff, retention=timedelta(days=7))
            except InvalidPlan:
                pass
            else:
                raise AssertionError(f"invalid plan accepted: {payload}")

        calls = []
        bad = json.dumps(invalid[0])
        assistant = Assistant(
            {"db_path": str(root / "phase5.db"),
             "detector_metadata_path": str(metadata),
             "history_retention_seconds": 604800},
            None, generate=lambda prompt: calls.append(prompt) or bad)
        answer = assistant.ask("Summarize the recorded evidence during the last ten minutes?", "cam1")
        assert answer["recovery"]["reason"] == "unmapped_conditions"
        assert len(calls) == 2, "invalid plan gets one retry, never an unbounded loop"
        conn.close()


def test_phase7_general_conversation_context_and_echo_adapter():
    from app.assistant import (
        CHAT_ANSWER_MARKER,
        CONTEXT_IDLE_SECONDS,
        Assistant,
        InvalidPlan,
        ServiceUnavailable,
        adapt_chat_response,
        decode_route_completion,
        validate_route,
    )

    echoed = "rendered prompt\n" + CHAT_ANSWER_MARKER + "General answer."
    assert adapt_chat_response(echoed) == "General answer."
    for invalid in ("Answer:\nunsafe echo", CHAT_ANSWER_MARKER):
        try:
            adapt_chat_response(invalid)
        except ServiceUnavailable:
            pass
        else:
            raise AssertionError("invalid stock response was exposed")
    assert decode_route_completion(
        '{"intent":"general_chat"}\nUnsolicited answer.'
    ) == {"intent": "general_chat"}
    assert decode_route_completion(
        '{"intent":"general_chat","response":"Unsolicited answer."}'
    ) == {"intent": "general_chat"}
    try:
        decode_route_completion(
            '{"intent":"video_history","plan":{}}\nUnsolicited answer.'
        )
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("malformed video route prefix was repaired")
    for invalid_route in (
        '{"intent":"mission_knowledge"}\nUnsolicited answer.',
        '{"intent":"app_control","response":"Unsolicited answer."}',
        '{"intent":"general_chat","response":"answer","extra":true}',
    ):
        try:
            route = decode_route_completion(invalid_route)
            validate_route(
                route,
                labels=("person",),
                known_streams=("cam1",),
                selected_stream="cam1",
                cutoff=datetime.now(UTC),
                retention=timedelta(hours=1),
            )
        except (json.JSONDecodeError, InvalidPlan):
            pass
        else:
            raise AssertionError(f"unsafe malformed route was accepted: {invalid_route}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        conn, _, metadata = _phase5_fixture(root)
        prompts = []
        def generate(prompt):
            if "Classify the operator request" in prompt:
                return '{"intent":"general_chat"}'
            prompts.append(prompt)
            return "General reply " + str(len(prompts))

        assistant = Assistant(
            {"db_path": str(root / "phase5.db"),
             "detector_metadata_path": str(metadata)},
            None, generate=generate)
        for index in range(6):
            answer = assistant.ask(f"General question {index}?")
            assert answer["intent"] == "general_chat" and not answer.get("query")
            assert answer["conversation"]["turns"] <= 4
            assert answer["conversation"]["characters"] <= 1200
        assert "General question 0?" not in prompts[-1]
        assert "General question 1?" in prompts[-1]
        assert "not mission material, camera" in prompts[-1]

        assistant.reset_context()
        assistant.ask("Fresh question?")
        assert "General question 5?" not in prompts[-1]

        assistant.ask("Idle context seed?")
        assistant._conversation_state().updated = time.monotonic() - CONTEXT_IDLE_SECONDS - 1
        assistant.ask("After idle?")
        assert "Idle context seed?" not in prompts[-1]

        assistant.ask("Before history?")
        assistant.search(
            {"window": {"kind": "relative", "minutes": 10}, "classes": None},
            "cam1",
        )
        assistant.ask("After history?")
        assert "Before history?" not in prompts[-1]

        assistant._remember_general("Long question", "x" * 5000)
        assert assistant.context_status()["characters"] <= 1200
        assert assistant.context_status()["turns"] <= 4

        generation_started, release_generation = threading.Event(), threading.Event()
        def blocked_generate(prompt):
            if "Classify the operator request" in prompt:
                return '{"intent":"general_chat"}'
            generation_started.set()
            assert release_generation.wait(1)
            return "Late reply"
        blocked = Assistant(
            {"db_path": str(root / "phase5.db"),
             "detector_metadata_path": str(metadata)},
            None, generate=blocked_generate)
        errors = []
        worker = threading.Thread(
            target=lambda: _capture(errors, blocked.ask, "Question before reset?"))
        worker.start()
        assert generation_started.wait(1)
        blocked.reset_context()
        release_generation.set()
        worker.join(1)
        assert not worker.is_alive() and not errors
        assert blocked.context_status()["turns"] == 0, "late completion repopulated reset context"
        conn.close()


def test_phase5_interval_query_aggregates_completeness_and_safe_grounding():
    from app.assistant import Assistant

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        conn, _, metadata = _phase5_fixture(root)
        responses = iter([
            json.dumps({
                "intent": "video_history",
                "plan": {"window": {"kind": "relative", "minutes": 10},
                         "classes": None, "attributes": [], "order": "last_observed_desc",
                         "page": 0, "page_size": 20},
            }),
        ])
        assistant = Assistant(
            {"db_path": str(root / "phase5.db"),
             "detector_metadata_path": str(metadata),
             "history_retention_seconds": 604800},
            None, generate=lambda _prompt: next(responses))
        answer = assistant.ask("What did camera one detect in the last ten minutes?", "cam1")
        query = answer["query"]
        assert query["total"] == 1 and query["class_counts"] == {"person": 1}, query
        assert [row["track_id"] for row in query["tracks"]] == [1], (
            "track envelope must not create a match without an overlapping interval")
        assert query["tracks"][0]["intervals"][0]["started_utc"] == query["window"]["started_utc"]
        gap = next(w for w in query["warnings"] if w["reason"] == "no_signal")
        assert gap["gap_seconds_total"] == 120.0, gap
        assert answer["answer_text"].startswith("Found 1 person track")
        assert query["with_crops_total"] == 1
        assert not any(w["code"].startswith("language_") for w in query["warnings"])
        # No second generation: the single routing fixture is now exhausted.
        assert next(responses, None) is None
        conn.execute("UPDATE candidates SET image_state='expired'")
        conn.commit()
        from app.assistant import query_history, validate_plan
        cutoff = datetime.now(UTC).replace(microsecond=0)
        plan = validate_plan({"window": {"kind": "relative", "minutes": 10}},
            labels=("person", "car"), known_streams=("cam1",), selected_stream="cam1",
            cutoff=cutoff, retention=timedelta(days=7))
        without = query_history(str(root / "phase5.db"), plan, cutoff, 2,
            {"cam1": {"tracking_session_id": "session-1", "history_generation": "generation-1"}})
        assert without["total"] == 1 and without["with_crops_total"] == 0
        assert without["tracks"] == [] and not without["truncated"]
        conn.close()


def test_phase5_readonly_budget_and_stable_paging():
    from app.assistant import (
        InvalidInput,
        QueryBudgetExceeded,
        decode_page_token,
        encode_page_token,
        guarded_query,
        query_history,
        readonly_connection,
        validate_plan,
    )

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        conn, cutoff, _ = _phase5_fixture(root)
        from app.assistant import _utc
        for track_id, last_seconds in ((3, 60), (4, 120)):
            conn.execute(
                """INSERT INTO tracks (
                     track_id, stream_id, tracking_session_id, history_generation, tracker_id,
                     class_label, first_observed_utc, last_observed_utc, observation_count,
                     peak_confidence, confidence_freshness, closed_at, closure_reason)
                   VALUES (?, 'cam1','session-1','generation-1',?,'person',?,?,1,.8,
                           'tracker_metadata_unverified',?,'timeout')""",
                (track_id, track_id, _utc(cutoff - timedelta(seconds=last_seconds + 10)),
                 _utc(cutoff - timedelta(seconds=last_seconds)),
                 _utc(cutoff - timedelta(seconds=last_seconds - 15))),
            )
            conn.execute(
                """INSERT INTO track_intervals (
                     track_id, interval_index, stream_id, started_utc, ended_utc,
                     observation_count) VALUES (?,0,'cam1',?,?,1)""",
                (track_id, _utc(cutoff - timedelta(seconds=last_seconds + 10)),
                 _utc(cutoff - timedelta(seconds=last_seconds))),
            )
        conn.commit()
        plan = validate_plan(
            {"window": {"kind": "relative", "minutes": 10}, "classes": ["person"],
             "streams": ["cam1"], "order": "last_observed_desc", "page": 0,
             "page_size": 1},
            labels=("person", "car", "suitcase"), known_streams=("cam1",),
            selected_stream=None, cutoff=cutoff, retention=timedelta(days=7))
        for tid in (3, 4):
            conn.execute("""INSERT INTO candidates(candidate_id, track_id, stream_id, tracking_session_id,
                history_generation, tracker_id, observed_at_utc, storage_key, expires_at_utc, image_state)
                VALUES (?,?,'cam1','session-1','generation-1',?,'2026-09-21T00:00:00Z','fixture.jpg','2099-01-01T00:00:00Z','present')""",
                (f"crop{tid}", tid, tid))
        conn.commit()
        ceiling = 4
        scope = {"cam1": {
            "tracking_session_id": "session-1",
            "history_generation": "generation-1",
        }}
        first = query_history(str(root / "phase5.db"), plan, cutoff, ceiling, scope)
        assert first["tracks"][0]["track_id"] == 3 and first["total"] == 3
        # Expiry on an earlier page must not shift later tracks past OFFSET.
        conn.execute("UPDATE candidates SET image_state='expired' WHERE track_id=3")
        conn.commit()
        expired = query_history(str(root / "phase5.db"), plan, cutoff, ceiling, scope)
        assert expired["tracks"][0]["track_id"] == 4 and expired["truncated"]
        secret = b"phase5-test-secret"
        token = encode_page_token(plan, cutoff, ceiling, scope, secret, cursor=first["next_cursor"])
        try:
            decode_page_token(token[:-1] + ("A" if token[-1] != "A" else "B"), secret)
        except InvalidInput:
            pass
        else:
            raise AssertionError("tampered page token was accepted")

        # A new, newer matching row arrives between pages. The token's cutoff and
        # track-id ceiling keep page two tied to the original selection.
        conn.execute(
            """INSERT INTO tracks (
                 track_id, stream_id, tracking_session_id, history_generation, tracker_id,
                 class_label, first_observed_utc, last_observed_utc, observation_count,
                 peak_confidence, confidence_freshness)
               VALUES (5,'cam1','session-1','generation-1',5,'person',?,?,1,.9,
                       'tracker_metadata_unverified')""",
            (_utc(cutoff - timedelta(seconds=20)), _utc(cutoff - timedelta(seconds=10))),
        )
        conn.execute(
            """INSERT INTO track_intervals (
                 track_id, interval_index, stream_id, started_utc, ended_utc,
                 observation_count) VALUES (5,0,'cam1',?,?,1)""",
            (_utc(cutoff - timedelta(seconds=20)), _utc(cutoff - timedelta(seconds=10))),
        )
        conn.execute(
            """UPDATE streams
               SET tracking_session_id='session-2', history_generation='generation-2'
               WHERE stream_id='cam1'"""
        )
        conn.commit()
        body = decode_page_token(token, secret)
        second_plan = validate_plan(
            body["plan"], labels=("person", "car", "suitcase"),
            known_streams=("cam1",), selected_stream=None, cutoff=cutoff,
            retention=timedelta(days=7))
        second = query_history(
            str(root / "phase5.db"), second_plan, cutoff, body["track_id_ceiling"],
            body["scope"], body["cursor"])
        assert second["tracks"][0]["track_id"] == 4, second["tracks"]

        assert second["truncated"]
        # A crop arriving on an earlier page must not duplicate later tracks.
        conn.execute("UPDATE candidates SET image_state='present' WHERE track_id=3")
        conn.commit()
        third = query_history(str(root / "phase5.db"), {**second_plan, "page": 2},
                              cutoff, ceiling, scope, second["next_cursor"])
        assert [row["track_id"] for row in third["tracks"]] == [1]
        assert not third["truncated"]
        previous = query_history(str(root / "phase5.db"), second_plan, cutoff,
                                 ceiling, scope, third["previous_cursor"])
        assert [row["track_id"] for row in previous["tracks"]] == [4]
        # An upcoming crop expires: seek skips it and still fills the next page.
        conn.execute("UPDATE candidates SET image_state='expired' WHERE track_id=4")
        conn.commit()
        skipped = query_history(str(root / "phase5.db"), second_plan, cutoff,
                                ceiling, scope, first["next_cursor"])
        assert [row["track_id"] for row in skipped["tracks"]] == [1]
        conn.execute("UPDATE candidates SET image_state='present'")
        conn.commit()
        ascending_plan = {**plan, "order": "last_observed_asc"}
        ascending = query_history(str(root / "phase5.db"), ascending_plan, cutoff, ceiling, scope)
        assert ascending["tracks"][0]["track_id"] == 1
        conn.execute("UPDATE candidates SET image_state='expired' WHERE track_id=1")
        conn.commit()
        ascending_next = query_history(str(root / "phase5.db"), {**ascending_plan, "page": 1},
                                       cutoff, ceiling, scope, ascending["next_cursor"])
        assert ascending_next["tracks"][0]["track_id"] == 4

        # Signed paging for a selected camera remains valid when other cameras
        # are registered; page() reconstructs the original selected scope.
        selected_page = validate_plan(
            body["plan"], labels=("person", "car", "suitcase"),
            known_streams=("cam1", "cam2"), selected_stream="cam1", cutoff=cutoff,
            retention=timedelta(days=7))
        assert selected_page["streams"] == ["cam1"]

        with readonly_connection(str(root / "phase5.db")) as read:
            try:
                read.execute("DELETE FROM tracks")
            except sqlite3.OperationalError:
                pass
            else:
                raise AssertionError("read-only query connection accepted a write")
            try:
                guarded_query(read, """
                    WITH RECURSIVE n(x) AS (
                      VALUES(0) UNION ALL SELECT x + 1 FROM n WHERE x < 100000000
                    ) SELECT sum(x) AS total FROM n
                """)
            except QueryBudgetExceeded:
                pass
            else:
                raise AssertionError("expensive query was not interrupted")
        conn.close()




def test_handheld_search_fast_path_and_recovery():
    from app.assistant import Assistant, simple_plan, InvalidInput, encode_page_token, query_context, validate_plan
    labels = ("person", "car")
    for text, classes in [
        ("Give me detected objects in the last ten minutes", None),
        ("Show me people from 16:20 to 16:25", ["person"]),
        ("Which cars appeared in the past five minutes?", ["car"]),
        ("What was detected in the last 10 minutes?", None),
        ("How many people were detected in the last 10 minutes?", ["person"]),
    ]:
        window = {"kind": "relative", "minutes": 10} if "16:20" in text else None
        assert simple_plan(text, labels, window)["classes"] == classes
    assert simple_plan("show cars from 16:20 to 16:25 near the gate", labels,
                       {"kind":"relative", "minutes":10}) is None
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        conn, cutoff, metadata = _phase5_fixture(root)
        a = Assistant({"db_path":str(root / "phase5.db"), "detector_metadata_path":str(metadata),
                       "assistant_queue_depth":1}, None,
                      generate=lambda _: json.dumps({"intent": "video_history", "plan": {
                          "window": {"kind": "relative", "minutes": 10}, "classes": None, "attributes": []}}))
        a.queue.acquire()
        with a.single_flight:
            answer = a.ask("Show me people in the last ten minutes", "cam1")
            assert answer["query"]["scope"] == "current_tracking_session"
            assert answer["query"]["filters"]["classes"] == ["person"]
            assert a.search({"window":{"kind":"relative", "minutes":10}, "classes":None}, "cam1")["query"]
        a.queue.release()
        assert a.ask("What was seen?", "cam1")["recovery"]["reason"] == "missing_window"
        a.generate = lambda _: '{"intent":"clarification"}'
        assert a.ask("Were there weapons in the last ten minutes?", "cam1")["recovery"]["reason"] == "unmapped_conditions"
        assert a.search({"window":{"kind":"relative", "minutes":10}, "classes":["invented"]}, "cam1")["recovery"]["reason"] == "invalid_filters"
        known, ceiling, scope = query_context(str(root / "phase5.db"))
        plan = validate_plan({"window":{"kind":"relative", "minutes":10}, "classes":None},
            labels=labels, known_streams=known, selected_stream="cam1", cutoff=cutoff, retention=timedelta(days=7))
        token = encode_page_token(plan, cutoff, ceiling, scope, a.page_secret)
        page = a.page(token)["query"]
        previous = a.page(page["previous_page_token"])["query"]
        assert previous["page"] == 0 and previous["query_cutoff_utc"] == page["query_cutoff_utc"]
        conn.execute("UPDATE streams SET tracking_session_id='new-session'")
        conn.commit()
        try:
            a.page(token)
        except InvalidInput as exc:
            assert "session changed" in str(exc)
        else:
            raise AssertionError("old session token accepted")
        conn.close()


def test_search_options_current_session_availability():
    from app.assistant import Assistant, _utc
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        conn, cutoff, metadata = _phase5_fixture(root)
        assistant = Assistant({"db_path": str(root / "phase5.db"),
                               "detector_metadata_path": str(metadata)}, None)
        options = assistant.options()
        assert options["classes"] == ("person", "car", "suitcase")
        assert options["availability"] == [{"stream_id": "cam1",
            "started_utc": _utc(cutoff - timedelta(minutes=30)),
            "ended_utc": _utc(cutoff - timedelta(minutes=8))}]
        conn.execute("UPDATE tracks SET history_generation='old' WHERE track_id=2")
        conn.commit()
        assert assistant.options()["availability"][0]["started_utc"] == _utc(cutoff - timedelta(minutes=12))
        assistant.retention = timedelta(minutes=10)
        assert assistant.options()["availability"][0]["started_utc"] >= _utc(cutoff - timedelta(minutes=10))
        conn.execute("UPDATE streams SET tracking_session_id='new-session'")
        conn.commit()
        assert assistant.options()["availability"] == [{"stream_id": "cam1", "started_utc": None, "ended_utc": None}]
        conn.close()


def test_history_ux_regressions():
    import asyncio
    from types import SimpleNamespace
    from app.main import create_app
    from app.assistant import Assistant, _history_answer
    truth = {"total": 7, "class_counts": {"person": 7}, "warnings": []}
    assert _history_answer(truth) == "Found 7 person tracks in the requested window."
    a = Assistant({}, None, generate=lambda _: (_ for _ in ()).throw(AssertionError("unneeded model call")))
    assert a.ask("What was seen?", "cam1")["intent"] == "clarification"

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        conn, _, metadata = _phase5_fixture(root)
        broad = json.dumps({"intent": "video_history", "plan": {
            "window": {"kind": "relative", "minutes": 10}, "classes": None, "attributes": []}})
        calls = []
        a = Assistant({"db_path": str(root / "phase5.db"), "detector_metadata_path": str(metadata)},
                      None, generate=lambda prompt: calls.append(prompt) or broad)
        assert a.ask(
            "What did camera 2 see in the last ten minutes?", "cam1"
        )["recovery"]["reason"] == "camera_mismatch"
        calls.clear()
        answer = a.ask("Summarize evidence concerning person tracks in the last ten minutes?", "cam1")
        assert not answer.get("query") and len(calls) == 2
        conn.close()

    async def responsive():
        app = create_app()
        def slow(text, stream, emit=None):
            if emit:
                emit({"answer": {"answer_text": "early counts"}})
            time.sleep(.25)
            return {"answer_text": "finished"}
        app.state.assistant = SimpleNamespace(ask=slow, conversation=lambda _: nullcontext())
        ticks = []
        async def heartbeat():
            for _ in range(25):
                ticks.append(time.monotonic())
                await asyncio.sleep(.02)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            beat = asyncio.create_task(heartbeat())
            response = await client.post("/api/ask", json={"text": "test"})
            await beat
            assert response.status_code == 200
            assert max(b-a for a,b in zip(ticks,ticks[1:])) < .15, "inference blocked event loop"
            response = await client.post("/api/ask", json={"text": "test"}, headers={"accept": "application/x-ndjson"})
            events = [json.loads(line) for line in response.text.splitlines()]
            assert events[0]["answer"]["answer_text"] == "early counts"
            assert events[-1]["done"] and events[-1]["answer"]["answer_text"] == "finished"
    asyncio.run(responsive())

    # A live span is checkpointed without being closed or losing its index.
    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp, cameras=("cam1",))
        span = {"last_at": time.time(), "index": 2}
        ing.open_intervals[("cam1", "s", "g", 1)] = span
        written = []
        ing._write_interval = lambda key, value: written.append((key, dict(value)))
        ing._flush_intervals()
        assert len(written) == 1 and ing.open_intervals
        assert written[0][1]["index"] == 2
        ing.conn.close()



def test_clock_time_history_question():
    from app.assistant import clock_window, InvalidInput, Assistant
    now = datetime(2026, 9, 21, 14, 30, tzinfo=UTC)
    question = "give me detected objects from 16:20 to 16:25"
    expected = {"kind": "absolute", "start_utc": "2026-09-21T14:20:00Z", "end_utc": "2026-09-21T14:25:00Z"}
    assert clock_window(question, "Europe/Warsaw", now) == expected
    assert clock_window(question + " yesterday", "Europe/Warsaw", now)["start_utc"] == "2026-09-20T14:20:00Z"
    assert clock_window(question + " on 2026-09-19", "Europe/Warsaw", now)["start_utc"] == "2026-09-19T14:20:00Z"
    for text, tz in [(question, None), (question, "invalid/timezone"),
                     ("objects from 26:20 to 26:25", "Europe/Warsaw"),
                     ("objects from 16:25 to 16:20", "Europe/Warsaw"),
                     ("objects from 17:20 to 17:25", "Europe/Warsaw"),
                     ("objects from 02:20 to 02:25 on 2026-03-29", "Europe/Warsaw"),
                     ("objects from 02:20 to 02:25 on 2025-10-26", "Europe/Warsaw")]:
        try:
            clock_window(text, tz, now)
        except InvalidInput:
            pass
        else:
            raise AssertionError((text,tz))
    for clocks in ("4:20 to 4:25 pm", "4:20pm to 4:25pm",
                   "4:20 PM to 4:25", "4:20 a.m. to 4:25 p.m."):
        text = f"show cars from {clocks} yesterday"
        def unexpected_generation(prompt):
            raise AssertionError("AM/PM must be clarified before model routing")
        result = Assistant({}, None, generate=unexpected_generation).ask(
            text, timezone="Europe/Warsaw")
        assert result["intent"] == "clarification", result
        assert "24-hour" in result["answer_text"], result
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        conn, cutoff, metadata = _phase5_fixture(root)
        from zoneinfo import ZoneInfo
        start = (cutoff-timedelta(minutes=10)).astimezone(ZoneInfo("Europe/Warsaw")).strftime("%H:%M")
        end = (cutoff-timedelta(minutes=5)).astimezone(ZoneInfo("Europe/Warsaw")).strftime("%H:%M")
        text = f"give me detected objects from {start} to {end}"
        window = clock_window(text, "Europe/Warsaw", cutoff)
        prompts = []
        def generate(prompt):
            prompts.append(prompt)
            return json.dumps({"intent":"video_history","plan":{"window":window,"classes":None}})
        a = Assistant({"db_path":str(root/'phase5.db'), "detector_metadata_path":str(metadata)}, None, generate=generate)
        result = a.ask(text, "cam1", timezone="Europe/Warsaw")
        assert result["intent"] == "video_history" and not prompts
        assert result["query"]["window"]["started_utc"] == window["start_utc"]
        conn.close()


def test_mission_lifecycle_retains_reconciles_and_discloses_citation_limits():
    """Phase 6 keeps originals outside stock in-memory FAISS and never fakes citations."""
    from app.mission import (
        BRIEF_FIELDS,
        MissionService,
        MissionValidationError,
        validate_upload,
    )

    try:
        validate_upload("not-a-pdf.pdf", b"not actually a PDF")
    except MissionValidationError:
        pass
    else:
        raise AssertionError("invalid PDF signature was accepted")
    try:
        validate_upload("../escape.pdf", b"%PDF-1.7")
    except MissionValidationError:
        pass
    else:
        raise AssertionError("path-like filename was accepted")

    class StockDocuments:
        def __init__(self):
            self.documents = set()

        def handler(self, request):
            if request.url.path == "/documents" and request.method == "GET":
                return httpx.Response(200, json={"metadata": {"documents": sorted(self.documents)}})
            if request.url.path == "/documents" and request.method == "POST":
                marker = b'filename="'
                name = request.content.split(marker, 1)[1].split(b'"', 1)[0].decode()
                self.documents.add(name)
                return httpx.Response(200, json={"status": "Success"})
            if request.url.path == "/documents" and request.method == "DELETE":
                params = dict(request.url.params)
                if params.get("delete_all") == "true":
                    self.documents.clear()
                else:
                    self.documents.discard(params["document"])
                return httpx.Response(204)
            raise AssertionError(f"unexpected stock request {request.method} {request.url}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = root / "app.db"
        conn = connect(str(db))
        migrate(conn)
        conn.close()
        stock = StockDocuments()
        services = types.SimpleNamespace(
            urls={"chatqna": "http://chatqna"},
            client=httpx.Client(transport=httpx.MockTransport(stock.handler)),
        )
        brief = json.dumps({field: "Not specified" for field in BRIEF_FIELDS})
        service = MissionService(
            {"db_path": str(db), "mission_sources_dir": str(root / "mission_sources"),
             "mission_ingest_timeout_seconds": 1, "mission_ingest_poll_seconds": 0},
            services, generate=lambda prompt: brief if "exact keys" in prompt
            else "That information is not available in the mission material.")
        result = service.upload("mission.pdf", _pdf_bytes())
        source = result["sources"][0]
        assert source["filename"] == "mission.pdf" and source["status"] == "ready"
        assert source["in_corpus"] and "mission.pdf" in stock.documents
        assert (root / "mission_sources" / source["sha256"]).is_file()
        assert result["answer_citations_available"] is False
        assert "inventory" in result["citation_notice"]
        service._generate_brief()
        generated = service.brief()
        assert generated["brief"] == {field: "Not specified" for field in BRIEF_FIELDS}
        answer = service.answer("Who leads the mission?")
        assert answer["intent"] == "mission_knowledge"
        assert answer["answer_citations_available"] is False

        # An empty inventory is the source-verified indication that Chat Q&A
        # restarted and lost its in-memory FAISS state.
        stock.documents.clear()
        service._reconcile()
        assert stock.documents == {"mission.pdf"}
        recovered = service.status()
        assert recovered["mode"] == "ready"
        assert recovered["corpus_version"] > source["corpus_version"]

        deleted = service.delete("mission.pdf", "mission.pdf")
        assert not deleted["sources"] and not stock.documents
        assert not (root / "mission_sources" / source["sha256"]).exists()
        deadline = time.monotonic() + 1
        while service._brief_running and time.monotonic() < deadline:
            time.sleep(.01)
        assert not service._brief_running
        services.client.close()


def test_mission_phase6_failure_matrix():
    """Regression coverage for lifecycle serialization, corpus truth and bounded uploads."""
    from app.assistant import Assistant, ServiceUnavailable
    from app.mission import (
        BRIEF_FIELDS,
        MAX_DOCX_ENTRIES,
        MAX_PDF_PAGES,
        MissionService,
        MissionUnavailable,
        MissionValidationError,
        validate_upload,
    )

    class StockDocuments:
        def __init__(self):
            self.documents = set()
            self.post_started = threading.Event()
            self.release_post = threading.Event()
            self.block_posts = False

        def handler(self, request):
            if request.url.path == "/documents" and request.method == "GET":
                return httpx.Response(200, json={"metadata": {"documents": sorted(self.documents)}})
            if request.url.path == "/documents" and request.method == "POST":
                self.post_started.set()
                if self.block_posts:
                    assert self.release_post.wait(2), "test upload was never released"
                name = request.content.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
                self.documents.add(name)
                return httpx.Response(200, json={"status": "Success"})
            if request.url.path == "/documents" and request.method == "DELETE":
                params = dict(request.url.params)
                if params.get("delete_all") == "true":
                    self.documents.clear()
                else:
                    self.documents.discard(params["document"])
                return httpx.Response(204)
            raise AssertionError(f"unexpected stock request {request.method} {request.url}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = root / "app.db"
        conn = connect(str(db))
        migrate(conn)
        conn.close()
        stock = StockDocuments()
        services = types.SimpleNamespace(
            urls={"chatqna": "http://chatqna"},
            client=httpx.Client(transport=httpx.MockTransport(stock.handler)),
        )
        brief = json.dumps({field: "Not specified" for field in BRIEF_FIELDS})
        context_resets = []
        service = MissionService(
            {"db_path": str(db), "mission_sources_dir": str(root / "sources"),
             "mission_ingest_timeout_seconds": 1, "mission_ingest_poll_seconds": 0},
            services, generate=lambda prompt: brief if "exact keys" in prompt else "mission answer",
            on_corpus_change=lambda: context_resets.append(True))
        queue_brief = service._queue_brief
        service._queue_brief = lambda: None

        # Same digest under two names has one retained file; deleting either
        # source must not break recovery of the other.
        content = _pdf_bytes()
        service.upload("first.pdf", content)
        service.upload("second.pdf", content)
        assert len(context_resets) == 2
        digest = service.status()["sources"][0]["sha256"]
        service.delete("first.pdf", "first.pdf")
        assert (root / "sources" / digest).is_file()
        stock.documents.clear()
        service._reconcile()
        assert stock.documents == {"second.pdf"}

        # Partial stock loss is unsafe, not ready: answer starts recovery and
        # refuses generation until every retained source is present.
        recovery_started = []
        real_start = service.start
        service.start = lambda **_: recovery_started.append(True)
        stock.documents.clear()
        try:
            service.answer("What is the objective?")
        except MissionUnavailable as exc:
            assert "reingesting" in str(exc)
        else:
            raise AssertionError("answer ran with a missing corpus document")
        assert recovery_started
        service.start = real_start
        service._reconcile()
        assert stock.documents == {"second.pdf"}

        # A clear waits for a pending upload's stock mutation and cannot leave
        # it orphaned in Chat Q&A after deleting the local record.
        stock.block_posts = True
        stock.post_started.clear()
        stock.release_post.clear()
        upload_error = []
        thread = threading.Thread(
            target=lambda: _capture(upload_error, service.upload, "race.pdf", _pdf_bytes()))
        thread.start()
        assert stock.post_started.wait(1)
        started = time.monotonic()
        assert service.status()["mode"] in {"ingesting", "reingesting"}
        assert time.monotonic() - started < .1, "status waited for stock ingestion"
        clear_done = threading.Event()
        clear_thread = threading.Thread(
            target=lambda: (_capture([], service.clear, "DELETE MISSION"), clear_done.set()))
        clear_thread.start()
        assert not clear_done.wait(.1), "clear interleaved with upload"
        stock.release_post.set()
        thread.join(2)
        clear_thread.join(2)
        assert not upload_error and clear_done.is_set()
        assert not stock.documents and not service.status()["sources"]

        # Recovery queues a missing brief rather than leaving a ready corpus
        # permanently stale after an interrupted or malformed generation.
        service._queue_brief = queue_brief
        stock.block_posts = False
        service.upload("brief.pdf", _pdf_bytes())
        with service._connection() as conn:
            conn.execute("DELETE FROM mission_brief")
            conn.commit()
        service._reconcile()
        for _ in range(50):
            if not service.brief()["stale"]:
                break
            time.sleep(.01)
        assert not service.brief()["stale"]

        attempts = []
        failing = MissionService(
            {"db_path": str(db), "mission_sources_dir": str(root / "failed-sources"),
             "mission_ingest_timeout_seconds": 1, "mission_ingest_poll_seconds": 0},
            services, generate=lambda _: attempts.append(True) or (_ for _ in ()).throw(TimeoutError()))
        failing.upload("timeout.pdf", _pdf_bytes())
        deadline = time.monotonic() + 1
        while failing._brief_running and time.monotonic() < deadline:
            time.sleep(.01)
        assert attempts == [True]
        for _ in range(3):
            assert failing.status()["mode"] == "ready"
            assert failing.brief()["error"] == "Mission text generation is unavailable."
        assert attempts == [True], "brief polling retried a timed-out stock generation"
        failing.start(retry_brief=True)
        deadline = time.monotonic() + 1
        while attempts != [True, True] and time.monotonic() < deadline:
            time.sleep(.01)
        assert attempts == [True, True], "explicit retry did not resubmit the brief"

        deadline = time.monotonic() + 1
        while service._brief_running and time.monotonic() < deadline:
            time.sleep(.01)
        assert not service._brief_running
        services.client.close()

    # Structural bounds run before retention/forwarding.
    pages = _pdf_bytes(MAX_PDF_PAGES + 1, escaped_page_name=True)
    try:
        validate_upload("many-pages.pdf", pages)
    except MissionValidationError as exc:
        assert "pages" in str(exc)
    else:
        raise AssertionError("oversized PDF page count accepted")
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as docx:
        docx.writestr("[Content_Types].xml", "x")
        docx.writestr("word/document.xml", "x")
        for index in range(MAX_DOCX_ENTRIES):
            docx.writestr(f"word/media/{index}.bin", b"x")
    try:
        validate_upload("many-entries.docx", archive.getvalue())
    except MissionValidationError:
        pass
    else:
        raise AssertionError("oversized DOCX entry count accepted")

    # Mission route must classify before history time/camera restrictions.
    with tempfile.NamedTemporaryFile("w", suffix=".yaml") as metadata:
        metadata.write("names: [person]\n")
        metadata.flush()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "app.db"
            conn = connect(str(db))
            migrate(conn)
            conn.execute(
                """INSERT INTO streams (stream_id, display_name, source_uri, annotated_path,
                                        enabled, sort_order)
                   VALUES ('cam1','Camera 1','rtsp://cam1','annotated1',1,0)"""
            )
            conn.commit()
            conn.close()
            mission = types.SimpleNamespace(answer=lambda text, generate=None: {
                "intent": "mission_knowledge", "answer_text": text, "errors": []})
            assistant = Assistant(
                {"assistant_queue_depth": 1, "detector_metadata_path": metadata.name,
                 "db_path": str(db)},
                None, generate=lambda _: '{"intent":"mission_knowledge"}', mission=mission)
            assert assistant.ask("What is the mission objective?")["intent"] == "mission_knowledge"
            assert assistant.ask(
                "What is the mission assignment for camera 1?", "cam2"
            )["intent"] == "mission_knowledge"
            assistant.mission = types.SimpleNamespace(
                answer=lambda text, generate=None: (_ for _ in ()).throw(
                    MissionUnavailable("No mission documents are ready.")))
            try:
                assistant.ask("What weapons are prohibited during the mission today?")
            except ServiceUnavailable as exc:
                assert "No mission documents" in str(exc)
            else:
                raise AssertionError("routed mission outage was not translated")

    # Mission brief/answer generation uses the same bounded admission path as
    # routing, so one in-flight Chat Q&A request blocks a second one.
    started, release = threading.Event(), threading.Event()
    def slow_generation(_):
        started.set()
        assert release.wait(1)
        return "done"
    coordinator = Assistant({"assistant_queue_depth": 1}, None, generate=slow_generation)
    worker = threading.Thread(target=lambda: coordinator.complete("first"))
    worker.start()
    assert started.wait(1)
    try:
        coordinator.complete("second")
    except Exception as exc:
        from app.assistant import QueueFull
        assert isinstance(exc, QueueFull)
    else:
        raise AssertionError("shared generation queue accepted concurrent mission work")
    release.set()
    worker.join(1)

    # Former lock inversion: routed request held the language lock while a
    # direct request held the mission lock and waited for language admission.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = root / "app.db"
        conn = connect(str(db))
        migrate(conn)
        conn.execute(
            """INSERT INTO streams (stream_id, display_name, source_uri, annotated_path,
                                    enabled, sort_order)
               VALUES ('cam1','Camera 1','rtsp://cam1','annotated1',1,0)"""
        )
        conn.commit()
        conn.close()
        metadata = root / "model.yaml"
        metadata.write_text("names: [person]\n")
        stock = StockDocuments()
        stock.documents.add("mission.pdf")
        services = types.SimpleNamespace(
            urls={"chatqna": "http://chatqna"},
            client=httpx.Client(transport=httpx.MockTransport(stock.handler)),
        )
        routing_started, release_routing = threading.Event(), threading.Event()
        def coordinated_generation(prompt):
            if "Classify the operator request" in prompt:
                routing_started.set()
                assert release_routing.wait(1)
                return '{"intent":"mission_knowledge"}'
            return "mission answer"
        assistant = Assistant(
            {"assistant_queue_depth": 2, "detector_metadata_path": str(metadata),
             "db_path": str(db)}, None, generate=coordinated_generation)
        mission = MissionService(
            {"db_path": str(db), "mission_sources_dir": str(root / "sources"),
             "mission_ingest_timeout_seconds": 1, "mission_ingest_poll_seconds": 0},
            services, generate=assistant.complete)
        content = _pdf_bytes()
        digest = hashlib.sha256(content).hexdigest()
        mission._store_original(digest, content)
        with mission._connection() as conn:
            conn.execute(
                """INSERT INTO mission_sources
                   (filename, sha256, uploaded_utc, status, corpus_version)
                   VALUES (?, ?, ?, 'ready', 0)""",
                ("mission.pdf", digest, datetime.now(UTC).isoformat()),
            )
            conn.commit()
        mission._set_mode("ready", "Mission corpus is reconciled.")
        assistant.mission = mission
        errors = []
        routed = threading.Thread(
            target=lambda: _capture(errors, assistant.ask, "What is the mission objective?"))
        routed.start()
        assert routing_started.wait(1)
        direct = threading.Thread(
            target=lambda: _capture(errors, mission.answer, "What is the mission objective?"))
        direct.start()
        time.sleep(.05)
        release_routing.set()
        routed.join(1)
        direct.join(1)
        assert not routed.is_alive() and not direct.is_alive(), "mission request lock-order deadlock"
        assert not errors, errors
        services.client.close()


def _capture(errors, call, *args):
    try:
        call(*args)
    except Exception as exc:
        errors.append(exc)


def test_chat_sessions_streaming_and_followups():
    from app.assistant import Assistant, InvalidInput, ServiceUnavailable
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        conn, _, metadata = _phase5_fixture(root)
        conn.execute("INSERT INTO streams (stream_id,display_name,source_uri,annotated_path,enabled,sort_order) VALUES ('cam2','Camera 2','rtsp://cam2','annotated2',1,1)")
        conn.commit()
        prompts = []
        def generate(prompt):
            prompts.append(prompt)
            return '{"intent":"general_chat"}' if "Classify the operator request" in prompt else "A short answer."
        assistant = Assistant({"db_path": str(root / "phase5.db"),
            "detector_metadata_path": str(metadata), "streams": [
                {"stream_id": "cam1", "display_name": "Camera 1", "description": "Configured test coverage."}]},
            None, generate=generate)
        for invalid in ([], {}, "unknown"):
            try:
                assistant.ask("question", mode=invalid)
            except InvalidInput:
                pass
            else:
                raise AssertionError("invalid answer mode accepted")
        first, second = "a" * 32, "b" * 32
        with assistant.conversation(first):
            assistant.ask("My reference is ORANGE-17.")
            calls = len(prompts)
            assistant.ask("Explain that more simply.")
            assert len(prompts) == calls + 1, "clear general followup used a routing call"
            assert "ORANGE-17" in prompts[-1]
            calls = len(prompts)
            assistant.ask("Why is the sky blue?", mode="general")
            assert len(prompts) == calls + 1, "explicit general chat called the router"
            assert "Configured test coverage" in prompts[-1]
        with assistant.conversation(second):
            assistant.ask("What do you remember?")
            assert "ORANGE-17" not in prompts[-1], "browser contexts mixed"
        assistant.reset_context(first)
        with assistant.conversation(first):
            assert assistant.context_status()["turns"] == 0
        with assistant.conversation(second):
            assert assistant.context_status()["turns"] == 1, "reset affected another conversation"
        with assistant.conversation(first):
            calls = len(prompts)
            assert assistant.ask("What can you do?")["intent"] == "application_help"
            assert "Configured test coverage" in assistant.ask("What does camera 1 cover?")["answer_text"]
            assert len(prompts) == calls, "configured help invoked the model"
            assistant.remember_result(assistant.search({"window": {"kind": "relative", "minutes": 30}, "classes": ["person"]}, "cam1"))
            result = assistant.ask("And camera 2?")
            assert result["query"]["filters"]["streams"] == ["cam2"]
            assert result["query"]["filters"]["classes"] == ["person"]
            assert len(prompts) == calls, "camera comparison invoked the model"
        conn.close()

    wire = b'data: Hello\n\n\n\ndata: world.\n\n'
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            for offset in range(0, len(wire), 3):
                yield wire[offset:offset + 3]
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assistant = Assistant({}, types.SimpleNamespace(client=client, urls={"chatqna": "http://stock"}))
    events = []
    assert assistant._chat_stream("question", events.append) == "Hello\n\nworld."
    assert "".join(e["delta"] for e in events if "delta" in e) == "Hello\n\nworld."
    assert requests == [{"input": "question", "stream": True}]
    assert events[-1]["timing"]["generation_ms"] >= 0
    client.close()
    calls = []
    def timeout(request):
        calls.append(1)
        raise httpx.ReadTimeout("test timeout")
    client = httpx.Client(transport=httpx.MockTransport(timeout))
    assistant.services.client = client
    try:
        assistant._chat_stream("question", lambda _: None)
    except ServiceUnavailable as exc:
        assert "not retried" in str(exc)
    else:
        raise AssertionError("stream timeout accepted")
    assert calls == [1]
    client.close()



def test_application_knowledge_grounding():
    from app.assistant import Assistant, _routing_prompt
    from app.mission import _brief_prompt, OPERATOR_GUIDE
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        conn, _, metadata = _phase5_fixture(root)
        prompts = []
        def generate(prompt):
            prompts.append(prompt)
            if "Classify the operator request" in prompt:
                return '{"intent":"application_help"}'
            return "Pause freezes list updates, not the camera pipeline."
        assistant = Assistant({"db_path": str(root / "phase5.db"),
            "detector_metadata_path": str(metadata), "streams": [
                {"stream_id":"cam1", "display_name":"Entrance", "description":"Test doorway."}]},
            None, generate=generate)
        with assistant.conversation("c" * 32):
            for question, phrase in [("What does Pause do?", "Live video and"),
                    ("How is Mission Brief generated in this system?", "stock Chat Q&A"),
                    ("Can you see camera video?", "cannot see or watch")]:
                direct = assistant.ask(question)
                assert direct["intent"] == "application_help" and phrase in direct["answer_text"]
            assert not prompts, "control explanations invoked inference"
            answer = assistant.ask("How does this application store evidence?", selected_stream="cam1")
            assert answer["intent"] == "application_help"
            assert OPERATOR_GUIDE in prompts[-1]
            assert "Test doorway." in prompts[-1] and '"selected_camera": "cam1"' in prompts[-1]
            assert "is not documented" in prompts[-1]
            assert len(prompts) == 2
            assistant.ask("Explain that more simply.")
            assert len(prompts) == 3 and "How does this application store evidence?" in prompts[-1]
            assistant.ask("What are the limitations of this application?", mode="general")
            assert len(prompts) == 4 and OPERATOR_GUIDE in prompts[-1]
        routing = _routing_prompt("How do I search camera history?", ("person",), None, None)
        assert "application_help" in routing and "HOW to use a control" in routing
        brief = _brief_prompt()
        assert OPERATOR_GUIDE in brief
        assert "Never fill mission fields from the application guide" in brief
        assert '"Not specified"' in brief
        conn.close()


def test_partial_clarification_offers_broader_search():
    from app.assistant import Assistant, InvalidPlan, validate_route
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        conn, cutoff, metadata = _phase5_fixture(root)
        proposal = {"intent":"clarification", "plan":{"window":{"kind":"relative","minutes":10},
            "classes":["person"], "attributes":[]}, "unresolved_conditions":["wearing red shirts"]}
        responses = iter(['{"intent":"clarification"}', json.dumps(proposal)])
        a = Assistant({"db_path":str(root/'phase5.db'),"detector_metadata_path":str(metadata)},
            None, generate=lambda _: next(responses))
        answer = a.ask("How many people wearing red shirts were detected in the last 10 minutes?", "cam1")
        assert 'query' not in answer, 'partial proposal ran automatically'
        alternative = answer['broader_search']
        assert alternative['excluded_conditions'] == ['other conditions in the original question']
        assert alternative['plan']['classes'] == ['person'] and alternative['plan']['streams'] == ['cam1']
        assert alternative['plan']['window']['kind'] == 'absolute'
        result = a.search(alternative['plan'],alternative['selected_stream'])
        assert result['query']['total'] == 1
        assert result['query']['window']['started_utc'] == alternative['plan']['window']['start_utc']
        for changes in ({'unresolved_conditions':[]}, {'unresolved_conditions':['x'*257]},
                        {'plan':dict(proposal['plan'],classes=None)}):
            try:
                validate_route(dict(proposal,**changes), labels=('person',), known_streams=('cam1',),
                    selected_stream='cam1',cutoff=cutoff,retention=timedelta(days=7))
            except InvalidPlan:
                pass
            else:
                raise AssertionError('unsafe partial proposal accepted')
        a.generate = lambda _: '{"intent":"clarification"}'
        assert 'broader_search' not in a.ask('What happened?', 'cam1'), 'invented scope for ambiguous request'
        short = a.ask('red shirt in the last 10 minutes', 'cam1')
        assert short['broader_search']['plan']['classes'] is None
        assert 'all recorded object types' in short['answer_text']
        assert short['broader_search']['plan']['window']['kind'] == 'absolute'

        responses = iter(['not JSON', json.dumps(proposal) + '\nUntrusted extra commentary.'])
        a.generate = lambda _: next(responses)
        recovered = a.ask('How many people wearing red shirts were detected in the last 10 minutes?', 'cam1')
        assert recovered['broader_search']['excluded_conditions'] == ['wearing red shirts']
        assert 'query' not in recovered and 'Untrusted extra' not in recovered['answer_text']
        a.generate = lambda _: 'not JSON'
        assert a.ask('How many people wearing red shirts were detected in the last 10 minutes?', 'cam1')['broader_search']['plan']['classes'] == ['person']


        conn.close()


def test_dynamic_classification_metadata_queries():
    from app.assistant import Assistant, attribute_catalog, _utc
    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp)
        root = Path(tmp)
        metadata = root / 'labels.yaml'
        metadata.write_text('names:\n  0: person\n')
        now = datetime.now(UTC).replace(microsecond=0)
        def frame(camera, tracker, seconds, attributes):
            body = json.loads(_observation(camera, tracker, observed_at=now.timestamp()+seconds))
            body['metadata']['objects'][0].update({name:{'label':value, 'confidence':.9,
                'model':{'name':'fixture-classifier'}} for name,value in attributes.items()})
            ing._on_message(None, None, types.SimpleNamespace(topic='detections/live', payload=json.dumps(body).encode()))
        frame('cam1', 1, -120, {'upper_color':'red', 'lower_color':'white', 'garment_type':'shirt'})
        frame('cam1', 1, -119, {'upper_color':'red', 'lower_color':'white', 'garment_type':'shirt'})
        frame('cam1', 1, -118, {'upper_color':'blue', 'lower_color':'black', 'garment_type':'shirt'})
        frame('cam1', 2, -117, {})
        frame('cam1', 3, -116, {'surface_finish':'A42'})
        frame('cam2', 1, -115, {'upper_color':'red','garment_type':'shirt'})
        ing._flush_intervals(force=True)
        ing.conn.commit()
        samples = ing.conn.execute('SELECT * FROM attribute_samples ORDER BY sample_id').fetchall()
        assert len(samples) == 4 and samples[0]['observation_count'] == 2
        assert json.loads(samples[0]['source_json'])['upper_color']['model'] == 'fixture-classifier'
        cfg = dict(ing.cfg, db_path=str(root/'ingest.db'), detector_metadata_path=str(metadata))
        prompts = []
        def generate(prompt):
            prompts.append(prompt)
            return json.dumps({'intent':'video_history','plan':{'window':{'kind':'relative','minutes':10},
                'classes':['person'], 'attributes':[{'name':'upper_color','value':'red'},{'name':'garment_type','value':'shirt'}]}})
        a = Assistant(cfg, None, generate=generate)
        def search(attributes, camera='cam1', window=None):
            return a.search({'window':window or {'kind':'relative','minutes':10},
                'classes':['person'], 'attributes':[{'name':k,'value':v} for k,v in attributes.items()]},camera)
        red = search({'upper_color':'red'})
        assert red['query']['total'] == 1
        from app.assistant import encode_page_token, query_context, validate_plan
        known, ceiling, scope = query_context(cfg['db_path'])
        plan = validate_plan({'window':{'kind':'relative','minutes':10}, 'classes':['person'],
            'attributes':[{'name':'upper_color','value':'red'}]}, labels=('person',), known_streams=known,
            selected_stream='cam1', cutoff=now, retention=timedelta(days=7))
        token = encode_page_token(plan, now, ceiling, {'cam1':scope['cam1']}, a.page_secret)
        assert a.page(token)['query']['filters']['attributes'] == plan['attributes']

        assert red['query']['attribute_coverage'] == {'candidate_tracks':3,'tracks_with_fields':1,'tracks_without_fields':2}
        assert search({'upper_color':'red','lower_color':'black'})['query']['total'] == 0, 'labels from different moments combined'
        assert search({'upper_color':'purple'})['query']['total'] == 0, 'unseen value treated as unsupported taxonomy'
        assert search({'surface_finish':'A42'})['query']['total'] == 1, 'domain vocabulary was hardcoded'
        assert search({'surface_finish':'A42'},'cam2')['recovery']['reason'] == 'attribute_data_unavailable'
        window = {'kind':'absolute','start_utc':_utc(now-timedelta(seconds=118)), 'end_utc':_utc(now-timedelta(seconds=100))}
        assert search({'upper_color':'red'},window=window)['query']['total'] == 0, 'old attributes leaked into later window'
        assert search({'invented_field':'x'})['recovery']['reason'] == 'attribute_data_unavailable'
        unavailable = search({'upper_color':'red','invented_field':'x'})
        alternative = unavailable['broader_search']
        assert alternative['excluded_attributes'] == [{'name':'invented_field','value':'x'}]
        assert alternative['plan']['attributes'] == [{'name':'upper_color','value':'red'}]
        assert alternative['plan']['classes'] == ['person'] and alternative['plan']['streams'] == ['cam1']
        assert alternative['plan']['window']['kind'] == 'absolute'
        assert a.search(alternative['plan'], alternative['selected_stream'])['query']['total'] == 1
        empty = search({'upper_color':'purple'})
        assert empty['query']['total'] == 0 and empty['broader_search']['plan']['attributes'] == []
        assert empty['broader_search']['plan']['window']['start_utc'] == empty['query']['window']['started_utc']

        assert {r['name'] for r in attribute_catalog(cfg['db_path'],'cam2')} == {'upper_color','garment_type'}
        with a.conversation('d'*32):
            answer = a.ask('How many people wearing red shirts were detected in the last 10 minutes?', 'cam1')
            assert answer['query']['total'] == 1 and 'upper_color' in prompts[-1]
            assert a.ask('And camera 2?')['query']['filters']['attributes'] == [{'name':'upper_color','value':'red'},{'name':'garment_type','value':'shirt'}]
        # A new camera/session doesn't inherit old classification capabilities.
        ing._on_message(None,None,types.SimpleNamespace(topic='detections/live',payload=_observation('cam1',1,
            observed_at=now.timestamp()-10,session='s2')))
        ing._flush_intervals(force=True); ing.conn.commit()
        assert attribute_catalog(cfg['db_path'],'cam1') == []
        assert search({'upper_color':'red'})['recovery']['reason'] == 'attribute_data_unavailable'
        ing.conn.execute("DELETE FROM tracks WHERE stream_id='cam1'")
        ing.conn.commit()
        assert ing.conn.execute('SELECT count(*) FROM attribute_samples').fetchone()[0] == 1
        ing.conn.close()


def test_unmapped_conditions_and_catalog_overflow_degrade_safely():
    """A qualified question must never be answered by an unqualified search."""
    from app.assistant import Assistant, _utc, attribute_catalog

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        conn, _, metadata = _phase5_fixture(root)
        cfg = {"db_path": str(root / "phase5.db"), "detector_metadata_path": str(metadata),
               "history_retention_seconds": 604800,
               "streams": [{"stream_id": "cam1", "display_name": "Camera 1", "enabled": True}]}
        base = {"window": {"kind": "relative", "minutes": 10}, "order": "last_observed_desc",
                "page": 0, "page_size": 20}

        def router(classes, attributes):
            return lambda _prompt: json.dumps({"intent": "video_history",
                "plan": dict(base, classes=classes, attributes=attributes)})

        # The model keeps the supported class and drops the quality beside it.
        dropped = Assistant(cfg, None, generate=router(["person"], []))
        for question in ("Show people carrying weapons in the last ten minutes",
                         "Show suspicious people in the last ten minutes",
                         "Which people in uniform were seen in the last ten minutes"):
            answer = dropped.ask(question, "cam1")
            assert "query" not in answer, f"unfiltered search answered {question!r}"
            assert answer["intent"] == "clarification", answer
            assert answer["broader_search"]["plan"]["classes"] == ["person"], answer
            assert "will not check" in answer["answer_text"] or "not check" in answer["answer_text"]
        # The same guard must not block an ordinary question.
        plain = dropped.ask("Show people in the last ten minutes", "cam1")
        assert plain["intent"] == "video_history" and plain["query"]["total"] == 1, plain

        # A quality the plan genuinely filters on stays answerable.
        now = datetime.now(UTC)
        conn.execute("""INSERT INTO attribute_samples (track_id, started_utc, ended_utc,
            attributes_json, source_json) VALUES (1,?,?,?,'{}')""",
            (_utc(now - timedelta(minutes=10)), _utc(now - timedelta(minutes=8)),
             json.dumps({"clothing": "uniform"})))
        conn.commit()
        mapped = Assistant(cfg, None, generate=router(["person"], [{"name": "clothing", "value": "uniform"}]))
        answer = mapped.ask("Which people in uniform were seen in the last ten minutes", "cam1")
        assert answer["intent"] == "video_history" and answer["query"]["total"] == 1, answer

        # Field names come from pipeline metadata, so their count is unbounded.
        for index in range(CATALOG_OVERFLOW):
            conn.execute("""INSERT INTO attribute_samples (track_id, started_utc, ended_utc,
                attributes_json, source_json) VALUES (1,?,?,?,'{}')""",
                (_utc(now - timedelta(minutes=5)), _utc(now - timedelta(minutes=4)),
                 json.dumps({f"field_{index:04}": "value"})))
        conn.commit()
        describing = Assistant(cfg, None, generate=lambda _prompt: "An answer.")
        options = describing.options()
        assert len(options["attributes"]) == 128 and options["attributes_truncated"] is True, options
        assert describing.ask("Why is the sky blue?", None, mode="general")["intent"] == "general_chat"
        assert attribute_catalog(cfg["db_path"]).truncated is True

        # An exact membership test must not be answered from a truncated list.
        conn.execute("""INSERT INTO attribute_samples (track_id, started_utc, ended_utc,
            attributes_json, source_json) VALUES (1,?,?,?,'{}')""",
            (_utc(now - timedelta(minutes=9)), _utc(now - timedelta(minutes=8)),
             json.dumps({"zzz_sorts_after_truncation": "present"})))
        conn.commit()
        late = Assistant(cfg, None, generate=router(
            ["person"], [{"name": "zzz_sorts_after_truncation", "value": "present"}]))
        answer = late.ask(
            "Show person tracks with zzz_sorts_after_truncation present in the last ten minutes", "cam1")
        assert answer["intent"] == "video_history", (
            "a field sorting past the description limit was reported unavailable")
        conn.close()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all checks passed")
