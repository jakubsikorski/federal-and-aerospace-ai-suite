#!/usr/bin/env python3
"""Self-check for Backend logic that can fail silently. Run: python3 backend/test_backend.py

Deliberately framework-free and offline: it covers config derivation, the
schema migration, stream sync and observation demultiplexing.
Service adapters and the pipeline lifecycle are not mocked here - they are proven
against the real pinned containers via /api/health and the Phase 2 evidence,
which are the only assertions that actually mean anything for them.
"""
from contextlib import nullcontext
import hashlib
import base64
import json
import os
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
# `make test` runs inside the deployed Backend, whose demo camera count must not
# rewrite the fixture configs these tests load.
os.environ.pop("BACKEND_DEMO_CAMERAS", None)

# Enough distinct pipeline metadata field names to exceed the described subset.
CATALOG_OVERFLOW = 130

from app.core import connect, envelope, load_config, migrate, request_id_var, sync_streams


def _is_brief_prompt(prompt):
    """Brief generation now asks one grounded question per field."""
    from app.mission import BRIEF_QUESTIONS
    return any(question in prompt for question in BRIEF_QUESTIONS.values())


def _pdf_bytes(page_count=1, escaped_page_name=False, text="Mission text"):
    page_type = b"/Pa#67e" if escaped_page_name else b"/Page"
    font, content = page_count + 3, page_count + 4
    stream = f"BT /F1 12 Tf 10 10 Td ({text}) Tj ET".encode() if text else b""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + b" ".join(
            f"{index} 0 R".encode() for index in range(3, page_count + 3)
        ) + f"] /Count {page_count} >>".encode(),
    ] + [b"<< /Type " + page_type + b" /Parent 2 0 R /MediaBox [0 0 200 50]"
         + f" /Resources << /Font << /F1 {font} 0 R >> >> /Contents {content} 0 R >>".encode()] * page_count + [
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
    ]
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


def test_demo_camera_count_enables_only_named_sources():
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write("streams:\n" + "".join(
            f"  - {{stream_id: cam{i}, display_name: C{i}, sort_order: {i}}}\n" for i in (1, 2, 3)))
    try:
        os.environ["BACKEND_DEMO_CAMERAS"] = "2"
        enabled = {s["stream_id"]: s["enabled"] for s in load_config(fh.name)["streams"]}
        assert enabled == {"cam1": True, "cam2": True, "cam3": False}, enabled
        os.environ["BACKEND_DEMO_CAMERAS"] = "0"  # make up: no deployment cameras
        assert not any(s["enabled"] for s in load_config(fh.name)["streams"])
        os.environ["BACKEND_DEMO_CAMERAS"] = "4"
        try:
            load_config(fh.name)
            raise AssertionError("accepted a camera count with no configured source")
        except ValueError:
            pass
    finally:
        os.environ.pop("BACKEND_DEMO_CAMERAS", None)


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
                 generation="g1", confidence=0.9, detection_rect=None, frame_width=None,
                 frame_height=None, object_touches_frame=None):
    obj = {"id": tracker_id, "detection": {"label": label, "confidence": confidence}}
    if detection_rect is not None:
        obj["detection_rect"] = detection_rect
    if frame_width is not None:
        obj["frame_width"] = frame_width
    if frame_height is not None:
        obj["frame_height"] = frame_height
    if object_touches_frame is not None:
        obj["object_touches_frame"] = object_touches_frame
    return json.dumps({"metadata": {
        "crop_context": {"stream_id": stream_id, "tracking_session_id": session,
                         "history_generation": generation, "observed_at": observed_at,
                         "frame_sequence": 1},
        "objects": [obj],
    }, "blob": ""}).encode()


def _ingest(tmp, cameras=("cam1", "cam2"), min_track_observations=1):
    """Real Ingest, real SQLite, no broker: only _on_message is exercised."""
    from app.ingest import Ingest

    crops = Path(tmp) / "crops"
    for camera in cameras:
        (crops / camera).mkdir(parents=True, exist_ok=True)
    cfg = {"streams": [{"stream_id": c} for c in cameras],
           "mqtt": {"host": "unused", "port": 0}, "detections_topic": "detections/live",
           "snips_topic": "detections/snips", "crop_dir": str(crops),
           "track_timeout_seconds": 10.0, "freshness_window_seconds": 3.0,
           "absence_split_seconds": 5.0, "pending_association_seconds": 120.0,
           "min_track_observations": min_track_observations}
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


def test_thumbnail_url_changes_only_when_best_candidate_changes():
    from app.history import _snip
    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp, cameras=("cam1",))
        _send(ing, _observation("cam1", 1), topic="detections/live")
        track_id = ing.conn.execute("SELECT track_id FROM tracks").fetchone()[0]
        def crop(cid, quality):
            event = json.loads(_crop("cam1", 1, cid))
            event["quality_score"] = quality
            _send(ing, json.dumps(event).encode())
        crop("a" * 32, .5)
        first = _snip(ing.conn, track_id)
        crop("b" * 32, .2)
        assert _snip(ing.conn, track_id) == first
        crop("c" * 32, .9)
        improved = _snip(ing.conn, track_id)
        assert first != improved and improved[0].endswith("c" * 32)
        ing.conn.execute("UPDATE candidates SET image_state='missing' WHERE candidate_id=?", ("c" * 32,))
        assert _snip(ing.conn, track_id) == first
        ing.conn.close()


def test_sidecar_read_failure_is_retried():
    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp, cameras=("cam1",))
        cid = "f" * 32
        sidecar = Path(ing.cfg["crop_dir"]) / "cam1" / (cid + ".json")
        sidecar.write_text("{")
        assert ing.reconcile_sidecars() == 0
        sidecar.write_bytes(_crop("cam1", 1, cid))
        assert ing.reconcile_sidecars() == 1
        assert ing.reconcile_sidecars() == 0
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
            "kind": "tracking_session", "since_utc": None, "limit": 3, "offset": 0,
            "priority_classes": []}, answer

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
            "pipeline": "vah264dec ! gvadetect model=/models/INT8/yolo26s.xml device=NPU ! gvatrack name=tracking ! appsink"}]}}))
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
        replies["/model"] = None
        early = placement.snapshot({}, {})["models"][1]
        assert early["status"] == "starting", "never-reached service may still be downloading"
        assert "downloading" in early["note"]
        replies["/model"] = {"llm_model": "Intel/neural-chat-7b-v3-3"}
        placement.expires = 0
        calls.clear()
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
        assert result["models"][0]["status"] == "idle", "no cameras on: pipelines are not running, not failed"
        assert result["models"][1]["status"] == "mismatch"
        assert result["models"][3]["status"] == "unavailable"
        assert result["models"][3]["configured_device"] == "GPU"
        assert result["models"][3]["runtime_device"] is None
        # make TTS_DEVICE=CPU: placement and the readiness probe follow the selection.
        replies["/v1/model-info"] = {"model": "microsoft/speecht5_tts", "device": "GPU"}
        os.environ["TTS_DEVICE"] = "CPU"
        try:
            placement.expires = 0
            speech = placement.snapshot({}, {})["models"][3]
            assert speech["configured_device"] == "CPU" and speech["status"] == "mismatch"
            try:
                services.tts()
                raise AssertionError("GPU speech must fail the CPU selection")
            except RuntimeError as exc:
                assert "expected CPU" in str(exc)
            replies["/v1/model-info"]["device"] = "CPU"
            assert services.tts()[0] == "microsoft/speecht5_tts on CPU"
        finally:
            del os.environ["TTS_DEVICE"]
        assert services.tts()[0] == "microsoft/speecht5_tts on CPU", "CPU is the default speech device"
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
        # No classification fields are recorded: classes and time need no router call.
        assert answer["intent"] == "video_history" and not answer.get("assumption") and not calls, answer
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
    # Live router output for "What is yolo26s": an answer inside the JSON plus a note.
    assert decode_route_completion(
        '{"intent":"general_chat","text":"YOLO is a detector."}\n\nNote: This response is general knowledge.'
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

        # Knowledge questions skip routing; video/app/mission wording still routes.
        routed = []
        def counting(prompt):
            if "Classify the operator request" in prompt:
                routed.append(prompt)
                return '{"intent":"general_chat"}'
            return "General reply"
        fast = Assistant({"db_path": str(root / "phase5.db"), "detector_metadata_path": str(metadata)},
                         None, generate=counting)
        for question in ("What is yolo26s", "What is a thermal camera?", "Explain OpenVINO.", "Who was Alan Turing?"):
            assert fast.ask(question)["intent"] == "general_chat", question
        assert routed == [], routed
        for question in ("What is on camera 1?", "What was seen in the last 10 minutes?",
                         "What are the rules?", "What is the mission objective?"):
            before = len(routed)
            try:
                intent = fast.ask(question)["intent"]
            except Exception:
                intent = "error"
            assert len(routed) > before or intent != "general_chat", question

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
        assert a.ask("What was seen?", "cam1")["assumption"].startswith("You didn't give a time")
        a.generate = lambda _: '{"intent":"clarification"}'
        weapons = a.ask("Were there weapons in the last ten minutes?", "cam1")
        assert weapons["intent"] == "video_history" and weapons["assumption"].startswith("I can't check weapons"), weapons
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


def test_camera_history_question_fast_path_common_phrasings():
    from app.assistant import Assistant, simple_plan, _utc
    labels = ("person", "car", "truck", "bicycle")
    for text, classes, minutes in [
        ("Were any cars seen in the last hour?", ["car"], 60),
        ("Any people on camera 2 in the last 10 minutes?", ["person"], 10),
        ("Did you see a truck today?", ["truck"], 1440),
        ("How many persons were detected in the past 30 minutes?", ["person"], 30),
        ("Show me cars from the last hour", ["car"], 60),
        # Whisper spells small counts out.
        ("Show me people from the last three minutes.", ["person"], 3),
        ("What objects were detected in the last three minutes?", None, 3),
        ("Any cars in the past twenty minutes?", ["car"], 20),
    ]:
        plan = simple_plan(text, labels, None)
        assert plan and plan["classes"] == classes and plan["window"]["minutes"] == minutes, text
    assert simple_plan("Were any red cars seen in the last hour?", labels, None) is None
    assert simple_plan("Was there a bicycle on camera 1 since 14:00?", labels,
                       {"kind": "absolute", "start_utc": "2026-09-25T12:00:00Z",
                        "end_utc": "2026-09-25T13:00:00Z"})["classes"] == ["bicycle"]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        conn, _, metadata = _phase5_fixture(root)
        conn.execute("""INSERT INTO streams (stream_id, display_name, source_uri, annotated_path,
                       enabled, sort_order, tracking_session_id, history_generation)
                       VALUES ('cam2','Camera 2','rtsp://cam2','annotated2',1,1,'session-2','generation-2')""")
        now = datetime.now(UTC).replace(microsecond=0)
        conn.execute("""INSERT INTO tracks (
             track_id, stream_id, tracking_session_id, history_generation, tracker_id,
             class_label, first_observed_utc, last_observed_utc, observation_count,
             peak_confidence, confidence_freshness, closed_at, closure_reason)
             VALUES (99, 'cam2', 'session-2', 'generation-2', 99, 'person', ?, ?, 2, .9,
                     'tracker_metadata_unverified', ?, 'timeout')""",
                     (_utc(now - timedelta(minutes=6)), _utc(now - timedelta(minutes=5)),
                      _utc(now - timedelta(minutes=4))))
        conn.execute("""INSERT INTO track_intervals (
             track_id, interval_index, stream_id, started_utc, ended_utc, observation_count)
             VALUES (99, 0, 'cam2', ?, ?, 2)""",
                     (_utc(now - timedelta(minutes=6)), _utc(now - timedelta(minutes=5))))
        conn.commit()
        calls = []
        assistant = Assistant({"db_path": str(root / "phase5.db"), "detector_metadata_path": str(metadata)},
                              None, generate=lambda prompt: calls.append(prompt) or '{"intent":"clarification"}')
        answer = assistant.ask("Any people on camera 2 in the last 10 minutes?")
        assert answer["intent"] == "video_history" and answer["query"]["filters"]["streams"] == ["cam2"]
        assert not calls, "camera-history fast path invoked LLM router"
        conn.close()


def test_questions_never_dead_end():
    from app.assistant import Assistant
    snapshot = {"models": [
        {"id": "chatqna", "label": "LLM", "configured_model": "Intel/neural-chat-7b-v3-3",
         "configured_device": "GPU", "runtime_model": "Intel/neural-chat-7b-v3-3", "status": "ready"}]}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        conn, _, metadata = _phase5_fixture(root)
        cfg = {"db_path": str(root / "phase5.db"), "detector_metadata_path": str(metadata),
               "streams": [{"stream_id": "cam1", "display_name": "Camera 1"},
                           {"stream_id": "cam2", "display_name": "Camera 2"}]}
        prompts = []
        a = Assistant(cfg, None, generate=lambda prompt: prompts.append(prompt) or "It runs neural-chat on the GPU.")
        a.models = lambda: snapshot
        # No record words: one general call, no routing call, models in context.
        answer = a.ask("What model is this?")
        assert answer["intent"] == "general_chat" and len(prompts) == 1, (answer, len(prompts))
        assert "language model: Intel/neural-chat-7b-v3-3 on GPU" in prompts[0] and "intent" not in prompts[0][:200]
        # Every general prompt carries the build description; the model, not a
        # question pattern, decides whether the question is about this application.
        assert "Application facts (complete and authoritative):" in prompts[0] and "- Assistant: Chat Q&A Core" in prompts[0]
        assert "For an application question" in prompts[0] and "It never describes this application" in prompts[0]
        prompts.clear()
        a.ask("What is an NPU?")
        assert len(prompts) == 1 and "Application facts (complete and authoritative):" in prompts[0], prompts
        assert "language model: Intel/neural-chat-7b-v3-3 on GPU" in prompts[0]

        prompts.clear()
        answer = a.ask("Tell me a joke about this setup")
        assert answer["intent"] == "general_chat" and len(prompts) == 1

        # A record question with no time searches the last 10 minutes and says so.
        a = Assistant(cfg, None, generate=lambda _: (_ for _ in ()).throw(AssertionError("unneeded model call")))
        answer = a.ask("What was seen?", "cam1")
        assert answer["intent"] == "video_history" and answer["answer_text"].startswith("You didn't give a time"), answer

        # Router cannot map the question: search everything, never refuse.
        a = Assistant(cfg, None, generate=lambda _: '{"intent":"clarification"}')
        answer = a.ask("Anything odd with the suitcases I should know about?")
        assert answer["intent"] == "video_history" and "I can't check odd from" in answer["answer_text"], answer

        # The spoken camera wins over a stale scope selector.
        a = Assistant(cfg, None, generate=lambda _: (_ for _ in ()).throw(AssertionError("unneeded model call")))
        answer = a.ask("Any people on camera 1 in the last 10 minutes?", "cam2")
        assert answer["intent"] == "video_history" and answer["query"]["filters"]["streams"] == ["cam1"]
        assert answer["answer_text"].startswith("You named Camera 1"), answer
        # A named camera also narrows an "All cameras" scope, on every route.
        conn.execute("""INSERT INTO streams (stream_id, display_name, source_uri, annotated_path, enabled,
                        sort_order, tracking_session_id, history_generation)
                        VALUES ('cam2','Camera 2','rtsp://cam2','annotated2',1,1,'session-2','generation-2')""")
        conn.commit()
        assert a.ask("What was seen?", "all")["query"]["filters"]["streams"] == ["cam1", "cam2"]
        for question in ("Any people on camera 1 in the last 10 minutes?", "What was seen recently on camera 1?"):
            answer = a.ask(question, "all")
            assert answer["query"]["filters"]["streams"] == ["cam1"], (question, answer)
            assert not answer["answer_text"].startswith("You named"), answer
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
    from app.main import answer_speech_text
    gap = {"total": 1, "class_counts": {"dog": 1}, "warnings": [
        {"message": "cam1: recording coverage is incomplete for 4 seconds in this window."},
        {"message": "cam2: recording coverage is incomplete for 14 seconds in this window."}]}
    shown = {"answer_text": "Note. " + _history_answer(gap), "query": gap}
    assert answer_speech_text(shown) == "Note. Found 1 dog track in the requested window."
    assert "cam2: recording coverage" in shown["answer_text"] and shown["spoken_text"] == "Note. Found 1 dog track in the requested window."
    assert answer_speech_text({"answer_text": "Hello."}) == "Hello."

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
        # No classification fields are recorded, so no routing call is needed.
        assert answer["query"]["filters"]["classes"] == ["person"] and not calls
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
        service = MissionService(
            {"db_path": str(db), "mission_sources_dir": str(root / "mission_sources"),
             "mission_ingest_timeout_seconds": 1, "mission_ingest_poll_seconds": 0},
            services, generate=lambda prompt: "Not specified" if _is_brief_prompt(prompt)
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


def test_mission_upload_limits_are_configurable_and_types_are_named():
    from app.mission import NO_TEXT, MissionService, MissionValidationError, upload_limits, validate_upload

    def rejected(name, content, limits=None):
        try:
            validate_upload(name, content, limits)
        except MissionValidationError as exc:
            return str(exc)
        return None

    assert upload_limits({}) == {"max_upload_mb": 25, "max_pdf_pages": 200, "max_docx_uncompressed_mb": 50}
    for bad in (0, 5000, True, "10", 2.5):
        try:
            upload_limits({"mission_max_pdf_pages": bad})
        except ValueError as exc:
            assert "mission_max_pdf_pages" in str(exc)
        else:
            raise AssertionError(f"out-of-range page limit accepted: {bad!r}")
    small = upload_limits({"mission_max_pdf_pages": 5, "mission_max_upload_mb": 1})
    assert rejected("five.pdf", _pdf_bytes(5), small) is None
    assert rejected("six.pdf", _pdf_bytes(6), small) == "Mission PDFs may contain at most 5 pages; this one has 6."
    assert rejected("six.pdf", _pdf_bytes(6)) is None, "default limit changed"
    assert rejected("big.pdf", _pdf_bytes(1) + b" " * (1024 * 1024), small) == "Mission documents must be 1 MB or smaller."

    message = rejected("photo.jpg", b"\xff\xd8")
    assert "PDF or Word (.docx)" in message and "Images" in message, message
    assert rejected("scan.pdf", _pdf_bytes(3, text="")) == NO_TEXT

    def docx(body):
        archive = BytesIO()
        with zipfile.ZipFile(archive, "w") as doc:
            doc.writestr("[Content_Types].xml", "x")
            doc.writestr("word/document.xml", body)
        return archive.getvalue()
    assert rejected("scan.docx", docx('<w:body><w:p><w:r><w:drawing/></w:r></w:p></w:body>')) == NO_TEXT
    assert rejected("brief.docx", docx('<w:body><w:p><w:r><w:t xml:space="preserve">Hold</w:t></w:r></w:p></w:body>')) is None

    with tempfile.TemporaryDirectory() as tmp:
        service = MissionService({"db_path": str(Path(tmp) / "m.db"), "mission_sources_dir": tmp,
                                  "mission_max_pdf_pages": 400}, None)
        assert service.limits["max_pdf_pages"] == 400


def test_mission_phase6_failure_matrix():
    """Regression coverage for lifecycle serialization, corpus truth and bounded uploads."""
    from app.assistant import Assistant, ServiceUnavailable
    from app.mission import (
        BRIEF_FIELDS,
        MAX_DOCX_ENTRIES,
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
        context_resets = []
        service = MissionService(
            {"db_path": str(db), "mission_sources_dir": str(root / "sources"),
             "mission_ingest_timeout_seconds": 1, "mission_ingest_poll_seconds": 0},
            services, generate=lambda prompt: "Not specified" if _is_brief_prompt(prompt)
            else "mission answer",
            on_corpus_change=lambda: context_resets.append(True))
        queue_brief = service._queue_brief
        service._queue_brief = lambda: None

        # Refused connections read as "starting" (model still loading or
        # downloading), and the next status poll recovers without operator action.
        handler = stock.handler
        def refusing(request):
            raise httpx.ConnectError("refused", request=request)
        services.client._transport = httpx.MockTransport(refusing)
        waiting = service.status()
        assert waiting["mode"] == "starting" and "downloading" in waiting["detail"], waiting
        services.client._transport = httpx.MockTransport(handler)
        assert service.status()["mode"] == "ready"

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
    pages = _pdf_bytes(201, escaped_page_name=True)
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
            mission = types.SimpleNamespace(answer=lambda text, generate=None, turns=(): {
                "intent": "mission_knowledge", "answer_text": text, "errors": []})
            assistant = Assistant(
                {"assistant_queue_depth": 1, "detector_metadata_path": metadata.name,
                 "db_path": str(db)},
                None, generate=lambda _: '{"intent":"mission_knowledge"}', mission=mission)
            assert assistant.ask("What is the mission objective?")["intent"] == "mission_knowledge"
            # Questions naming the mission documents skip the router entirely.
            prompts = []
            direct = Assistant(
                {"assistant_queue_depth": 1, "detector_metadata_path": metadata.name, "db_path": str(db)},
                None, generate=lambda prompt: prompts.append(prompt) or "routed", mission=mission)
            for question in ("What is the objective in the mission documents?",
                             "What restrictions are specified in the mission documents?"):
                answer = direct.ask(question)
                assert answer == {"intent": "mission_knowledge", "answer_text": question, "errors": []}, answer
            assert prompts == [], prompts
            from app.mission import _mission_prompt
            assert _mission_prompt("What is the mission objective?").startswith("What is the mission objective?\n")
            assert direct.ask("Delete the mission documents")["intent"] != "mission_knowledge" or prompts
            assert assistant.ask(
                "What is the mission assignment for camera 1?", "cam2"
            )["intent"] == "mission_knowledge"
            assistant.mission = types.SimpleNamespace(
                answer=lambda text, generate=None, turns=(): (_ for _ in ()).throw(
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
        # Operational wording is routed without the model; this one still needs it.
        routed = threading.Thread(
            target=lambda: _capture(errors, assistant.ask, "Summarise the packet for me"))
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
        for question in ("When is the report due?", "Am I allowed to stop people?", "Who is the safety officer?",
                         "What are the risks?", "What resources do I have?", "When does the watch start?"):
            assert assistant._mission_route(question, None) == "terms", question
        for question in ("Were any people seen in the last 10 minutes?", "Show person tracks",
                         "What is YOLO26s?", "Explain photosynthesis", "Any cars at 14:05?"):
            assert assistant._mission_route(question, None) is None, question
        routing_started.clear()
        answer = assistant.ask("When is the report due?")
        assert answer["intent"] == "mission_knowledge" and not routing_started.is_set()
        with mission._connection() as conn:
            conn.execute("INSERT INTO mission_brief (corpus_version, generated_utc, brief_json, stale) "
                         "VALUES (0, '2026-01-01T00:00:00Z', ?, 0)",
                         (json.dumps({"fields": {"objective": "Exercise AMBER FIELD watches the benches."}}),))
            conn.commit()
        assert assistant._mission_route("Tell me about amber field", None) == "vocabulary"
        assert assistant._mission_route("Which person waited near the benches?", None) is None
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
            # Plain knowledge: no ~1.3k-token application guide, only the build description.
            assert "Configured test coverage" not in prompts[-1] and "ORANGE-17" in prompts[-1]
            from app.assistant import _architecture_brief
            assert len(prompts[-1]) < 2500 + len(_architecture_brief()), len(prompts[-1])
            assistant.ask("Which model does this app use for detection?", mode="general")
            assert "Configured test coverage" in prompts[-1], "system question lost the guide"
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


def test_mission_answers_emit_streaming_deltas():
    from app.assistant import Assistant
    wire = b"data: Mission \n\ndata: answer.\n\n"
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield wire
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())
    class Mission:
        def answer(self, question, *, generate=None, turns=()):
            text = generate("mission prompt")
            return {"intent": "mission_knowledge", "answer_text": text, "spoken_text": None,
                    "source_inventory": [], "answer_citations_available": False,
                    "citation_notice": "Answer-level citations are unavailable from the stock mission interface.",
                    "errors": []}
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assistant = Assistant({"assistant_queue_depth": 1},
                          types.SimpleNamespace(client=client, urls={"chatqna": "http://stock"}),
                          mission=Mission())
    events = []
    result = assistant._mission_answer("What is the mission?", events.append)
    assert result["answer_text"] == "Mission answer."
    assert "".join(e["delta"] for e in events if "delta" in e) == "Mission answer."
    client.close()


def test_mission_follow_up_questions_carry_earlier_mission_turns():
    from app.assistant import Assistant
    from app.mission import MISSION_UNAVAILABLE_ANSWER, _mission_prompt
    replies = {"When is the report due?": "The report is due at 18:00.",
               "Who receives it?": "The duty officer receives it.",
               "Is there a radio schedule?": MISSION_UNAVAILABLE_ANSWER}
    seen = []

    class Mission:
        def loaded(self):
            return True

        def vocabulary(self):
            return {"radio"}

        def answer(self, question, *, generate=None, turns=()):
            seen.append((question, list(turns)))
            return {"intent": "mission_knowledge", "answer_text": replies[question], "errors": []}

    with tempfile.NamedTemporaryFile("w", suffix=".yaml") as metadata:
        metadata.write("names:\n  0: person\n  1: dog\n")
        metadata.flush()
        assistant = Assistant({"assistant_queue_depth": 1, "detector_metadata_path": metadata.name},
                              None, generate=lambda _: "unused", mission=Mission())
        assistant.ask("When is the report due?")
        assistant.ask("Who receives it?")
        assert seen[0] == ("When is the report due?", []), seen
        assert seen[1] == ("Who receives it?", [("When is the report due?", "The report is due at 18:00.")]), seen
        # A new question carries no earlier turns: they would steer stock retrieval.
        # An unanswered question is not remembered.
        assistant.ask("Is there a radio schedule?")
        assert seen[2][1] == [], seen
        assert len(assistant._conversation_state().mission_turns) == 2
        # Follow-ups that name a camera, recording words, or a class without a
        # pronoun are left to the router; a pronoun keeps them with the mission.
        assert assistant._mission_route("What about the dog?", None) is None
        assert assistant._mission_route("And on camera 2?", None) is None
        assert assistant._mission_route("Were they seen today?", None) is None
        assert assistant._mission_route("And why is that?", None) == "followup"
        assert assistant._mission_route("Can they stop people?", None) == "followup"
        # New conversation (and a corpus change, which calls reset_context) forgets them.
        assistant.reset_context()
        assert assistant._mission_route("And why is that?", None) is None
        assistant.ask("When is the report due?")
        assert seen[-1][1] == []
    prompt = _mission_prompt("Who receives it?", [("When is the report due?", "Due at 18:00.")])
    assert prompt.startswith("Who receives it?\n") and "Earlier question: When is the report due?" in prompt
    assert "never as a source of facts" in prompt
    assert "Earlier question" not in _mission_prompt("Who receives it?")


def test_explain_mode_app_questions_use_the_guide_not_mission_documents():
    from app.assistant import Assistant
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        conn, _, metadata = _phase5_fixture(root)
        prompts = []
        def generate(prompt):
            prompts.append(prompt)
            return "YOLO26s on the NPU detects people; gvatrack tracks them."
        assistant = Assistant({"db_path": str(root / "phase5.db"),
            "detector_metadata_path": str(metadata), "streams": [
                {"stream_id": "cam1", "display_name": "Entrance"}]}, None, generate=generate)
        with assistant.conversation("e" * 32):
            assistant.ask("How is person detection working with Intel silicon?", mode="general")
        prompt = prompts[-1]
        assert "How detection works: " in prompt and "YOLO26s" in prompt and "NPU" in prompt
        assert "Do not use or mention the mission documents" in prompt
        assert "does not identify people, read colours" in prompt


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
            assert "Application guide summary" in prompts[-1] and len(prompts[-1]) < len(OPERATOR_GUIDE) + 2000
            assert "Test doorway." in prompts[-1] and '"selected_camera": "cam1"' in prompts[-1]
            assert "That detail isn't part of how this application is described." in prompts[-1]
            # A how-does-the-application question needs no routing call, even with record words.
            assert len(prompts) == 1
            assistant.ask("Explain that more simply.")
            assert len(prompts) == 2 and "How does this application store evidence?" in prompts[-1]
            assistant.ask("What are the limitations of this application?", mode="general")
            assert len(prompts) == 3 and "Application guide summary" in prompts[-1]
            answer = assistant.ask("How does the assistant answer questions about what was seen?")
            assert answer["intent"] == "application_help" and len(prompts) == 4, answer
            assert "DL Streamer Pipeline Server" in prompts[-1] and "- Camera history (what was seen):" in prompts[-1]
            assert "/api/v1/actions/execute" not in prompts[-1], "unrelated facts crowd the answer"
        routing = _routing_prompt("How do I search camera history?", ("person",), None, None)
        assert "application_help" in routing and "HOW to use a control" in routing
        brief = _brief_prompt("objective")
        assert OPERATOR_GUIDE not in brief, "guide text derails brief generation"
        assert "Never fill mission fields from the application guide" in brief
        assert '"Not specified"' in brief
        conn.close()


def test_colour_questions_get_a_qualified_answer_with_images_and_no_model_call():
    from app.assistant import Assistant
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        conn, _, metadata = _phase5_fixture(root)
        prompts = []
        def generate(prompt):
            prompts.append(prompt)
            return "It is a general answer."
        a = Assistant({"db_path": str(root / "phase5.db"), "detector_metadata_path": str(metadata)},
                      None, generate=generate)
        with a.conversation("b" * 32):
            car = a.ask("What is the color of the car detected in the last hour?", "cam1")
            assert not prompts, "a colour question reached the language model"
            assert car["intent"] == "video_history", car
            assert car["answer_text"].startswith("This application does not detect colour"), car
            assert car["query"]["filters"]["classes"] == ["car"] and car["query"]["total"] == 1, car
            plate = a.ask("Read the licence plate of the car", "cam1")
            assert plate["answer_text"].startswith("This application does not detect licence plates")
            assert "from the last 10 minutes" in plate["answer_text"]
            assert not prompts
            a.ask("What colour is the sky?", mode="general")
            assert len(prompts) == 1, "a colour question about no detected class was intercepted"
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
        # The broader search runs, and the answer leads with what it did not check.
        assert answer['query']['filters']['classes'] == ['person'] and answer['query']['filters']['streams'] == ['cam1']
        assert answer['answer_text'].startswith("I can't check wearing red shirts"), answer
        assert answer['query']['total'] == 1
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
        vague = a.ask('What happened?', 'cam1')
        assert vague['intent'] == 'video_history' and vague['answer_text'].startswith("You didn't give a time"), vague
        short = a.ask('red shirt in the last 10 minutes', 'cam1')
        assert short['query']['filters']['classes'] is None and 'recorded objects' in short['answer_text'], short

        responses = iter(['not JSON', json.dumps(proposal) + '\nUntrusted extra commentary.'])
        a.generate = lambda _: next(responses)
        recovered = a.ask('How many people wearing red shirts were detected in the last 10 minutes?', 'cam1')
        assert recovered['answer_text'].startswith("I can't check wearing red shirts"), recovered
        assert 'Untrusted extra' not in recovered['answer_text']
        a.generate = lambda _: 'not JSON'
        assert a.ask('How many people wearing red shirts were detected in the last 10 minutes?', 'cam1')['query']['filters']['classes'] == ['person']


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
        other = search({'surface_finish':'A42'},'cam2')
        assert other['missing_attributes'] == ['surface_finish'] and other['query']['filters']['attributes'] == []
        window = {'kind':'absolute','start_utc':_utc(now-timedelta(seconds=118)), 'end_utc':_utc(now-timedelta(seconds=100))}
        assert search({'upper_color':'red'},window=window)['query']['total'] == 0, 'old attributes leaked into later window'
        assert search({'invented_field':'x'})['missing_attributes'] == ['invented_field']
        unavailable = search({'upper_color':'red','invented_field':'x'})
        # Unavailable data is searched around and stated, never reported as a zero count.
        assert unavailable['missing_attributes'] == ['invented_field']
        assert unavailable['query']['filters']['attributes'] == [{'name':'upper_color','value':'red'}]
        assert unavailable['query']['filters']['classes'] == ['person'] and unavailable['query']['filters']['streams'] == ['cam1']
        assert unavailable['answer_text'].startswith('No invented_field data is recorded')
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
        assert search({'upper_color':'red'})['missing_attributes'] == ['upper_color']
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
            # The broader search is labelled as such before any count is given.
            assert answer["intent"] == "video_history", answer
            assert answer["query"]["filters"]["classes"] == ["person"], answer
            assert answer["answer_text"].startswith("I can't check"), answer
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


def test_deployed_component_config_matches_backend_expectations():
    """Static checks for the two settings a model or pipeline swap can desynchronise.

    Both failures used to appear only at runtime: an unstartable camera, and a
    missing response marker on the operator's first question.
    """
    import yaml
    from app.assistant import (
        InvalidInput, ServiceUnavailable, adapt_chat_response, resolve_answer_marker,
    )
    from app.pipelines import resolve_pipeline_name

    repo = Path(__file__).resolve().parent.parent
    for variant in ("cpu", "gpu", "npu"):
        config = repo / "config/dlsps" / variant / "config.json"
        name = resolve_pipeline_name(str(config))
        assert name, variant
        declared = [p["name"] for p in json.loads(config.read_text())["config"]["pipelines"]]
        assert name in declared, (
            f"{variant} variant would leave Backend requesting a pipeline it does not declare")

    template = yaml.safe_load(
        (repo / "config/chatqna/config.yaml").read_text())["model_settings"]["PROMPT_TEMPLATE"]
    marker = resolve_answer_marker(template)
    assert template.endswith(marker) and marker.strip()
    assert adapt_chat_response("echoed prompt" + marker + "Answer body.", marker) == "Answer body."
    for bad in ("Answer:\nunmarked", marker):
        try:
            adapt_chat_response(bad, marker)
        except ServiceUnavailable:
            pass
        else:
            raise AssertionError("an unmarked completion was exposed to the operator")
    try:
        resolve_answer_marker(template, "### Nothing like the template:\n")
    except InvalidInput:
        pass
    else:
        raise AssertionError("a marker unrelated to the template was accepted")


def test_brief_fields_are_generated_one_grounded_question_at_a_time():
    """A single JSON request never produced a brief against the stock 7B model."""
    from app.mission import (BRIEF_FIELDS, BRIEF_QUESTIONS, BRIEF_EXCLUSION, MissionService,
                             OPERATOR_GUIDE, _brief_prompt)
    from app.core import connect, migrate
    assert set(BRIEF_QUESTIONS) == set(BRIEF_FIELDS)
    for field in BRIEF_FIELDS:
        prompt = _brief_prompt(field)
        assert BRIEF_QUESTIONS[field] in prompt
        assert BRIEF_EXCLUSION in prompt
        assert '"Not specified"' in prompt
        assert OPERATOR_GUIDE not in prompt, "guide text derails brief generation"
        assert "JSON" not in prompt and "keys" not in prompt

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = root / "mission.db"
        conn = connect(str(db)); migrate(conn); conn.close()
        documents = set()
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"metadata": {"documents": sorted(documents)}})
            if request.method == "POST":
                name = request.content.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
                documents.add(name)
                return httpx.Response(200, json={"status": "Success"})
            return httpx.Response(204)
        services = types.SimpleNamespace(
            urls={"chatqna": "http://chatqna"},
            client=httpx.Client(transport=httpx.MockTransport(handler)))
        asked = []
        def generate(prompt):
            asked.append(prompt)
            if BRIEF_QUESTIONS["objective"] in prompt:
                return "  Hold the north gate.  "
            if BRIEF_QUESTIONS["risks"] in prompt:
                return "risk " * 400            # a wordy field must not lose the brief
            if BRIEF_QUESTIONS["locations"] in prompt:
                return "Two guards; relief not specified."
            if BRIEF_QUESTIONS["people_units"] in prompt:
                # Long, substantive, and merely mentions the sentinel phrase.
                return ("Two role players act as visitors and one acts as staff; the "
                        "relieving shift is not specified in the given context, so the "
                        "walk-through is conducted by the single handheld operator on duty.")
            return "Not specified"
        service = MissionService(
            {"db_path": str(db), "mission_sources_dir": str(root / "sources"),
             "mission_ingest_timeout_seconds": 1, "mission_ingest_poll_seconds": 0},
            services, generate=generate)
        service.upload("mission.pdf", _pdf_bytes())
        deadline = time.monotonic() + 5
        while service._brief_running and time.monotonic() < deadline:
            time.sleep(.01)
        body = service.brief()
        assert body["error"] is None, body["error"]
        fields = body["brief"]
        assert set(fields) == set(BRIEF_FIELDS)
        assert fields["objective"] == "Hold the north gate."
        assert fields["times"] == "Not specified"
        assert fields["locations"] == "Two guards; relief not specified.", "short known fact must survive"
        assert fields["people_units"].startswith("Two role players"), "substantive answer kept"
        assert fields["risks"] == ("risk " * 400).strip(), "full text remains available"
        assert len([p for p in asked if any(q in p for q in BRIEF_QUESTIONS.values())]) == 8


def test_brief_failures_are_named_for_the_operator():
    from app.assistant import LLM_NOT_ANSWERING, ServiceUnavailable
    from app.mission import MissionError, MissionUnavailable, brief_failure_message

    try:
        json.loads("")
    except ValueError as exc:
        decoded = exc
    cases = {
        decoded: "The language model returned an unusable answer.",
        MissionUnavailable("Mission text generation is unavailable."):
            "Mission text generation is unavailable.",
        MissionError("Mission Brief generation returned an invalid structure."):
            "Mission Brief generation returned an invalid structure.",
        TimeoutError(): "Brief generation timed out.",
        ServiceUnavailable("Chat Q&A connection failed"):
            "The language service is not responding.",
        ServiceUnavailable(LLM_NOT_ANSWERING): LLM_NOT_ANSWERING,
        RuntimeError("worker exploded"): "Brief generation failed unexpectedly.",
    }
    for exc, expected in cases.items():
        actual = brief_failure_message(exc)
        assert actual == expected, (type(exc).__name__, actual)
        assert "line 1 column 1" not in actual and "Traceback" not in actual
        assert actual and actual[0].isupper() and actual.endswith(".")


def test_priority_classes_survive_the_evidence_limit():
    from app import history
    from app.assistant import _utc

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        conn, cutoff, _metadata = _phase5_fixture(root)
        cfg = {"evidence_priority_classes": ["person"]}
        # Furniture observed more recently than the person, as on a real feed.
        for index in range(30):
            conn.execute(
                """INSERT INTO tracks (
                     track_id, stream_id, tracking_session_id, history_generation, tracker_id,
                     class_label, first_observed_utc, last_observed_utc, observation_count,
                     peak_confidence, confidence_freshness, closed_at, closure_reason)
                   VALUES (?, 'cam1', 'session-1', 'generation-1', ?, 'chair', ?, ?, 5, .9,
                           'tracker_metadata_unverified', ?, 'timeout')""",
                (5000 + index, 5000 + index,
                 _utc(cutoff - timedelta(minutes=2)), _utc(cutoff - timedelta(minutes=1)),
                 _utc(cutoff - timedelta(seconds=45))))
        conn.commit()

        result = history.recent(conn, None, cfg, "cam1", limit=5)
        labels = [track["class_label"] for track in result["tracks"]]
        assert "person" in labels, (
            "people were pushed below the result limit by more recent furniture")
        assert result["scope"]["priority_classes"] == ["person"]
        assert result["class_counts"].get("chair"), "non-priority classes must stay counted"
        assert result["matched_count"] > len(result["tracks"])

        # An explicit class filter is the operator's choice and overrides priority.
        chairs = history.recent(conn, None, cfg, "cam1", limit=5, class_labels=("chair",))
        assert {track["class_label"] for track in chairs["tracks"]} == {"chair"}
        assert chairs["scope"]["priority_classes"] == []

        # Recency alone buries the person: that is what the priority order fixes.
        unset = history.recent(conn, None, cfg, "cam1", limit=5, prioritize=False)
        assert "person" not in [track["class_label"] for track in unset["tracks"]]

        # Paging: consecutive offsets continue the same order without gaps or repeats.
        seen = []
        for offset in range(0, result["matched_count"] + 5, 5):
            page = history.recent(conn, None, cfg, "cam1", limit=5, offset=offset)
            assert page["scope"]["offset"] == offset
            assert page["truncated"] == (offset + 5 < result["matched_count"])
            seen += [track["track_id"] for track in page["tracks"]]
        everything = history.recent(conn, None, cfg, "cam1", limit=200)
        assert seen == [track["track_id"] for track in everything["tracks"]], seen
        assert history.recent(conn, None, cfg, "cam1", limit=5, offset=-3)["scope"]["offset"] == 0
        conn.close()


def test_profile_validation_and_required_source_startup():
    import hashlib
    from app.pipelines import resolve_pipeline_name, validate_detector_profile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        model = root / "model.xml"; model.write_text("test model")
        weights = root / "model.bin"; weights.write_bytes(b"test weights")
        metadata = root / "metadata.yaml"
        metadata.write_text("task: detect\nnames: {0: vehicle}\n")
        definition = {"name": "chosen", "pipeline": "gvadetect model=/models/model.xml device=CPU",
            "parameters": {"properties": {key: {"type": "string"}
                for key in ("crop-config", "model_instance")}}}
        pipeline = root / "pipeline.json"
        pipeline.write_text(json.dumps({"config": {"pipelines": [dict(definition, name="other", pipeline="gvadetect model=/different/model.xml"), definition]}}))
        profile = root / "profile.json"
        profile.write_text(json.dumps({"pipeline_model": "/models/model.xml", "classes": ["vehicle"],
            "artifact_sets": [{p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (model, weights)}]}))
        assert resolve_pipeline_name(str(pipeline), "chosen") == "chosen"
        validate_detector_profile(str(metadata), str(profile), str(pipeline), "chosen")
        for bad in ("absent", ""):
            try: resolve_pipeline_name(str(pipeline), bad)
            except ValueError: pass
            else: raise AssertionError("invalid override accepted")
        metadata.write_text("task: detect\nnames: {0: wrong}\n")
        try: validate_detector_profile(str(metadata), str(profile), str(pipeline), "chosen")
        except ValueError: pass
        else: raise AssertionError("mismatched taxonomy accepted")
        metadata.write_text("task: detect\nnames: {0: vehicle}\n")
        weights.write_bytes(b"different weights")
        try: validate_detector_profile(str(metadata), str(profile), str(pipeline), "chosen")
        except ValueError: pass
        else: raise AssertionError("mismatched weights accepted")
        del definition["parameters"]["properties"]["crop-config"]
        pipeline.write_text(json.dumps({"config": {"pipelines": [definition]}}))
        try: resolve_pipeline_name(str(pipeline), "chosen")
        except ValueError: pass
        else: raise AssertionError("missing crop contract accepted")
    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp)
        assert ing.initial_feed_reason(ing.started_at + 1) == "starting"
        assert ing.initial_feed_reason(ing.started_at + 181) == "no_signal_yet"
        ing.conn.close()
    from unittest.mock import patch
    import os
    with patch.dict(os.environ, {"BACKEND_DEMO_CAMERAS": "2"}):
        cfg = load_config(str(Path(__file__).resolve().parent.parent / "config/backend/backend.yaml"))
        assert [s["stream_id"] for s in cfg["streams"] if s["enabled"]] == ["cam1", "cam2"]



# -- Phase 8: rules, dispatch and the audio arbiter ---------------------------
class _SaidArbiter:
    def __init__(self):
        self.said = []
        self.cancelled = []

    def say(self, kind, text, **extra):
        self.said.append((kind, text, extra))
        return {"id": str(len(self.said))}

    def cancel(self, **target):
        self.cancelled.append(target)
        return 0


def _alerts(ing, db, arbiter=None, handler=None, streams=("cam1", "cam2")):
    from app.alerts import Alerts

    cfg = {"services": {"alert_agent": "http://agent"},
           "streams": [{"stream_id": s, "display_name": "Camera " + s[-1]} for s in streams]}
    client = httpx.Client(transport=httpx.MockTransport(
        handler or (lambda request: httpx.Response(200, json={"actions_taken": ["log_alert"]}))))
    alerts = Alerts(cfg, db, arbiter, lambda: ("person", "car"), client=client)
    if ing is not None:
        ing.alerts = alerts
    return alerts


def _observe(ing, stream_id, tracker_id, count=1, label="person", start=1000.0, **kw):
    for index in range(count):
        ing._on_message(None, None, types.SimpleNamespace(
            topic="detections/live",
            payload=_observation(stream_id, tracker_id, label, start + index * 0.1, **kw)))


def test_phase8_rule_fires_once_per_logical_track_and_restart_does_not_replay():
    from app.alerts import RuleError

    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp)
        db = str(Path(tmp) / "ingest.db")
        said = _SaidArbiter()
        alerts = _alerts(ing, db, said)
        rule = alerts.create(ing.conn, {"class_label": "person"})
        assert rule["stream_id"] is None and rule["name"] == "person on any camera", rule
        alerts.create(ing.conn, {"class_label": "person", "stream_id": "cam1", "speak": False})
        alerts.create(ing.conn, {"class_label": "person", "min_confidence": 0.95})
        for bad in ({"class_label": "tank"}, {"class_label": "person", "stream_id": "cam9"},
                    {"class_label": "person", "min_confidence": 2},
                    {"class_label": "person", "cooldown_seconds": True},
                    {"class_label": "person", "extra": 1}):
            try:
                alerts.create(ing.conn, bad)
            except RuleError:
                pass
            else:
                raise AssertionError(f"invalid rule accepted: {bad}")
        _observe(ing, "cam1", 1, count=40)
        _observe(ing, "cam2", 1, count=40, start=1000.05)
        _observe(ing, "cam2", 2, label="car", start=1005.0)
        rows = ing.conn.execute(
            "SELECT rule_id, track_id FROM rule_matches ORDER BY rule_id, track_id").fetchall()
        # any-camera rule on both tracks, cam1 rule on cam1 only, 0.95 rule never.
        assert len(rows) == 3, [tuple(r) for r in rows]
        assert [kind for kind, *_ in said.said] == ["alert", "alert"], said.said
        assert said.said[1][1] == "person on Camera 2.", said.said
        ing.conn.close()
        alerts.close()

        restarted = _ingest(tmp)
        again = _alerts(restarted, db, _SaidArbiter())
        restarted.recover()
        _observe(restarted, "cam1", 1, count=5, start=2000.0)
        count = restarted.conn.execute("SELECT count(*) FROM rule_matches").fetchone()[0]
        assert count == 3, "a restart replayed an alert for an already-matched track"
        assert again.arbiter.said == []
        restarted.conn.close()
        again.close()


def test_rule_options_validate_patch_clear_and_old_rows_load():
    from app.alerts import RuleError, list_rules

    good_zone = {"points": [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]]}
    good_hours = {"start": "08:00", "end": "17:30", "timezone": "Europe/Warsaw"}
    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp, cameras=("cam1",))
        alerts = _alerts(ing, str(Path(tmp) / "ingest.db"))
        rule = alerts.create(ing.conn, {
            "class_label": "person", "dwell_seconds": 10,
            "active_hours": good_hours, "zone": good_zone,
        })
        assert rule["dwell_seconds"] == 10 and rule["active_hours"] == good_hours
        assert rule["zone"] == good_zone, rule
        cleared = alerts.update(ing.conn, rule["rule_id"], {
            "dwell_seconds": None, "active_hours": None, "zone": None,
        })
        assert cleared["dwell_seconds"] is cleared["active_hours"] is cleared["zone"] is None

        for bad in (
            {"class_label": "person", "dwell_seconds": 0},
            {"class_label": "person", "dwell_seconds": 3601},
            {"class_label": "person", "active_hours": {"start": "08:00", "end": "08:00", "timezone": "Europe/Warsaw"}},
            {"class_label": "person", "active_hours": {"start": "08:00", "end": "09:00", "timezone": "No/SuchZone"}},
            {"class_label": "person", "zone": {"points": [[0, 0], [1, 1], [1, 1]]}},
            {"class_label": "person", "zone": {"points": [[0, 0], [0.0001, 0], [0.0002, 0]]}},
            {"class_label": "person", "zone": {"points": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.5, 0.95], [0.1, 0.9], [0.05, 0.5]]}},
            {"class_label": "person", "zone": {"points": [[0.1, 0.1], [0.9, 0.1], [0.1, 0.9], [0.9, 0.9]]}},
        ):
            try:
                alerts.create(ing.conn, bad)
            except RuleError as exc:
                assert str(exc), bad
            else:
                raise AssertionError(f"invalid rule accepted: {bad}")

        ing.conn.execute(
            """INSERT INTO rules (rule_id, name, class_label, enabled, created_utc, updated_utc, spec_json)
               VALUES ('old', 'old', 'person', 1, 't', 't', '{}')""")
        old = next(item for item in list_rules(ing.conn) if item["rule_id"] == "old")
        assert old["dwell_seconds"] is old["active_hours"] is old["zone"] is None
        ing.conn.close()
        alerts.close()


def test_busy_scene_is_announced_again_after_the_cooldown():
    from app import history
    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp)
        said = _SaidArbiter()
        alerts = _alerts(ing, str(Path(tmp) / "ingest.db"), said)
        alerts.create(ing.conn, {"class_label": "person", "cooldown_seconds": 30})
        now = time.time()
        _observe(ing, "cam2", 1, start=now)
        _observe(ing, "cam2", 2, start=now + 1)
        assert len(said.said) == 1, said.said
        # Sightings keep arriving (last match is recent), but the cooldown has
        # passed since the announcement: the next new object is announced.
        ing.conn.execute("UPDATE rule_matches SET matched_utc='2000-01-01T00:00:00Z' "
                         "WHERE dispatch_state='pending'")
        ing.conn.commit()
        _observe(ing, "cam2", 3, start=now + 2)
        assert len(said.said) == 2, "busy scene stayed silent past the cooldown"
        _observe(ing, "cam2", 4, start=now + 3)
        assert len(said.said) == 2, "a re-announcement restarts the cooldown"
        (incident,) = history.incidents(ing.conn)
        assert incident["match_count"] == 4 and incident["state"] == "open", incident
        ing.conn.close()
        alerts.close()


def test_new_kind_of_object_and_other_camera_are_announced_within_the_cooldown():
    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp)
        said = _SaidArbiter()
        alerts = _alerts(ing, str(Path(tmp) / "ingest.db"), said)
        alerts.create(ing.conn, {"cooldown_seconds": 300})  # Anything
        now = time.time()
        _observe(ing, "cam1", 1, label="car", start=now)
        _observe(ing, "cam1", 2, label="person", start=now + 1)
        assert [t for _, t, _ in said.said] == ["car on Camera 1.", "person on Camera 1."], said.said
        _observe(ing, "cam1", 3, label="car", start=now + 2)
        _observe(ing, "cam1", 4, label="person", start=now + 3)
        assert len(said.said) == 2, "a kind already announced waits for the cooldown"
        # Same rule, other camera: its own incident, spoken without a rule cooldown.
        _observe(ing, "cam2", 5, label="person", start=now + 4)
        assert len(said.said) == 3 and "cooldown" not in said.said[-1][2], said.said
        ing.conn.close()
        alerts.close()


def test_zone_rule_accepts_objects_touching_any_frame_edge():
    zone = {"points": [[0.3, 0.3], [0.7, 0.3], [0.7, 1.0], [0.3, 1.0]]}
    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp, cameras=("cam1",))
        alerts = _alerts(ing, str(Path(tmp) / "ingest.db"))
        alerts.create(ing.conn, {"class_label": "person", "zone": zone})
        rect = {"frame_width": 100, "frame_height": 100}
        # A tall, close person cut by the top edge: feet are inside the zone.
        _observe(ing, "cam1", 1, detection_rect=[45, 0, 10, 50], object_touches_frame=True, **rect)
        assert ing.conn.execute("SELECT count(*) FROM rule_matches").fetchone()[0] == 1
        # Cut by the bottom edge: counted like any other detection.
        _observe(ing, "cam1", 2, start=1100.0, detection_rect=[45, 50, 10, 49.5],
                 object_touches_frame=True, **rect)
        assert ing.conn.execute("SELECT count(*) FROM rule_matches").fetchone()[0] == 2
        ing.conn.close()
        alerts.close()


def test_zone_hysteresis_and_frame_touching_ignore():
    zone = {"points": [[0.4, 0.4], [0.6, 0.4], [0.6, 0.6], [0.4, 0.6]]}
    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp, cameras=("cam1",))
        alerts = _alerts(ing, str(Path(tmp) / "ingest.db"))
        alerts.create(ing.conn, {"class_label": "person", "zone": zone})
        rect = {"frame_width": 100, "frame_height": 100}
        _observe(ing, "cam1", 1, detection_rect=[70, 40, 10, 10], **rect)
        assert ing.conn.execute("SELECT count(*) FROM rule_matches").fetchone()[0] == 0
        _observe(ing, "cam1", 2, start=1000.2, detection_rect=[45, 40, 10, 10], **rect)
        assert ing.conn.execute("SELECT count(*) FROM rule_matches").fetchone()[0] == 1

        alerts.create(ing.conn, {"class_label": "person", "zone": zone, "dwell_seconds": 3})
        _observe(ing, "cam1", 3, start=2000.0, detection_rect=[45, 40, 10, 10], **rect)
        _observe(ing, "cam1", 3, start=2001.0, detection_rect=[45, 40, 10, 10], **rect)
        _observe(ing, "cam1", 3, start=2002.5, detection_rect=[60, 40, 2, 10], **rect)
        _observe(ing, "cam1", 3, start=2003.1, detection_rect=[60, 40, 2, 10], **rect)
        assert ing.conn.execute(
            "SELECT count(*) FROM rule_matches WHERE track_id=(SELECT track_id FROM tracks WHERE tracker_id=3)"
        ).fetchone()[0] == 2, "zone dwell should survive edge jitter"
        ing.conn.close()
        alerts.close()


def test_incident_in_view_follows_live_matched_objects_and_zone():
    from app import history
    zone = {"points": [[0.4, 0.4], [0.6, 0.4], [0.6, 0.6], [0.4, 0.6]]}
    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp, cameras=("cam1",))
        alerts = _alerts(ing, str(Path(tmp) / "ingest.db"))
        alerts.create(ing.conn, {"class_label": "person", "zone": zone})
        rect = {"frame_width": 100, "frame_height": 100}
        _observe(ing, "cam1", 1, start=1000.0, detection_rect=[45, 40, 10, 10], **rect)

        def in_view(cutoff):
            rows = history.incidents(ing.conn)
            alerts.mark_in_view(rows, ing, cutoff)
            return rows[0]["in_view"]
        assert in_view(995.0), "matched object inside the zone is not in view"
        assert not in_view(1005.0), "an object last seen before the window is still in view"
        # Still on camera but walked out of the zone: the alert has fired, not firing.
        _observe(ing, "cam1", 1, start=1001.0, detection_rect=[80, 40, 10, 10], **rect)
        assert not in_view(995.0), "object outside the zone counted as in view"
        _observe(ing, "cam1", 1, start=1002.0, detection_rect=[45, 40, 10, 10], **rect)
        assert in_view(995.0)
        # "Now" is the camera's newest frame, not the wall clock: once later
        # frames no longer show the object, it has left even though its last
        # sighting is still inside the freshness window.
        _observe(ing, "cam1", 2, start=1003.0, label="car")
        assert in_view(995.0), "a one-second detection miss ended in view"
        _observe(ing, "cam1", 2, start=1003.4, label="car")
        assert not in_view(995.0), "object gone from newer frames is still in view"
        # The tracker re-identifies the same object under a new id: still in
        # view, even before that track has matched (here a dwell rule).
        (incident,) = history.incidents(ing.conn)
        ing.conn.commit()
        assert alerts.update(ing.conn, incident["rule_id"], {"dwell_seconds": 60})
        _observe(ing, "cam1", 7, start=1003.5, detection_rect=[45, 40, 10, 10], **rect)
        assert in_view(995.0), "a not-yet-matched object meeting the rule is not in view"
        ing.conn.commit()
        (incident,) = history.incidents(ing.conn)
        assert alerts.act(incident["incident_id"], "dismiss")
        assert not in_view(995.0), "a closed alert is in view"
        ing.conn.close()
        alerts.close()


def test_zone_uses_real_dlsps_message_shape():
    # Live DLSPS: pixel box as x/y/w/h on the object, frame size as metadata width/height.
    zone = {"points": [[0.4, 0.4], [0.6, 0.4], [0.6, 0.6], [0.4, 0.6]]}
    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp, cameras=("cam1",))
        alerts = _alerts(ing, str(Path(tmp) / "ingest.db"))
        alerts.create(ing.conn, {"class_label": "person", "zone": zone})
        for tracker_id, x in ((1, 1400), (2, 900)):
            message = json.loads(_observation("cam1", tracker_id, observed_at=1000.0 + tracker_id))
            message["metadata"].update(width=1920, height=1080)
            message["metadata"]["objects"][0].update(x=x, y=300, w=100, h=250)
            ing._on_message(None, None, types.SimpleNamespace(
                topic="detections/live", payload=json.dumps(message)))
        rows = ing.conn.execute(
            "SELECT t.tracker_id FROM rule_matches m JOIN tracks t USING (track_id)").fetchall()
        assert [row[0] for row in rows] == [2], rows
        ing.conn.close()
        alerts.close()


def test_dwell_brief_gap_survives_and_long_gap_resets():
    zone = {"points": [[0.4, 0.4], [0.6, 0.4], [0.6, 0.6], [0.4, 0.6]]}
    rect = {"frame_width": 100, "frame_height": 100}
    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp, cameras=("cam1",))
        alerts = _alerts(ing, str(Path(tmp) / "ingest.db"))
        alerts.create(ing.conn, {"class_label": "person", "zone": zone, "dwell_seconds": 3})
        _observe(ing, "cam1", 1, start=1000.0, detection_rect=[45, 40, 10, 10], **rect)
        _observe(ing, "cam1", 1, start=1001.2, detection_rect=[45, 40, 10, 10], **rect)
        _observe(ing, "cam1", 1, start=1002.0, detection_rect=[75, 40, 10, 10], **rect)
        _observe(ing, "cam1", 1, start=1003.1, detection_rect=[45, 40, 10, 10], **rect)
        assert ing.conn.execute("SELECT count(*) FROM rule_matches").fetchone()[0] == 1

        _observe(ing, "cam1", 2, start=2000.0, detection_rect=[45, 40, 10, 10], **rect)
        _observe(ing, "cam1", 2, start=2002.5, detection_rect=[75, 40, 10, 10], **rect)
        _observe(ing, "cam1", 2, start=2003.1, detection_rect=[45, 40, 10, 10], **rect)
        assert ing.conn.execute("SELECT count(*) FROM rule_matches").fetchone()[0] == 1
        _observe(ing, "cam1", 2, start=2004.5, detection_rect=[45, 40, 10, 10], **rect)
        _observe(ing, "cam1", 2, start=2006.2, detection_rect=[45, 40, 10, 10], **rect)
        assert ing.conn.execute("SELECT count(*) FROM rule_matches").fetchone()[0] == 2
        ing.conn.close()
        alerts.close()


def test_active_hours_daytime_and_overnight_windows():
    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp, cameras=("cam1",))
        alerts = _alerts(ing, str(Path(tmp) / "ingest.db"))
        alerts.create(ing.conn, {"class_label": "person", "active_hours": {
            "start": "10:00", "end": "12:00", "timezone": "Europe/Warsaw"}})
        inside = datetime(2026, 1, 1, 10, 30, tzinfo=UTC).timestamp()  # 11:30 Warsaw
        outside = datetime(2026, 1, 1, 8, 30, tzinfo=UTC).timestamp()  # 09:30 Warsaw
        _observe(ing, "cam1", 1, start=outside)
        assert ing.conn.execute("SELECT count(*) FROM rule_matches").fetchone()[0] == 0
        _observe(ing, "cam1", 2, start=inside)
        assert ing.conn.execute("SELECT count(*) FROM rule_matches").fetchone()[0] == 1

        alerts.create(ing.conn, {"class_label": "person", "active_hours": {
            "start": "23:00", "end": "02:00", "timezone": "Europe/Warsaw"}})
        overnight = datetime(2026, 1, 1, 22, 30, tzinfo=UTC).timestamp()  # 23:30 Warsaw
        _observe(ing, "cam1", 3, start=overnight)
        assert ing.conn.execute("SELECT count(*) FROM rule_matches").fetchone()[0] == 2
        ing.conn.close()
        alerts.close()


def test_classification_seen_before_track_admission_is_kept_at_its_own_time():
    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp, cameras=("cam1",), min_track_observations=3)
        start = time.time() - 30

        def frame(offset, attributes):
            body = json.loads(_observation("cam1", 7, observed_at=start + offset))
            body["metadata"]["objects"][0].update({name: {"label": value, "confidence": .9}
                                                   for name, value in attributes.items()})
            ing._on_message(None, None, types.SimpleNamespace(
                topic="detections/live", payload=json.dumps(body).encode()))
        frame(0, {"upper_color": "red"})
        frame(1, {})
        assert ing.conn.execute("SELECT count(*) FROM attribute_samples").fetchone()[0] == 0
        frame(2, {})
        ing.conn.commit()
        samples = ing.conn.execute("SELECT * FROM attribute_samples").fetchall()
        assert len(samples) == 1, [dict(r) for r in samples]
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start))
        assert samples[0]["started_utc"] == samples[0]["ended_utc"] == stamp, dict(samples[0])
        assert samples[0]["observation_count"] == 1 and json.loads(samples[0]["attributes_json"]) == {"upper_color": "red"}
        ing.conn.close()


def test_tracks_shorter_than_three_observations_are_never_stored_shown_or_alerted():
    from app import history

    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp, cameras=("cam1",), min_track_observations=3)
        alerts = _alerts(ing, str(Path(tmp) / "ingest.db"), _SaidArbiter())
        alerts.create(ing.conn, {"class_label": "person"})
        now = time.time()
        _observe(ing, "cam1", 1, count=2, start=now, confidence=0.8)
        assert ing.conn.execute("SELECT count(*) FROM tracks").fetchone()[0] == 0
        assert history.current(ing.conn, ing, ing.cfg, "cam1")["tracks"] == []
        assert ing.conn.execute("SELECT count(*) FROM rule_matches").fetchone()[0] == 0
        _observe(ing, "cam1", 1, start=now + 0.2, confidence=0.9)
        row = ing.conn.execute("SELECT first_observed_utc, observation_count, peak_confidence "
                               "FROM tracks").fetchone()
        assert row["observation_count"] == 3 and row["peak_confidence"] == 0.9, dict(row)
        assert row["first_observed_utc"] == time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), dict(row)
        assert ing.conn.execute("SELECT count(*) FROM rule_matches").fetchone()[0] == 1
        ing._flush_intervals(force=True)
        spans = ing.conn.execute("SELECT sum(observation_count) FROM track_intervals").fetchone()[0]
        assert spans == 3, spans
        _observe(ing, "cam1", 2, count=2, start=now + 0.3)
        assert len(ing.tentative) == 1
        for seed in ing.tentative.values():
            seed["last_at"] -= 60
        ing._close_timed_out()
        assert not ing.tentative and ing.counters["short_tracks_discarded"] == 1, (ing.tentative, ing.counters)
        ing.conn.close()
        alerts.close()


def test_alert_latency_stages_and_alerted_crop_is_committed_and_pushed_at_once():
    from app.main import _latency

    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp, cameras=("cam1",), min_track_observations=3)
        db = str(Path(tmp) / "ingest.db")
        alerts = _alerts(ing, db, _SaidArbiter(), streams=("cam1",))
        alerts.create(ing.conn, {"class_label": "person"})
        now = time.time()
        _observe(ing, "cam1", 1, count=3, start=now)
        match_id, record = next(iter(alerts.latency.items()))
        assert record["detected_at"] == now and record["qualified_at"] == now + 0.2, record
        assert record["matched_at"] >= now and alerts.version == 1, (record, alerts.version)

        other = connect(db)
        visible = lambda: other.execute("SELECT count(*) FROM candidates").fetchone()[0]
        _observe(ing, "cam1", 2, count=3, label="car", start=now + 0.3)
        _send(ing, _crop("cam1", 2, "b" * 32, observed_at=now + 0.3))
        assert visible() == 0 and alerts.version == 1, "unalerted crops wait for the batch commit"
        _send(ing, _crop("cam1", 1, "c" * 32, observed_at=now + 0.3))
        assert visible() == 2 and alerts.version == 2, (visible(), alerts.version)
        other.close()

        speech = {"playing": now + 0.5, "playback_started": now + 0.7}
        state = types.SimpleNamespace(alerts=alerts, ingest=ing,
                                      arbiter=types.SimpleNamespace(timing=lambda ref: speech))
        latency = _latency(state, match_id)
        assert latency["detected_at"] == now, latency
        seconds = latency["seconds"]
        assert seconds["qualified"] == 0.2 and seconds["voice_ready"] == 0.5, seconds
        assert seconds["voice_started"] == 0.7 and 0 <= seconds["image_ready"] < 5, seconds
        assert _latency(state, "unknown") is None and _latency(state, None) is None
        # A grouped sighting's crop keeps the alert's first image time, and one
        # whose crop came before its match makes a new alert's image available at once.
        _observe(ing, "cam1", 3, count=3, start=now + 1)
        _send(ing, _crop("cam1", 3, "d" * 32, observed_at=now + 1.2))
        assert _latency(state, match_id)["seconds"]["image_ready"] == seconds["image_ready"]
        from app import history
        _observe(ing, "cam1", 4, count=3, start=now + 1.5)
        ing.conn.commit()
        incident = next(i for i in history.incidents(ing.conn) if i["head_match_id"] == match_id)
        assert incident["track"]["track_id"] == 4 and incident["track"]["snip_state"] == "none", incident
        assert incident["track"]["snip_url"] is None, "the track never claims another track's image"
        assert "/api/tracks/3/snip" in (incident["snip_url"] or ""), "new sighting without a crop keeps the last image"
        assert alerts.act(incident["incident_id"], "acknowledge")
        _observe(ing, "cam1", 5, count=3, start=now + 1.8)
        reopened = next(r for r in alerts.latency.values() if r["detected_at"] == now + 1.8)
        assert reopened["image_at"] >= reopened["matched_at"], "a reopened alert shows the earlier image"
        alerts.create(ing.conn, {"class_label": "car"})
        _observe(ing, "cam1", 2, start=now + 2, label="car")
        car = next(m for m, r in alerts.latency.items() if r["detected_at"] == now + 0.3)
        assert alerts.latency[car]["image_at"] >= alerts.latency[car]["matched_at"]
        ing.conn.close()
        alerts.close()


def test_alert_incidents_group_matches_and_follow_operator_actions():
    from app import history
    from app.alerts import RuleError

    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp)
        db = str(Path(tmp) / "ingest.db")
        said = _SaidArbiter()
        alerts = _alerts(ing, db, said)
        rule = alerts.create(ing.conn, {"class_label": "person", "cooldown_seconds": 30})
        now = time.time()
        # Three tracks on one camera: one incident, one spoken alert, one dispatch.
        for tracker in (1, 2, 3):
            _observe(ing, "cam2", tracker, start=now + tracker)
        (incident,) = history.incidents(ing.conn)
        assert incident["match_count"] == 3 and incident["state"] == "open", incident
        assert incident["track"]["stream_id"] == "cam2" and incident["rule_name"] == rule["name"]
        assert len(said.said) == 1, said.said
        states = sorted(r[0] for r in ing.conn.execute("SELECT dispatch_state FROM rule_matches"))
        assert states == ["grouped", "grouped", "pending"], states
        sightings = history.incident_tracks(ing.conn, incident["incident_id"])
        assert len(sightings) == 3 and {t["stream_id"] for t in sightings} == {"cam2"}, sightings
        assert history.incident_tracks(ing.conn, "unknown") is None
        # Another camera is its own incident.
        _observe(ing, "cam1", 9, start=now + 5)
        assert len(history.incidents(ing.conn)) == 2 and len(said.said) == 2
        cam1 = next(i for i in history.incidents(ing.conn) if i["stream_id"] == "cam1")

        # Once the cooldown has passed since the announcement, a new sighting
        # re-announces an unhandled incident.
        ing.conn.execute("UPDATE alert_incidents SET last_match_utc='2000-01-01T00:00:00Z' "
                         "WHERE incident_id=?", (incident["incident_id"],))
        ing.conn.execute("UPDATE rule_matches SET matched_utc='2000-01-01T00:00:00Z' "
                         "WHERE match_id=(SELECT head_match_id FROM alert_incidents "
                         "WHERE incident_id=?)", (incident["incident_id"],))
        _observe(ing, "cam2", 4, start=now + 6)
        assert len(said.said) == 3 and said.said[-1][1] == "person on Camera 2.", said.said

        try:
            alerts.act(incident["incident_id"], "snooze", 7)
        except RuleError:
            pass
        else:
            raise AssertionError("unsupported snooze length accepted")
        assert alerts.act(incident["incident_id"], "snooze", 5)
        snoozed = next(i for i in history.incidents(ing.conn) if i["incident_id"] == incident["incident_id"])
        assert snoozed["state"] == "snoozed" and snoozed["snoozed_until_utc"], snoozed
        ing.conn.execute("UPDATE alert_incidents SET last_match_utc='2000-01-01T00:00:00Z' "
                         "WHERE incident_id=?", (incident["incident_id"],))
        _observe(ing, "cam2", 5, start=now + 7)
        assert len(said.said) == 3, "a snoozed incident spoke"
        assert alerts.close_quiet(now + 10) == 0, "snoozed incident closed before its snooze ended"
        assert said.cancelled[-1] == {"group": incident["incident_id"]}, "snooze left its speech queued"
        assert said.said[-1][2]["group"] == incident["incident_id"]
        # An operator snooze that lands while quiet-close runs must survive it.
        original = alerts.writer

        def snooze_first(operation):
            alerts.writer = original
            alerts.act(incident["incident_id"], "snooze", 60)
            return original(operation)
        alerts.writer = snooze_first
        assert alerts.close_quiet(now + 400) == 0, "quiet close overwrote a fresh snooze"
        alerts.writer = original
        assert alerts.close_quiet(now + 3700) == 1

        # Acknowledge stops nothing but the badge; the incident closes once quiet.
        assert alerts.act(cam1["incident_id"], "acknowledge")
        assert not alerts.act(cam1["incident_id"], "acknowledge"), "acknowledged twice"
        assert alerts.close_quiet(now + 5) == 0
        assert alerts.close_quiet(now + 60) == 1
        # A later sighting opens a fresh incident, which speaks again.
        _observe(ing, "cam1", 10, start=now + 8)
        fresh = next(i for i in history.incidents(ing.conn) if i["state"] == "open")
        assert fresh["incident_id"] != cam1["incident_id"] and len(said.said) == 4
        assert alerts.act(fresh["incident_id"], "false_alarm")
        closed = next(i for i in history.incidents(ing.conn) if i["incident_id"] == fresh["incident_id"])
        assert closed["state"] == "closed" and closed["closure"] == "false_alarm", closed
        assert not alerts.act(fresh["incident_id"], "dismiss")

        # Acknowledge means "seen so far": the count restarts and the very next
        # sighting needs attention again, spoken despite the rule cooldown.
        _observe(ing, "cam2", 11, start=now + 9)
        live = next(i for i in history.incidents(ing.conn) if i["state"] == "open")
        spoken = len(said.said)
        assert alerts.act(live["incident_id"], "acknowledge")
        acked = next(i for i in history.incidents(ing.conn) if i["incident_id"] == live["incident_id"])
        assert acked["match_count"] == 0 and acked["counted_from_utc"] == acked["acknowledged_utc"], acked
        ing.conn.execute("UPDATE alert_incidents SET counted_from_utc='2000-01-01T00:00:00Z' "
                         "WHERE incident_id=?", (live["incident_id"],))
        _observe(ing, "cam2", 12, start=now + 10)
        seen = next(i for i in history.incidents(ing.conn) if i["incident_id"] == live["incident_id"])
        assert seen["state"] == "open" and seen["match_count"] == 1 and seen["acknowledged_utc"] is None, seen
        assert seen["counted_from_utc"] == seen["last_match_utc"] != "2000-01-01T00:00:00Z", \
            "span did not restart at the first sighting after Acknowledge"
        assert len(said.said) == spoken + 1 and said.said[-1][2].get("cooldown", 0.0) == 0.0, said.said[-1]
        assert [t["track_id"] for t in history.incident_tracks(ing.conn, live["incident_id"])] == \
            [seen["track"]["track_id"]], "sightings before Acknowledge still listed"
        # Sightings while open are grouped; the span start stays at the first one.
        _observe(ing, "cam2", 13, start=now + 11)
        again = next(i for i in history.incidents(ing.conn) if i["incident_id"] == live["incident_id"])
        assert again["match_count"] == 2 and again["counted_from_utc"] == seen["counted_from_utc"], again
        assert len(said.said) == spoken + 1 and len(history.incident_tracks(ing.conn, live["incident_id"])) == 2
        # A snooze holds sightings back until it ends.
        assert alerts.act(live["incident_id"], "snooze", 5)
        _observe(ing, "cam2", 15, start=now + 12)
        held = next(i for i in history.incidents(ing.conn) if i["incident_id"] == live["incident_id"])
        assert held["state"] == "snoozed" and held["match_count"] == 1 and len(said.said) == spoken + 1, held
        ing.conn.execute("UPDATE alert_incidents SET snoozed_until_utc='2000-01-01T00:00:00Z' "
                         "WHERE incident_id=?", (live["incident_id"],))
        _observe(ing, "cam2", 16, start=now + 13)
        woke = next(i for i in history.incidents(ing.conn) if i["incident_id"] == live["incident_id"])
        assert woke["state"] == "open" and len(said.said) == spoken + 2, woke

        # Operator test speech repeats the announcement without a rule cooldown or match ref.
        assert alerts.speak_test(ing.conn, live["incident_id"])["id"]
        assert said.said[-1][:2] == ("alert", "person on Camera 2.") \
            and said.said[-1][2] == {"bypass_alert_floor": True}, said.said[-1]
        assert alerts.speak_test(ing.conn, "unknown") is None
        # Audio turned on later: the alerts still open are announced once.
        open_now = [i for i in history.incidents(ing.conn) if i["state"] == "open"]
        assert alerts.speak_open(ing.conn)["id"] and said.said[-1][2] == {"bypass_alert_floor": True}
        expected = "person on Camera 2." if len(open_now) == 1 else f"{len(open_now)} alerts need attention: "
        assert said.said[-1][1].startswith(expected), (said.said[-1], open_now)
        # Remove drops the incident; matches stay and the next sighting opens a new one.
        matches = ing.conn.execute("SELECT count(*) FROM rule_matches").fetchone()[0]
        assert alerts.remove(live["incident_id"]) and not alerts.remove(live["incident_id"])
        assert all(i["incident_id"] != live["incident_id"] for i in history.incidents(ing.conn))
        assert ing.conn.execute("SELECT count(*) FROM rule_matches").fetchone()[0] == matches
        _observe(ing, "cam2", 14, start=now + 14)
        assert any(i["state"] == "open" and i["incident_id"] != live["incident_id"]
                   for i in history.incidents(ing.conn))
        # Disabling the rule closes anything it left open and withdraws its speech.
        alerts.update(ing.conn, rule["rule_id"], {"enabled": False})
        assert said.cancelled[-1] == {"rule_id": rule["rule_id"]}
        assert all(i["state"] == "closed" for i in history.incidents(ing.conn))
        heard = len(said.said)
        assert alerts.speak_open(ing.conn) is None and len(said.said) == heard, "nothing open, nothing said"
        ing.conn.close()
        alerts.close()


def test_tracker_predicted_confidence_never_becomes_peak_or_matches_threshold_rules():
    # Stock short-term gvatrack reports 1.0 on frames where gvadetect skipped inference.
    from app import pipelines

    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "config.json"
        saved = pipelines.PIPELINE, pipelines.PREDICTED_CONFIDENCE
        for tracker, expected in (("short-term-imageless", 1.0), ("zero-term-imageless", None)):
            config.write_text(json.dumps({"config": {"pipelines": [{"name": "p", "pipeline":
                f"src ! gvadetect model=m.xml ! gvatrack tracking-type={tracker} config=a=b ! sink"}]}}))
            assert pipelines.tracker_predicted_confidence(str(config), "p") == expected, tracker
            pipelines.configure("p", str(config))
            crop = json.loads(pipelines._request_body({"source_uri": "rtsp://x", "annotated_path": "a",
                                                       "stream_id": "cam1"})["parameters"]["crop-config"])
            assert crop.get("predicted_confidence") == expected, crop
            assert crop["config"].endswith("/template.json") and crop["stream_id"] == "cam1", crop
        pipelines.PIPELINE, pipelines.PREDICTED_CONFIDENCE = saved
        ing = _ingest(tmp)
        ing.predicted_confidence = 1.0
        alerts = _alerts(ing, str(Path(tmp) / "ingest.db"), _SaidArbiter())
        alerts.create(ing.conn, {"class_label": "person", "min_confidence": 0.95})
        _observe(ing, "cam1", 1, confidence=0.6)
        _observe(ing, "cam1", 1, count=5, start=1000.1, confidence=1.0)
        peak = ing.conn.execute("SELECT peak_confidence FROM tracks").fetchone()[0]
        assert peak == 0.6, peak
        assert ing.conn.execute("SELECT count(*) FROM rule_matches").fetchone()[0] == 0
        # Without a predicting tracker a genuine near-one detector score is kept.
        ing.predicted_confidence = None
        _observe(ing, "cam1", 2, start=1001.0, confidence=0.99995)
        peak = ing.conn.execute("SELECT peak_confidence FROM tracks WHERE tracker_id=2").fetchone()[0]
        assert peak == 0.99995, peak
        assert ing.conn.execute("SELECT count(*) FROM rule_matches").fetchone()[0] == 1
        ing.conn.close()
        alerts.close()


def test_phase8_dispatch_distinguishes_delivered_suppressed_and_failed_with_backoff():
    from app import alerts as alerting

    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp, cameras=("cam1", "cam2", "cam3"))
        db = str(Path(tmp) / "ingest.db")
        replies, requests = [], []

        def handle(request):
            requests.append(json.loads(request.content))
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        alerts = _alerts(ing, db, None, handle, streams=("cam1", "cam2", "cam3"))
        alerts.create(ing.conn, {"class_label": "person", "cooldown_seconds": 45})
        _observe(ing, "cam1", 1)
        _observe(ing, "cam2", 1, start=1000.05)
        # Same-camera repeats are grouped into one incident, so use a third camera.
        _observe(ing, "cam3", 2, start=1000.1)
        ing.conn.commit()
        replies.extend([httpx.Response(200, json={"actions_taken": ["log_alert"]}),
                        httpx.Response(200, json={"actions_taken": []}),
                        httpx.ConnectError("agent down")])
        for row in alerts._due():
            alerts.dispatch(row)
        states = [r["dispatch_state"] for r in alerts.conn.execute(
            "SELECT dispatch_state FROM rule_matches ORDER BY matched_utc, rowid")]
        assert states == ["delivered", "suppressed", "failed"], states
        body = requests[0]["data"]
        assert body["alert_name"].startswith("HANDHELD_R") and body["answer"] == "YES"
        assert body["dedup"]["fields"] == ["metadata.track_id"]
        assert body["dedup"]["window_seconds"] == 45
        assert body["event_id"] == body["metadata"]["match_id"]
        assert alerts._due() == [], "a failed match was retried before its backoff"
        failed = alerts.conn.execute(
            "SELECT match_id FROM rule_matches WHERE dispatch_state='failed'").fetchone()[0]
        for attempt in range(2, alerting.MAX_ATTEMPTS + 1):
            alerts.conn.execute("UPDATE rule_matches SET last_attempt_utc='2000-01-01T00:00:00Z'")
            due = alerts._due()
            assert [r["match_id"] for r in due] == [failed], attempt
            replies.append(httpx.Response(500))
            assert alerts.dispatch(due[0]) == "failed"
        alerts.conn.execute("UPDATE rule_matches SET last_attempt_utc='2000-01-01T00:00:00Z'")
        assert alerts._due() == [], "retries continued past the attempt cap"
        assert not alerts.retry(alerts.conn, "unknown")
        assert alerts.retry(alerts.conn, failed)
        replies.append(httpx.Response(200, json={"actions_taken": ["log_alert"]}))
        assert alerts.dispatch(alerts._due()[0]) == "delivered"
        # A process that died mid-request is resent; settled rows never are.
        alerts.conn.execute("UPDATE rule_matches SET dispatch_state='dispatching' WHERE match_id=?",
                            (failed,))
        alerts.conn.commit()
        alerts.stopping.set()  # recovery runs; the worker threads exit at once
        alerts.start()
        state = alerts.conn.execute("SELECT dispatch_state FROM rule_matches WHERE match_id=?",
                                    (failed,)).fetchone()[0]
        assert state == "pending", state
        ing.conn.close()
        alerts.close()


def _wav(seconds):
    import wave
    buffer = BytesIO()
    with wave.open(buffer, "wb") as clip:
        clip.setnchannels(1); clip.setsampwidth(2); clip.setframerate(16000)
        clip.writeframes(b"\0\0" * int(16000 * seconds))
    return buffer.getvalue()


def _arbiter(durations=None, fail=False):
    from app import speech

    speech.PLAYBACK_MARGIN_SECONDS = 0.0

    def synthesize(text):
        if fail:
            raise httpx.ConnectError("tts down")
        return _wav((durations or {}).get(text, 0.2))

    arbiter = speech.Arbiter(synthesize, alert_floor=0.1)
    arbiter.start()
    return arbiter


def _settle(arbiter, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = arbiter.snapshot()
        if snap["current"] is None and not snap["queued"] and snap["preparing"] is None:
            return
        time.sleep(0.02)
    raise AssertionError("arbiter did not settle")


def test_phase8_arbiter_never_overlaps_and_preempts_by_priority():
    arbiter = _arbiter({"long alert": 1.0})
    try:
        arbiter.say("alert", "long alert", ref="a1", rule_id="r1", cooldown=0)
        deadline = time.monotonic() + 3
        while (arbiter.snapshot()["current"] or {}).get("ref") != "a1":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        # The browser plays the stock TTS base64 WAV straight from the speech state.
        clip = base64.b64decode(arbiter.snapshot()["current"]["audio_base64"])
        assert clip == _wav(1.0) and not hasattr(arbiter, "clip")
        # The operator's answer outranks the alert: the alert yields, then replays.
        arbiter.say("answer", "your answer")
        _settle(arbiter)
        assert any(e["event"] == "deferred" and e["ref"] == "a1" for e in arbiter.trace)
        assert arbiter.outcome("a1") == "unconfirmed"
        assert arbiter.snapshot()["current"] is None
        for index in range(4):
            arbiter.say("alert", f"alert {index}", ref=f"b{index}", rule_id=f"r{index}")
        _settle(arbiter)
        playing = None
        for event in arbiter.trace:
            if event["event"] == "playing":
                assert playing is None, "two utterances were playing at once"
                playing = event["id"]
            elif event["id"] == playing and event["event"] in {"unconfirmed", "preempted", "deferred"}:
                playing = None
        spoken = [e["text"] for e in arbiter.trace if e["event"] == "unconfirmed"]
        assert spoken == ["your answer", "long alert", "alert 0", "alert 1", "alert 2", "alert 3"], spoken
        last_alert_end = float("-inf")
        for event in arbiter.trace:
            if event["kind"] != "alert":
                continue
            if event["event"] == "playing":
                assert event["mono"] - last_alert_end >= 0.09, "alert speech ignored the global floor"
            elif event["event"] in {"unconfirmed", "preempted", "deferred"}:
                last_alert_end = event["mono"]
    finally:
        arbiter.close()


def _wait_current(arbiter, ref, timeout=3.0):
    deadline = time.monotonic() + timeout
    while (arbiter.snapshot()["current"] or {}).get("ref") != ref:
        assert time.monotonic() < deadline, f"{ref} never became current"
        time.sleep(0.01)
    return arbiter.snapshot()["current"]["id"]


def test_phase8_speech_is_spoken_only_on_browser_ack_and_cancels_with_the_operator():
    from app import speech

    arbiter = _arbiter({"long": 2.0, "late": 1.0})
    try:
        # Browser playback ended: spoken, and the slot frees before the WAV deadline.
        arbiter.say("alert", "long", ref="k1")
        speech_id = _wait_current(arbiter, "k1")
        assert not arbiter.ack("other", "started") and arbiter.ack(speech_id, "started")
        began = time.monotonic()
        assert arbiter.ack(speech_id, "ended")
        _settle(arbiter)
        assert arbiter.outcome("k1") == "spoken" and time.monotonic() - began < 1.0
        try:
            arbiter.ack(speech_id, "bogus")
        except ValueError:
            pass
        else:
            raise AssertionError("unknown playback event accepted")
        # A late start restarts the window instead of cutting the clip off.
        arbiter.say("alert", "late", ref="k2")
        speech_id = _wait_current(arbiter, "k2")
        time.sleep(0.7)
        assert arbiter.ack(speech_id, "started")
        time.sleep(0.5)
        assert (arbiter.snapshot()["current"] or {}).get("ref") == "k2", "late start was cut off"
        _settle(arbiter)
        assert arbiter.outcome("k2") == "unconfirmed"
        # Playback error is recorded, not reported as spoken.
        arbiter.say("alert", "late", ref="k3")
        arbiter.ack(_wait_current(arbiter, "k3"), "error")
        _settle(arbiter)
        assert arbiter.outcome("k3") == "playback_failed"

        # Operator silencing: queued, preparing and current items for the incident go.
        arbiter.set_talk(True)
        arbiter.say("alert", "late", ref="q1", rule_id="rq", group="iA")
        arbiter.say("alert", "late", ref="q2", rule_id="rq2", group="iB")
        assert arbiter.cancel(group="iA") == 1 and arbiter.outcome("q1") == "cancelled"
        assert arbiter.cancel(rule_id="rq2") == 1 and arbiter.outcome("q2") == "cancelled"
        assert arbiter.cancel() == 0
        arbiter.set_talk(False)
        arbiter.say("alert", "long", ref="c1", group="iC")
        _wait_current(arbiter, "c1")
        assert arbiter.cancel(group="iC") == 1
        _settle(arbiter)
        assert arbiter.outcome("c1") == "cancelled"
    finally:
        arbiter.close()

    gate = threading.Event()

    def slow(text):
        gate.wait(3)
        return _wav(0.2)

    preparing = speech.Arbiter(slow, alert_floor=0.1)
    preparing.start()
    try:
        preparing.say("alert", "during synthesis", ref="p1", rule_id="rp")
        deadline = time.monotonic() + 3
        while preparing.snapshot()["preparing"] is None:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert preparing.cancel(rule_id="rp") == 1
        gate.set()
        _settle(preparing)
        assert preparing.outcome("p1") == "cancelled"
        assert not [e for e in preparing.trace if e["event"] == "playing"], "cancelled speech played"
    finally:
        preparing.close()


def test_phase8_arbiter_suppresses_while_talking_bounds_queue_and_rate_limits():
    from app.speech import bound_words

    arbiter = _arbiter({"interrupted": 1.0})
    try:
        arbiter.say("alert", "interrupted", ref="h1", rule_id="rh")
        deadline = time.monotonic() + 3
        while (arbiter.snapshot()["current"] or {}).get("ref") != "h1":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        arbiter.set_talk(True)
        time.sleep(0.1)
        snap = arbiter.snapshot()
        assert snap["current"] is None and [q["text"] for q in snap["queued"]] == ["interrupted"], snap
        arbiter.set_talk(False)
        _settle(arbiter)
        assert arbiter.outcome("h1") == "unconfirmed"
        arbiter.trace.clear()
        arbiter.set_talk(True)
        for index in range(10):
            arbiter.say("alert", f"alert {index}", ref=f"q{index}", rule_id=f"r{index}")
        arbiter.say("notice", "a notice", ref="n1")
        time.sleep(0.3)
        assert not [e for e in arbiter.trace if e["event"] == "playing"], "spoke while talk held"
        assert arbiter.outcome("n1") == "dropped_queue_full"
        dropped = [e["ref"] for e in arbiter.trace if e["event"] == "dropped_queue_full"]
        assert dropped == ["q0", "q1", "n1"], dropped
        arbiter.set_talk(False)
        _settle(arbiter)
        assert arbiter.outcome("q9") == "unconfirmed"
        arbiter.say("alert", "again", ref="c1", rule_id="r9", cooldown=60)
        assert arbiter.outcome("c1") == "rate_limited"
        assert bound_words(" ".join(["w"] * 50), 40) == " ".join(["w"] * 40) + "…"
        long_answer = "One two three four five six. " * 10
        assert bound_words(long_answer, 30) == " ".join(long_answer.split()[:30])
        assert bound_words("Short answer.", 30) == "Short answer."
    finally:
        arbiter.close()
    failing = _arbiter(fail=True)
    try:
        failing.say("alert", "person on Camera 1.", ref="t1", rule_id="r1")
        _settle(failing)
        assert failing.outcome("t1") == "text_only"
        assert failing.snapshot()["last"]["outcome"] == "text_only"
        assert failing.snapshot()["tts_ok"] is False
    finally:
        failing.close()


def test_phase8_tts_cache_hit_skips_second_synthesis_and_is_visible():
    from app import speech

    speech.PLAYBACK_MARGIN_SECONDS = 0.0
    calls = []

    def synthesize(text):
        calls.append(text)
        return _wav(0.05)

    arbiter = speech.Arbiter(synthesize, alert_floor=0.0, cache_identity="speecht5-gpu")
    arbiter.start()
    try:
        arbiter.say("answer", "person on Camera 1", ref="c1")
        _settle(arbiter)
        arbiter.say("answer", "person on Camera 1", ref="c2")
        _settle(arbiter)
        assert calls == ["person on Camera 1"], calls
        assert arbiter.outcome("c2") == "unconfirmed"
        assert arbiter.snapshot()["last"]["cached"] is True
        assert [e for e in arbiter.trace if e["event"] == "cache_hit" and e["ref"] == "c2"]
    finally:
        arbiter.close()


def test_phase8_tts_cache_is_byte_bounded_lru_and_skips_failures():
    from app import speech

    cache = speech.AudioCache(max_bytes=10, max_entries=3)
    cache.put(("a", "id"), b"12345")
    cache.put(("b", "id"), b"12345")
    assert cache.bytes == 10 and cache.get(("a", "id")) == b"12345"
    cache.put(("c", "id"), b"12345")
    assert cache.get(("b", "id")) is None and cache.get(("a", "id")) == b"12345"
    cache.put(("too-big", "id"), b"12345678901")
    assert cache.get(("too-big", "id")) is None and cache.bytes <= 10

    speech.PLAYBACK_MARGIN_SECONDS = 0.0
    calls = 0

    def synthesize(text):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("tts down")
        return _wav(0.05)

    arbiter = speech.Arbiter(synthesize, alert_floor=0.0, cache_identity="speecht5-gpu")
    arbiter.start()
    try:
        arbiter.say("answer", "not cached after failure", ref="f1")
        _settle(arbiter)
        assert arbiter.outcome("f1") == "text_only"
        arbiter.say("answer", "not cached after failure", ref="f2")
        _settle(arbiter)
        assert calls == 2 and arbiter.outcome("f2") == "unconfirmed"
        assert not arbiter.snapshot()["last"]["cached"]
    finally:
        arbiter.close()


def test_phase8_manual_speak_skips_floor_without_overlap_and_auto_alerts_wait():
    from app import speech

    speech.PLAYBACK_MARGIN_SECONDS = 0.0
    arbiter = speech.Arbiter(lambda text: _wav(0.2), alert_floor=0.8, cache_identity="speecht5-gpu")
    arbiter.start()
    try:
        arbiter.say("alert", "first", ref="first")
        first_id = _wait_current(arbiter, "first")
        arbiter.say("alert", "manual", ref="manual", bypass_alert_floor=True)
        time.sleep(0.05)
        assert (arbiter.snapshot()["current"] or {}).get("ref") == "first", "manual overlapped current speech"
        assert arbiter.ack(first_id, "ended")
        _wait_current(arbiter, "manual")
        assert arbiter.ack(arbiter.snapshot()["current"]["id"], "ended")
        _settle(arbiter)

        arbiter.say("alert", "automatic", ref="auto")
        time.sleep(0.2)
        assert arbiter.snapshot()["current"] is None, "automatic alert skipped the floor"
        _wait_current(arbiter, "auto", timeout=2.0)
        before = time.time()
        assert arbiter.ack(arbiter.snapshot()["current"]["id"], "started")
        timing = arbiter.timing("first")
        assert timing["queued"] <= timing["playing"] <= timing["playback_ended"], timing
        assert arbiter.timing("auto")["playback_started"] >= before
        assert arbiter.timing("never") == {}
    finally:
        arbiter.close()


def test_zone_dwell_rejects_stale_geometry_gaps_and_revised_rules():
    zone = {"points": [[.4, .4], [.6, .4], [.6, .6], [.4, .6]]}
    size = {"frame_width": 100, "frame_height": 100}
    for case in ("missing_rect", "missing_size", "gap", "frame_edge", "zone_edit", "reenable"):
        with tempfile.TemporaryDirectory() as tmp:
            ing = _ingest(tmp, cameras=("cam1",))
            alerts = _alerts(ing, str(Path(tmp) / "ingest.db"))
            alerts.writer = ing.write
            rule = alerts.create(ing.conn, {"class_label": "person", "stream_id": "cam1",
                                           "zone": zone, "dwell_seconds": 3})
            box = [45, 40, 10, 10]
            _observe(ing, "cam1", 1, start=1000, detection_rect=box, **size)
            _observe(ing, "cam1", 1, start=1001, detection_rect=box, **size)
            if case == "missing_rect":
                _observe(ing, "cam1", 1, start=1002, **size)
            elif case == "missing_size":
                _observe(ing, "cam1", 1, start=1002, detection_rect=box)
            elif case == "frame_edge":
                for offset in (2, 3, 4, 5):
                    _observe(ing, "cam1", 1, start=1000+offset,
                             detection_rect=[45, 90, 10, 10], **size)
            elif case == "zone_edit":
                alerts.update(ing.conn, rule["rule_id"], {"zone": {
                    "points": [[.7, .4], [.9, .4], [.9, .6], [.7, .6]]}})
                box = [75, 40, 10, 10]
            elif case == "reenable":
                alerts.update(ing.conn, rule["rule_id"], {"enabled": False})
                alerts.update(ing.conn, rule["rule_id"], {"enabled": True})
            returned = 1008 if case == "gap" else 1005.1 if case == "frame_edge" else 1003
            _observe(ing, "cam1", 1, start=returned, detection_rect=box, **size)
            assert ing.conn.execute("SELECT count(*) FROM rule_matches").fetchone()[0] == 0, case
            for offset in (1, 2, 3.1):
                _observe(ing, "cam1", 1, start=returned+offset, detection_rect=box, **size)
            assert ing.conn.execute("SELECT count(*) FROM rule_matches").fetchone()[0] == 1, case
            ing.conn.close()
            alerts.close()


def test_any_object_dwell_is_per_track_and_retains_actual_types():
    from app.alerts import RuleError
    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp, cameras=("cam1",))
        alerts = _alerts(ing, str(Path(tmp) / "ingest.db"))
        alerts.writer = ing.write
        rule = alerts.create(ing.conn, {"dwell_seconds": 3, "stream_id": "cam1"})
        assert rule["class_label"] is None
        for n in (0, 1, 2):
            _observe(ing, "cam1", 1, start=1000+n, label="person")
            _observe(ing, "cam1", 2, start=1000+n, label="car")
        assert ing.conn.execute("SELECT count(*) FROM rule_matches").fetchone()[0] == 0
        _observe(ing, "cam1", 1, start=1003, label="person")
        assert ing.conn.execute("SELECT class_label FROM alert_incidents").fetchone()[0] == "person"
        _observe(ing, "cam1", 2, start=1003, label="car")
        assert ing.conn.execute("SELECT count(*) FROM rule_matches").fetchone()[0] == 2
        assert ing.conn.execute("SELECT class_label FROM alert_incidents").fetchone()[0] == "object"
        alerts.update(ing.conn, rule["rule_id"], {"class_label": "car"})
        _observe(ing, "cam1", 3, count=40, start=1010, label="person")
        assert ing.conn.execute("SELECT count(*) FROM rule_matches").fetchone()[0] == 2
        alerts.update(ing.conn, rule["rule_id"], {"class_label": None})
        assert alerts.get(ing.conn, rule["rule_id"])["class_label"] is None
        try:
            alerts.create(ing.conn, {"class_label": ""})
            assert False, "invalid empty class must not mean wildcard"
        except RuleError:
            pass
        ing.conn.close()
        alerts.close()


def test_speech_spells_acronyms_with_letter_names():
    from app.voice import speakable
    assert speakable("The LLM runs on the GPU, detection on the NPU.") == (
        "The ell ell em runs on the jee pee you, detection on the en pee you.")
    assert speakable("Both GPUs; RTSP and MQTT; FP16.") == (
        "Both jee pee youz; arr tee ess pee and em cue tee tee; ef pee 16.")
    # Pronounceable capitals and mixed-case words are left alone.
    assert speakable("Mission AMBER FIELD uses JSON and OpenVINO on the iGPU.") == (
        "Mission AMBER FIELD uses JSON and OpenVINO on the iGPU.")


def test_phase9_audio_upload_limits_and_stock_alias():
    import asyncio
    from types import SimpleNamespace
    from app.main import create_app
    from app.voice import validate_audio, transcribe
    for data in (b"not audio", _wav(0), _wav(20.01), _wav(1)[:-10]):
        try:
            validate_audio(data)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid or oversized duration accepted")
    validate_audio(_wav(.5))
    requests = []
    def upstream(request):
        requests.append(request)
        assert b'whisper-1' in request.content and b'recording.wav' in request.content
        return httpx.Response(200, json={"text": "How many people were detected?"})
    services = SimpleNamespace(client=httpx.Client(transport=httpx.MockTransport(upstream)))
    assert transcribe(services, 'http://whisper', _wav(.5)) == 'How many people were detected?'
    async def check():
        app = create_app()
        app.state.services = services
        app.state.cfg = {"services": {"audio_analyzer": "http://whisper"}}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.post('/api/voice/transcribe', content=b'bad')).status_code == 422
            assert (await client.post('/api/voice/transcribe', content=b'x'*(10*1024*1024+1))).status_code == 413
            response = await client.post('/api/voice/transcribe', content=_wav(.5))
            assert response.status_code == 200 and response.json()['data']['text'] == 'How many people were detected?'
            await app.state.voice_lock.acquire()
            try:
                assert (await client.post('/api/voice/transcribe', content=_wav(.5))).status_code == 429
            finally:
                app.state.voice_lock.release()
            # A warm-up holding the lock delays a real question instead of refusing it.
            await app.state.voice_lock.acquire()
            app.state.voice_warming = True
            waiting = asyncio.create_task(client.post('/api/voice/transcribe', content=_wav(.5)))
            await asyncio.sleep(0.05)
            assert not waiting.done(), "transcription ran beside the warm-up"
            app.state.voice_warming = False
            app.state.voice_lock.release()
            assert (await waiting).status_code == 200
            # Audio on: a recognized question is acknowledged at once; silence is not.
            from app import speech
            spoken = []
            app.state.arbiter = speech.Arbiter(lambda text: spoken.append(text) or _wav(.05), alert_floor=0.0)
            app.state.cfg["speech_ack_text"] = "Checking."
            app.state.voice_warmed = float("-inf")
            assert (await client.post('/api/voice/transcribe', content=_wav(.5))).status_code == 200
            assert not app.state.arbiter.queue, "ack without speak=true"
            assert (await client.post('/api/voice/transcribe?speak=true', content=_wav(.5))).status_code == 200
            assert [i["text"] for i in app.state.arbiter.queue] == ["Checking."]
            app.state.arbiter.queue.clear()
            # Released with a pending question: alerts wait from that instant;
            # a failed upload releases them.
            arbiter = app.state.arbiter
            assert (await client.post('/api/speech/talk', json={"held": False, "answer_pending": True})).status_code == 200
            assert arbiter.answer_hold == arbiter.answer_generation
            assert (await client.post('/api/voice/transcribe?speak=true', content=b'bad')).status_code == 422
            assert arbiter.answer_hold is None and not arbiter.queue
            for bad in ({"held": True, "answer_pending": True}, {"answer_pending": True}, {"held": False, "x": 1}):
                assert (await client.post('/api/speech/talk', json=bad)).status_code == 422, bad
            # Cancel during transcription: the answer generation moves on, so no cue.
            release = asyncio.Event()
            import app.main as main_module
            original_transcribe = main_module.transcribe
            def slow(services, url, data):
                asyncio.run_coroutine_threadsafe(release.wait(), loop).result(2)
                return original_transcribe(services, url, data)
            loop = asyncio.get_running_loop()
            main_module.transcribe = slow
            try:
                arbiter.say("alert", "person on Camera 2.", ref="kept")
                running = asyncio.create_task(client.post('/api/voice/transcribe?speak=true', content=_wav(.5)))
                await asyncio.sleep(0.05)
                assert (await client.post('/api/voice/cancel')).status_code == 200
                release.set()
                assert (await running).status_code == 200
            finally:
                main_module.transcribe = original_transcribe
            assert [i["text"] for i in arbiter.queue] == ["person on Camera 2."], "cancel spoke a cue or dropped the alert"
            assert arbiter.answer_hold is None
            arbiter.queue.clear()
            # Talk press wakes Whisper once per interval, off the request path;
            # recent real use counts as warm.
            before = len(requests)
            for held in (True, False):
                assert (await client.post('/api/speech/talk', json={"held": held})).status_code == 200
            await asyncio.sleep(0.1)
            assert len(requests) == before, "warmed a device that was just used"
            app.state.voice_warmed = float("-inf")
            arbiter.start()
            for held in (True, False, True):
                assert (await client.post('/api/speech/talk', json={"held": held})).status_code == 200
            deadline = time.monotonic() + 2
            while len(requests) == before or "Checking." not in spoken:
                assert time.monotonic() < deadline, "Talk did not warm Whisper and TTS"
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)
            assert len(requests) == before + 1 and spoken == ["Checking."], (len(requests) - before, spoken)
            arbiter.close()
    asyncio.run(check())
    services.client.close()


def test_phase9_chunked_answer_order_and_interruption():
    from app.speech import Arbiter, answer_chunks
    text = 'First answer. ' + ' '.join(['word'] * 75) + '. Last sentence.'
    chunks = answer_chunks(text)
    assert ' '.join(chunks) == text and all(len(c.split()) <= 12 for c in chunks)
    seen = []
    arbiter = Arbiter(lambda text: seen.append(text) or _wav(2))
    arbiter.start()
    try:
        arbiter.say_answer('First sentence. Second sentence. Third sentence.', arbiter.answer_generation)
        for expected in ('First sentence.', 'Second sentence.'):
            deadline = time.monotonic() + 2
            while not arbiter.snapshot()['current'] or arbiter.snapshot()['current']['text'] != expected:
                assert time.monotonic() < deadline
                time.sleep(.01)
            current = arbiter.snapshot()['current']
            assert expected in seen
            if expected == 'First sentence.':
                arbiter.ack(current['id'], 'ended')
        generation = arbiter.answer_generation
        arbiter.set_talk(True)
        _settle(arbiter)
        assert not [e for e in arbiter.trace if e["event"] == "playing" and e["text"] == "Third sentence."]
        arbiter.set_talk(False)
        assert arbiter.say_answer('Stale response.', generation) is None
        arbiter.say_answer('New response. Must not play.', arbiter.answer_generation)
        arbiter.stop_speaking()
        _settle(arbiter)
        assert 'Must not play.' not in seen
        arbiter.set_talk(True)
        with arbiter.cond:
            arbiter.talk_deadline = arbiter.clock() - 1
            arbiter.cond.notify_all()
        deadline = time.monotonic() + 1
        while arbiter.snapshot()['talk_held']:
            assert time.monotonic() < deadline
            time.sleep(.01)
    finally:
        arbiter.close()


def test_phase9_prefetch_one_chunk_serial_order_and_missing_ack():
    from app import speech

    speech.PLAYBACK_MARGIN_SECONDS = 0.0
    active = 0
    max_active = 0
    calls = []
    lock = threading.Lock()

    def synthesize(text):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            calls.append(text)
            time.sleep(0.03)
            return _wav(0.2)
        finally:
            with lock:
                active -= 1

    arbiter = speech.Arbiter(synthesize, alert_floor=0.0)
    arbiter.start()
    try:
        arbiter.say_answer("One. Two. Three.", arbiter.answer_generation)
        first = _wait_text_current(arbiter, "One.")
        deadline = time.monotonic() + 2
        while "Two." not in calls:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert calls == ["One.", "Two."] and max_active == 1, calls
        assert arbiter.ack(first, "ended")
        second = _wait_text_current(arbiter, "Two.")
        assert arbiter.ack(second, "ended")
        third = _wait_text_current(arbiter, "Three.")
        assert arbiter.ack(third, "ended")
        _settle(arbiter)
        assert calls == ["One.", "Two.", "Three."], calls

        calls.clear()
        arbiter.say_answer("Alpha. Beta. Gamma.", arbiter.answer_generation)
        _wait_text_current(arbiter, "Alpha.")
        deadline = time.monotonic() + 2
        while "Beta." not in calls:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        _settle(arbiter)
        assert calls == ["Alpha.", "Beta."], "missing ACK must not advance past one prefetched chunk"
    finally:
        arbiter.close()


def test_phase9_numbered_stream_preserves_order_and_chunk_shape():
    from app.main import EarlyAnswerSpeech
    from app import speech

    speech.PLAYBACK_MARGIN_SECONDS = 0.0
    answer = ("1. Drones primarily rely on direct radio waves for command and control. "
              "2. Radio signals can face interference, obstruction, or even loss in areas "
              "where there's no direct path for signals to travel. "
              "3. By necessitating a line of sight, the pilot can ensure reliable "
              "communication and maintain a visual reference when needed.")
    chunks = speech.answer_chunks(answer)
    assert all(chunk not in {"1.", "2.", "3."} for chunk in chunks), chunks
    assert "to travel." not in chunks and all(len(chunk.split()) <= 12 for chunk in chunks), chunks
    assert speech.answer_chunks("Dr. Smith measured 3.5 volts. 1. Test follows.") == [
        "Dr. Smith measured 3.5 volts.", "1. Test follows."]

    spoken = []
    arbiter = speech.Arbiter(lambda text: spoken.append(text) or _wav(0.05), alert_floor=0.0)
    arbiter.start()
    try:
        early = EarlyAnswerSpeech(arbiter, arbiter.answer_generation)
        for delta in ("1. ", "Drones primarily rely on direct radio waves for command and control. 2. ",
                      "Radio signals can face interference, obstruction, or even loss in areas where ",
                      "there's no direct path for signals to travel. 3. By necessitating a line of sight, ",
                      "the pilot can ensure reliable communication and maintain a visual reference when needed."):
            early.feed(delta)
        early.finish()
        expected_chunks = speech.short_head(chunks)
        assert expected_chunks[0] == "1. Drones primarily rely on direct radio", expected_chunks
        for expected in expected_chunks:
            speech_id = _wait_text_current(arbiter, expected)
            assert arbiter.ack(speech_id, "ended")
        _settle(arbiter)
        assert spoken == expected_chunks, spoken
    finally:
        arbiter.close()


def test_first_answer_words_are_spoken_before_the_first_sentence_ends():
    from app.main import EarlyAnswerSpeech
    from app import speech

    assert speech.short_head(["A thermal camera detects infrared radiation and shows it as heat."]) == [
        "A thermal camera detects infrared radiation", "and shows it as heat."]
    assert speech.short_head(["The sky appears blue due to the scattering of sunlight."])[0] == (
        "The sky appears blue"), "the head must not end on a leaning word"
    # A clause boundary inside the head wins; short sentences stay whole.
    assert speech.short_head(["A thermal camera detects heat, so it sees through smoke and fog."])[0] == (
        "A thermal camera detects heat,")
    assert speech.short_head(["Two people, both near the door."]) == ["Two people, both near the door."]
    assert speech.short_head(["Radar sends pulses, then times echoes back."]) == [
        "Radar sends pulses, then times echoes back."], "tail under 4 words must not split"
    assert speech.early_head("A thermal camera") == (None, "A thermal camera")
    assert speech.early_head("A thermal camera detects infrared radiat") == (None, "A thermal camera detects infrared radiat")
    assert speech.early_head("A thermal camera detects infrared radiation and ") == (
        None, "A thermal camera detects infrared radiation and ")
    assert speech.early_head("A thermal camera detects infrared radiation and turns ") == (
        "A thermal camera detects infrared radiation", "and turns ")

    speech.PLAYBACK_MARGIN_SECONDS = 0.0
    spoken = []
    arbiter = speech.Arbiter(lambda text: spoken.append(text) or _wav(0.05), alert_floor=0.0)
    arbiter.start()
    try:
        early = EarlyAnswerSpeech(arbiter, arbiter.answer_generation)
        for delta in ("A thermal ", "camera detects infrared ", "radiation and turns "):
            early.feed(delta)
        first = _wait_text_current(arbiter, "A thermal camera detects infrared radiation")
        early.feed("it into images. It sees through smoke.")
        early.finish()
        deadline = time.time() + 2
        while "and turns it into images." not in spoken and time.time() < deadline:
            time.sleep(0.01)
        assert "and turns it into images." in spoken, "text streamed during playback must be synthesized before it ends"
        assert arbiter.ack(first, "ended")
        for expected in ("and turns it into images.", "It sees through smoke."):
            assert arbiter.ack(_wait_text_current(arbiter, expected), "ended")
        _settle(arbiter)
        assert spoken == ["A thermal camera detects infrared radiation", "and turns it into images.",
                          "It sees through smoke."], spoken
    finally:
        arbiter.close()


def test_voice_acknowledgement_plays_first_and_is_separate_from_the_answer():
    from app import speech

    speech.PLAYBACK_MARGIN_SECONDS = 0.0
    calls = []
    arbiter = speech.Arbiter(lambda text: calls.append(text) or _wav(0.05), alert_floor=0.0)
    arbiter.start()
    try:
        arbiter.warm("Checking.")
        deadline = time.monotonic() + 2
        while "Checking." not in calls:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        time.sleep(0.05)
        assert arbiter.snapshot()["current"] is None, "warm-up must never play"
        generation = arbiter.answer_generation
        arbiter.begin_answer(generation)
        arbiter.say("alert", "person on Camera 1.", ref="held")
        cue = arbiter.acknowledge("Checking.")
        # Cached by the warm-up: no second synthesis, playable at once.
        _wait_text_current(arbiter, "Checking.")
        assert calls.count("Checking.") == 1 and arbiter.snapshot()["current"]["cached"]
        # The browser never confirms the cue: the answer must still be spoken whole.
        arbiter.say_answer("Two people are near the door. Both arrived recently.", generation)
        arbiter.end_answer(generation)
        for expected in ("Two people are near the door.", "Both arrived recently."):
            assert arbiter.ack(_wait_text_current(arbiter, expected), "ended")
        _settle(arbiter)
        order = [e["text"] for e in arbiter.trace if e["event"] == "playing"]
        assert order == ["Checking.", "Two people are near the door.", "Both arrived recently.",
                         "person on Camera 1."], order
        assert arbiter.outcome("held") == "unconfirmed" and cue["kind"] == "answer"
        # Talk cancels a pending cue like any other answer speech.
        arbiter.set_talk(True)
        assert arbiter.acknowledge("Checking.") is None
        arbiter.set_talk(False)
        # A cue for a question abandoned before transcription finished is stale.
        stale = arbiter.answer_generation
        arbiter.cancel_answer()
        assert arbiter.acknowledge("Checking.", stale) is None
        assert arbiter.acknowledge("Checking.", arbiter.answer_generation) is not None
    finally:
        arbiter.close()


def test_voice_release_holds_alerts_and_warmup_never_overlaps_synthesis():
    from app import speech

    speech.PLAYBACK_MARGIN_SECONDS = 0.0
    active, peak, calls = [0], [0], []
    lock = threading.Lock()

    def synthesize(text):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        calls.append(text)
        time.sleep(0.15)
        with lock:
            active[0] -= 1
        return _wav(0.05)

    arbiter = speech.Arbiter(synthesize, alert_floor=0.0)
    arbiter.start()
    try:
        # Microphone released with a question uploading: an alert queued in
        # that gap must not synthesize ahead of the answer.
        arbiter.set_talk(True)
        arbiter.set_talk(False, answer_pending=True)
        arbiter.say("alert", "person on Camera 1.", ref="gap")
        time.sleep(0.2)
        assert "person on Camera 1." not in calls, "alert took TTS between recording and transcription"
        # Warm-up requested while operator speech is synthesizing waits its turn.
        generation = arbiter.answer_generation
        arbiter.say_answer("One. Two. Three.", generation)
        time.sleep(0.02)
        assert arbiter.warm("Checking.")
        assert not arbiter.warm("Checking."), "overlapping warm-ups"
        arbiter.end_answer(generation)
        deadline = time.monotonic() + 5
        while "Checking." not in calls or "person on Camera 1." not in calls:
            assert time.monotonic() < deadline, calls
            current = arbiter.snapshot()["current"]
            if current:
                arbiter.ack(current["id"], "ended")
            time.sleep(0.01)
        assert peak[0] == 1, "warm-up ran beside real synthesis"
        # TTS just ran: the device is awake, nothing to warm.
        assert not arbiter.warm("Checking.", idle_seconds=20)
    finally:
        arbiter.close()


def test_phase9_prepared_chunk_starts_before_following_prefetch_finishes():
    from app import speech

    speech.PLAYBACK_MARGIN_SECONDS = 0.0
    delays = {"First sentence.": 0.01, "Second sentence.": 0.01, "Third sentence.": 0.4}
    calls = []

    def synthesize(text):
        calls.append((text, time.monotonic()))
        time.sleep(delays[text])
        return _wav(0.5)

    arbiter = speech.Arbiter(synthesize, alert_floor=0.0)
    arbiter.start()
    try:
        arbiter.say_answer("First sentence. Second sentence. Third sentence.",
                           arbiter.answer_generation)
        first = _wait_text_current(arbiter, "First sentence.")
        deadline = time.monotonic() + 2
        while "Second sentence." not in [text for text, _ in calls]:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert arbiter.ack(first, "ended")
        ended = time.monotonic()
        _wait_text_current(arbiter, "Second sentence.", timeout=0.2)
        assert time.monotonic() - ended < 0.2
        assert "Third sentence." in [text for text, _ in calls]
    finally:
        arbiter.close()


def _wait_text_current(arbiter, text, timeout=3.0):
    deadline = time.monotonic() + timeout
    while (arbiter.snapshot()["current"] or {}).get("text") != text:
        assert time.monotonic() < deadline, f"{text!r} never became current"
        time.sleep(0.01)
    return arbiter.snapshot()["current"]["id"]


def test_ready_next_chunk_is_exposed_and_its_early_acks_are_kept():
    from app import speech

    speech.PLAYBACK_MARGIN_SECONDS = 0.0
    arbiter = speech.Arbiter(lambda text: _wav(0.5), alert_floor=0.0)
    arbiter.start()
    try:
        assert arbiter.say_answer("First chunk here now. Second chunk follows here.", arbiter.answer_generation)
        first = _wait_text_current(arbiter, "First chunk here now.")
        deadline = time.monotonic() + 3
        while arbiter.snapshot()["next"] is None:
            assert time.monotonic() < deadline, "prefetched chunk never exposed"
            time.sleep(0.01)
        nxt = arbiter.snapshot()["next"]
        assert nxt["text"] == "Second chunk follows here." and nxt["audio_base64"]
        # The browser starts the preload as its clip ends, before promotion.
        assert arbiter.ack(first, "started")
        assert arbiter.ack(nxt["id"], "started"), "early start of the ready next chunk must be accepted"
        assert arbiter.ack(first, "ended")
        assert _wait_text_current(arbiter, "Second chunk follows here.") == nxt["id"]
        assert arbiter.ack(nxt["id"], "ended")
        _settle(arbiter)
        assert arbiter.snapshot()["last"]["outcome"] == "spoken"
        assert not arbiter.ack("unknown", "started")
    finally:
        arbiter.close()


def test_phase9_prefetch_rejects_late_results_failures_and_answer_outranks_alerts():
    from app import speech

    speech.PLAYBACK_MARGIN_SECONDS = 0.0
    calls = []

    def synthesize(text):
        calls.append(text)
        if text == "Two fails.":
            raise httpx.ConnectError("tts down")
        if text == "Answer one.":
            arbiter.say("alert", "urgent alert", ref="urgent")
        return _wav(0.1)

    arbiter = speech.Arbiter(synthesize, alert_floor=0.0)
    arbiter.start()
    try:
        arbiter.say_answer("Answer one. Answer two.", arbiter.answer_generation)
        for text in ("Answer one.", "Answer two."):
            assert arbiter.ack(_wait_text_current(arbiter, text), "ended")
        _settle(arbiter)
        assert arbiter.outcome("urgent") == "unconfirmed"
        order = [e["text"] for e in arbiter.trace if e["event"] == "playing"]
        assert order[-3:] == ["Answer one.", "Answer two.", "urgent alert"], order

        arbiter.say_answer("One ok. Two fails.", arbiter.answer_generation)
        first = _wait_text_current(arbiter, "One ok.")
        deadline = time.monotonic() + 2
        while "Two fails." not in calls:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert arbiter.ack(first, "ended")
        _settle(arbiter)
        assert arbiter.snapshot()["last"]["text"] == "Two fails."
        assert arbiter.snapshot()["last"]["outcome"] == "text_only"

        generation = arbiter.answer_generation
        arbiter.say_answer("Late one. Late two.", generation)
        _wait_text_current(arbiter, "Late one.")
        arbiter.stop_speaking()
        _settle(arbiter)
        assert arbiter.say_answer("Rejected late.", generation) is None
        assert "Rejected late." not in calls
    finally:
        arbiter.close()


def test_answer_window_holds_alerts_until_the_answer_is_spoken():
    from app import speech

    speech.PLAYBACK_MARGIN_SECONDS = 0.0
    calls = []
    arbiter = speech.Arbiter(lambda text: calls.append(text) or _wav(0.1), alert_floor=0.0)
    arbiter.start()
    try:
        generation = arbiter.answer_generation
        arbiter.begin_answer(generation)
        arbiter.say("alert", "person on Camera 1.", ref="held")
        time.sleep(0.3)
        # The alert must not take the TTS lock while the LLM is still thinking.
        assert calls == [] and arbiter.outcome("held") == "queued"
        arbiter.say_answer("First part. Second part.", generation)
        arbiter.end_answer(generation)
        for text in ("First part.", "Second part."):
            assert arbiter.ack(_wait_text_current(arbiter, text), "ended")
        _settle(arbiter)
        order = [e["text"] for e in arbiter.trace if e["event"] == "playing"]
        assert order == ["First part.", "Second part.", "person on Camera 1."], order
        assert calls[0] == "First part."

        # A lost request cannot starve alerts: the window is bounded.
        arbiter.begin_answer(arbiter.answer_generation, seconds=0.3)
        arbiter.say("alert", "dog on Camera 2.", ref="bounded")
        _settle(arbiter)
        assert arbiter.outcome("bounded") == "unconfirmed"

        # Stop speaking (new generation) releases the window at once.
        arbiter.begin_answer(arbiter.answer_generation)
        arbiter.stop_speaking()
        started = time.monotonic()
        arbiter.say("alert", "car on Camera 1.", ref="released")
        _settle(arbiter)
        assert arbiter.outcome("released") == "unconfirmed" and time.monotonic() - started < 2
    finally:
        arbiter.close()


def test_phase9_streaming_answer_speech_once_order_and_races():
    from app.main import EarlyAnswerSpeech
    from app import speech

    speech.PLAYBACK_MARGIN_SECONDS = 0.0
    spoken = []
    arbiter = speech.Arbiter(lambda text: spoken.append(text) or _wav(0.1), alert_floor=0.0)
    arbiter.start()
    try:
        early = EarlyAnswerSpeech(arbiter, arbiter.answer_generation)
        early.feed("First sentence. Second ")
        first = _wait_text_current(arbiter, "First sentence.")
        early.feed("sentence. Third")
        early.finish()
        assert early.first is not None
        assert arbiter.ack(first, "ended")
        second = _wait_text_current(arbiter, "Second sentence.")
        assert arbiter.ack(second, "ended")
        third = _wait_text_current(arbiter, "Third")
        assert arbiter.ack(third, "ended")
        _settle(arbiter)
        assert spoken == ["First sentence.", "Second sentence.", "Third"], spoken

        early = EarlyAnswerSpeech(arbiter, arbiter.answer_generation)
        early.feed("Partial answer. ")
        _wait_text_current(arbiter, "Partial answer.")
        early.feed("Do not speak this yet")
        early.cancel()
        _settle(arbiter)
        assert "Do not speak this yet" not in spoken

        generation = arbiter.answer_generation
        early = EarlyAnswerSpeech(arbiter, generation)
        early.feed("Old conversation sentence. ")
        arbiter.stop_speaking()
        early.feed("Late sentence. ")
        early.finish()
        _settle(arbiter)
        assert "Late sentence." not in spoken
    finally:
        arbiter.close()


def test_live_and_rule_work_leave_event_loop_responsive():
    import asyncio
    from unittest.mock import patch
    from starlette.requests import Request
    from app.main import create_app, _live_events

    async def check():
        app = create_app()
        owner = threading.get_ident()
        calls = []
        def operation(*args):
            assert threading.get_ident() != owner, "database work ran on event loop"
            calls.append(True)
            return {"rule_id": "r1"}
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "live.db")
            conn = connect(db)
            migrate(conn)
            conn.close()
            app.state.cfg = {"db_path": db, "streams": []}
            app.state.ingest = types.SimpleNamespace(snapshot=lambda: {})
            app.state.telemetry = types.SimpleNamespace(snapshot=lambda: {"metrics": []})
            app.state.alerts = types.SimpleNamespace(create=operation, update=operation,
                act=lambda *a: bool(operation(*a)), confirmed=set(), latency={}, version=0,
                mark_in_view=lambda *a: None)
            app.state.arbiter = types.SimpleNamespace(snapshot=lambda: {"version": 1})
            async def receive():
                await asyncio.sleep(60)
                return {"type": "http.request", "body": b""}
            request = Request({"type": "http", "app": app, "method": "GET",
                               "path": "/api/stream", "headers": []}, receive=receive)
            batch = _live_events(request, (), False, True)
            assert len(batch) == 2 and batch[0].startswith("event: alert")
            def slow_batch(*args):
                operation()
                time.sleep(.1)
                return batch
            endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", None) == "/api/stream")
            with patch("app.main._live_events", slow_batch):
                response = await endpoint(request, classes=None, has_image=False, prioritize=True)
                iterator = response.body_iterator
                await anext(iterator)  # retry
                await anext(iterator)  # mode
                event = asyncio.create_task(anext(iterator))
                await asyncio.sleep(.02)
                assert not event.done(), "SSE read blocked event loop"
                assert (await event).startswith("event: alert")
                await iterator.aclose()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                assert (await client.post("/api/rules", json={})).status_code == 201
                assert (await client.patch("/api/rules/r1", json={})).status_code == 200
                assert (await client.post("/api/incidents/i1/acknowledge", json={})).status_code == 200
                before = len(calls)
                for payload in ("{", "[]", '"bad"', '{"unexpected":1}'):
                    response = await client.post("/api/incidents/i1/acknowledge", content=payload)
                    assert response.status_code == 422
                assert len(calls) == before, "invalid action reached writer"
    asyncio.run(check())


def test_old_stream_error_cannot_cancel_new_answer():
    from app.main import EarlyAnswerSpeech
    from app.speech import Arbiter
    arbiter = Arbiter(lambda text: _wav(.01))
    old = EarlyAnswerSpeech(arbiter, arbiter.answer_generation)
    arbiter.cancel_answer()
    new = arbiter.say_answer("Keep this newer answer.", arbiter.answer_generation)
    old.cancel()
    assert new in arbiter.queue
    active = EarlyAnswerSpeech(arbiter, arbiter.answer_generation)
    active.cancel()
    assert new not in arbiter.queue


def test_replay_answer_validation_and_recording_guard():
    import asyncio
    from app.main import create_app
    from app.speech import Arbiter
    async def check():
        app = create_app()
        app.state.arbiter = Arbiter(lambda text: _wav(.01))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for body in ({}, {"text": ""}, {"text": 5}, {"text": "x" * 8001}, {"text": "ok", "extra": 1}):
                assert (await client.post("/api/speech/replay", json=body)).status_code == 422
            assert (await client.post("/api/speech/replay", content="{")).status_code == 422
            app.state.arbiter.set_talk(True)
            assert (await client.post("/api/speech/replay", json={"text": "Hello."})).status_code == 409
            app.state.arbiter.set_talk(False)
            response = await client.post("/api/speech/replay", json={"text": "Hello."})
            assert response.status_code == 200
            assert app.state.arbiter.queue[0]["text"] == "Hello."
            arbiter = app.state.arbiter
            arbiter.begin_answer(arbiter.answer_generation)
            assert (await client.post("/api/speech/replay", json={"text": "Hello."})).status_code == 409
            arbiter.end_answer()
            response = await client.post("/api/speech/replay", json={"text": "Short sentence here. " * 200})
            assert response.status_code == 200
            item = arbiter.queue[-1]
            spoken = " ".join([item["text"], *item["remaining"]])
            assert len(spoken) <= 1500 and spoken.endswith("."), len(spoken)
            # A generation change between cancel and queueing yields no item: 422, not 500.
            real = arbiter.say_answer
            arbiter.say_answer = lambda text, generation: None
            assert (await client.post("/api/speech/replay", json={"text": "Hello."})).status_code == 422
            arbiter.say_answer = real
            # The UI revalidates so a browser never keeps running a pre-update page.
            for path in ("/", "/architecture.html"):
                page = await client.get(path)
                assert page.status_code == 200 and page.headers["cache-control"] == "no-cache", path
            assert "EventSource.CLOSED" in (await client.get("/")).text, "closed live stream must reconnect"
    asyncio.run(check())


class FakeMediaMtx:
    """MediaMTX config-path API: add/patch/delete/list, with injectable refusal."""

    def __init__(self):
        self.paths, self.calls, self.refuse = {}, [], None

    def __call__(self, request):
        verb, _, name = request.url.path.removeprefix("/v3/config/paths/").partition("/")
        self.calls.append((request.method, verb, name))
        if verb == "list":
            return httpx.Response(200, json={"items": [
                {"name": n, "source": body["source"]} for n, body in self.paths.items()]})
        if verb == "add":
            if self.refuse:
                return httpx.Response(400, json={"error": self.refuse})
            if name in self.paths:
                return httpx.Response(400, json={"error": "path already exists"})
            self.paths[name] = json.loads(request.content)
            return httpx.Response(200)
        if verb == "patch":
            self.paths[name] = json.loads(request.content)
            return httpx.Response(200)
        if verb == "delete":
            return httpx.Response(200 if self.paths.pop(name, None) else 404)
        return httpx.Response(404)


def _camera_state(tmp, config=("cam1", "cam2")):
    from app import cameras

    ing = _ingest(tmp, cameras=config)
    db = str(Path(tmp) / "ingest.db")
    mediamtx = FakeMediaMtx()
    cfg = {"db_path": db, "rtsp_base": "rtsp://mediamtx:8554",
           "services": {"mediamtx": "http://mediamtx:9997"},
           "camera_source_allow": ["192.168.0.0/16", "cam.example"],
           "streams": [{"stream_id": c, "display_name": c, "description": "yard"} for c in config]}
    cfg["config_streams"] = list(cfg["streams"])
    state = types.SimpleNamespace(cfg=cfg, ingest=ing,
        services=types.SimpleNamespace(client=httpx.Client(transport=httpx.MockTransport(mediamtx))))
    state.alerts = _alerts(ing, db, streams=config)
    cameras.refresh(state)
    return state, mediamtx


def _camera_error(call):
    from app.cameras import CameraError
    try:
        call()
    except CameraError as exc:
        return exc
    raise AssertionError("expected CameraError")


def test_camera_pull_url_validation():
    from app.cameras import validate_pull_url
    allow = ["192.168.0.0/16", "10.0.0.0/8", "cam.example"]
    for good in ("rtsp://192.168.1.20:554/stream1", "rtsps://10.1.2.3/live", "rtsp://cam.example/a"):
        assert validate_pull_url(good, allow) == good
    for bad, status in (("http://192.168.1.2/x", 422), ("rtsp://user:pw@192.168.1.2/x", 422),
                        ("rtsp://@192.168.1.2/x", 422), ("rtsp://127.0.0.1/cam1", 403),
                        ("rtsp://169.254.1.1/x", 403), ("rtsp://224.0.0.1/x", 403),
                        ("rtsp://0.0.0.0/x", 403), ("rtsp://8.8.8.8/x", 403),
                        ("rtsp://other.example/x", 403), ("rtsp://192.168.1.2:99999/x", 422),
                        ("rtsp://192.168.1.2/a b", 422), ("rtsp:///x", 422), ("", 422), (5, 422),
                        ("rtsp://192.168.1.2/" + "x" * 600, 422)):
        assert _camera_error(lambda: validate_pull_url(bad, allow)).status == status, bad


def test_config_cameras_keep_operator_rows_and_locked_names():
    with tempfile.TemporaryDirectory() as tmp:
        conn = connect(str(Path(tmp) / "app.db"))
        migrate(conn)
        migrate(conn)
        row = lambda sid, name="cam", enabled=True: {"stream_id": sid, "display_name": name,
            "source_uri": "u", "annotated_path": "a", "enabled": enabled, "sort_order": 0}
        sync_streams(conn, [row("cam1", "Gate")])
        conn.execute("INSERT INTO streams (stream_id, display_name, source_uri, annotated_path,"
                     " enabled, origin) VALUES ('cam2','Mine','u','a',1,'operator')")
        conn.execute("UPDATE streams SET display_name='Front', name_locked=1 WHERE stream_id='cam1'")
        sync_streams(conn, [row("cam1", "Gate"), row("cam2", "Config two")])
        rows = {r["stream_id"]: r for r in conn.execute("SELECT * FROM streams")}
        assert rows["cam1"]["display_name"] == "Front"
        assert rows["cam2"]["display_name"] == "Mine" and rows["cam2"]["origin"] == "operator"
        # An operator "off" survives restarts while the deployment has the camera on.
        conn.execute("UPDATE streams SET enabled=0, operator_off=1 WHERE stream_id='cam1'")
        sync_streams(conn, [row("cam1", "Gate")])
        r = conn.execute("SELECT enabled, operator_off FROM streams WHERE stream_id='cam1'").fetchone()
        assert tuple(r) == (0, 1)
        # A deployment "off" clears it, so the camera is not offered in the console.
        sync_streams(conn, [row("cam1", "Gate", enabled=False)])
        r = conn.execute("SELECT enabled, operator_off FROM streams WHERE stream_id='cam1'").fetchone()
        assert tuple(r) == (0, 0)
        conn.execute("UPDATE streams SET enabled=0, operator_off=1 WHERE stream_id='cam1'")
        sync_streams(conn, [])  # retiring config cameras never touches operator ones
        rows = {r["stream_id"]: r for r in conn.execute("SELECT * FROM streams")}
        assert not rows["cam1"]["enabled"] and not rows["cam1"]["operator_off"] and rows["cam2"]["enabled"]
        conn.close()


def test_camera_add_rename_toggle_remove():
    from app import cameras, pipelines

    with tempfile.TemporaryDirectory() as tmp:
        state, mediamtx = _camera_state(tmp)
        conn = state.ingest.conn
        pipelines.wake.clear()
        cam3 = cameras.add(state, {"display_name": " Back  door ", "source": {"kind": "publish"}})
        assert cam3 == "cam3" and pipelines.wake.is_set()
        row = conn.execute("SELECT * FROM streams WHERE stream_id='cam3'").fetchone()
        assert row["display_name"] == "Back door" and row["origin"] == "operator"
        assert row["source_uri"] == "rtsp://mediamtx:8554/cam3" and row["annotated_path"] == "annotated3"
        assert "cam3" in state.ingest.known and "cam3" in state.alerts.enabled_streams
        assert [s["stream_id"] for s in state.cfg["streams"]] == ["cam1", "cam2", "cam3"]
        assert state.cfg["streams"][0]["description"] == "yard"  # config extras kept
        for body in ({"display_name": "BACK DOOR", "source": {"kind": "publish"}},):
            cameras.update(state, "cam3", {"enabled": False})
            assert _camera_error(lambda: cameras.add(state, body)).code == "name_taken"
        for body in (None, {}, {"display_name": "x"}, {"display_name": "x", "source": {"kind": "file"}},
                     {"display_name": "x", "source": {"kind": "publish", "url": "rtsp://x"}},
                     {"display_name": "\x07", "source": {"kind": "publish"}},
                     {"display_name": "x" * 41, "source": {"kind": "publish"}},
                     {"display_name": "x", "source": {"kind": "publish"}, "extra": 1}):
            assert _camera_error(lambda: cameras.add(state, body)).status == 422, body
        assert "cam3" not in state.ingest.known and "cam3" not in state.alerts.enabled_streams
        # Pull camera: MediaMTX path added over TCP; the failed add leaves no row.
        mediamtx.refuse = "invalid source"
        refused = _camera_error(lambda: cameras.add(state, {"display_name": "Roof",
            "source": {"kind": "pull", "url": "rtsp://192.168.1.9/s"}}))
        assert refused.status == 502 and "invalid source" in str(refused)
        assert conn.execute("SELECT count(*) FROM streams").fetchone()[0] == 3
        mediamtx.refuse = None
        cam4 = cameras.add(state, {"display_name": "Roof",
                                   "source": {"kind": "pull", "url": "rtsp://192.168.1.9/s"}})
        assert cam4 == "cam4" and mediamtx.paths["cam4"] == {
            "source": "rtsp://192.168.1.9/s", "sourceOnDemand": False, "rtspTransport": "tcp"}
        assert _camera_error(lambda: cameras.add(state, {"display_name": "Dup",
            "source": {"kind": "pull", "url": "rtsp://192.168.1.9/s"}})).code == "source_taken"
        # No camera limit: a fourth camera turns on; the device capacity is the limit.
        cameras.update(state, "cam3", {"enabled": True})
        assert cameras.view(conn, state.cfg, {})["enabled_count"] == 4
        cameras.update(state, "cam1", {"display_name": "Gate"})
        assert state.alerts.names["cam1"] == "Gate"
        assert conn.execute("SELECT name_locked FROM streams WHERE stream_id='cam1'").fetchone()[0] == 1
        # Config cameras: the operator may turn them off and on, not remove them.
        cameras.update(state, "cam1", {"enabled": False})
        assert conn.execute("SELECT operator_off FROM streams WHERE stream_id='cam1'").fetchone()[0] == 1
        assert "cam1" in [c["stream_id"] for c in cameras.view(conn, state.cfg, {})["cameras"]]
        cameras.update(state, "cam1", {"enabled": True})
        assert conn.execute("SELECT operator_off FROM streams WHERE stream_id='cam1'").fetchone()[0] == 0
        assert _camera_error(lambda: cameras.remove(state, "cam1", "cam1")).code == "config_camera"
        assert _camera_error(lambda: cameras.update(state, "cam9", {"display_name": "x"})).status == 404
        # Disabling a pull camera drops its MediaMTX path; removal is soft.
        cameras.update(state, "cam4", {"enabled": False})
        assert "cam4" not in mediamtx.paths
        assert _camera_error(lambda: cameras.remove(state, "cam3", "cam4")).status == 422
        cameras.remove(state, "cam3", "cam3")
        row = conn.execute("SELECT * FROM streams WHERE stream_id='cam3'").fetchone()
        assert row["removed_utc"] and not row["enabled"]
        assert "cam3" not in [s["stream_id"] for s in state.cfg["streams"]]
        assert state.alerts.names["cam3"] == "Back door"  # past alerts keep their label
        assert _camera_error(lambda: cameras.update(state, "cam3", {"display_name": "x"})).status == 404
        # Removed ids are never reused; a removed name is free again.
        assert cameras.add(state, {"display_name": "Back door", "source": {"kind": "publish"}}) == "cam5"
        actions = [r[0] for r in conn.execute("SELECT action FROM camera_audit ORDER BY audit_id")]
        assert actions == ["added", "disabled", "added", "add_failed", "added", "enabled", "renamed", "disabled",
                           "enabled", "disabled", "removed", "added"], actions
        view = cameras.view(conn, state.cfg, {})
        assert [c["stream_id"] for c in view["cameras"]] == ["cam1", "cam2", "cam4", "cam5"]
        assert view["enabled_count"] == 3 and "max_streams" not in view
        assert view["cameras"][3]["source"]["publish_url"] == "rtsp://127.0.0.1:8554/cam5"
        assert view["cameras"][2]["feed"]["reason"] == "off"
        state.alerts.close()
        state.ingest.conn.close()


def test_off_config_camera_hidden_and_pull_path_restore():
    from app import cameras

    with tempfile.TemporaryDirectory() as tmp:
        state, mediamtx = _camera_state(tmp, config=("cam1",))
        conn = state.ingest.conn
        cameras.add(state, {"display_name": "A", "source": {"kind": "pull", "url": "rtsp://192.168.1.2/a"}})
        cameras.add(state, {"display_name": "B", "source": {"kind": "publish"}})
        # MediaMTX restarted (runtime path gone) and holds a path nobody wants.
        mediamtx.paths = {"cam7": {"source": "rtsp://192.168.1.7/x"}}
        cameras.ensure_pull_paths(conn, state.cfg, state.services.client)
        assert set(mediamtx.paths) == {"cam2"}
        calls = len(mediamtx.calls)
        cameras.ensure_pull_paths(conn, state.cfg, state.services.client)
        assert len(mediamtx.calls) == calls + 1  # steady state: one list, no writes
        # A deployment camera that is off (not selected by make CAMERAS, or dropped
        # from backend.yaml) cannot be turned on in the console: not listed, and
        # its name stays free.
        conn.execute("INSERT INTO streams (stream_id, display_name, source_uri, annotated_path,"
                     " enabled, origin) VALUES ('cam9','Camera 9','u','a',0,'config')")
        assert "cam9" not in [c["stream_id"] for c in cameras.view(conn, state.cfg, {})["cameras"]]
        assert cameras.add(state, {"display_name": "Camera 9", "source": {"kind": "publish"}}) == "cam4"
        state.alerts.close()
        state.ingest.conn.close()


def test_ingest_set_known_closes_removed_camera_and_grants_new_grace():
    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp)
        ing._on_message(None, None, types.SimpleNamespace(
            topic="detections/live", payload=_observation("cam2", 1)))
        assert ing.active and ing.health.get("cam2")
        ing.started_at -= 10_000  # long past the startup grace
        ing.set_known(["cam1", "cam3"])
        assert ing.known == {"cam1", "cam3"}
        assert not any(k[0] == "cam2" for k in ing.active) and "cam2" not in ing.health
        closed = ing.conn.execute("SELECT closure_reason FROM tracks WHERE stream_id='cam2'").fetchone()
        assert closed[0] == "camera_disabled"
        assert ing.conn.execute("SELECT count(*) FROM feed_health WHERE stream_id='cam2' "
                                "AND ended_utc IS NULL").fetchone()[0] == 0
        now = time.monotonic()
        assert ing.initial_feed_reason(now, "cam3") == "starting"
        assert ing.initial_feed_reason(now, "cam1") == "no_signal_yet"
        ing._on_message(None, None, types.SimpleNamespace(
            topic="detections/live", payload=_observation("cam2", 2, observed_at=2000.0)))
        assert not any(k[0] == "cam2" for k in ing.active)  # a removed camera is ignored
        ing.conn.close()


def test_rule_on_removed_camera_can_still_be_turned_off():
    from app.alerts import RuleError

    with tempfile.TemporaryDirectory() as tmp:
        ing = _ingest(tmp)
        alerts = _alerts(ing, str(Path(tmp) / "ingest.db"))
        rule = alerts.create(ing.conn, {"stream_id": "cam2", "class_label": "person"})
        alerts.set_streams({"cam1": "Camera 1", "cam2": "Camera 2"}, ["cam1"])
        for body in ({"cooldown_seconds": 90}, {"enabled": True}):
            try:
                alerts.update(ing.conn, rule["rule_id"], body)
            except RuleError:
                pass
            else:
                raise AssertionError(f"rule on a removed camera accepted {body}")
        assert alerts.update(ing.conn, rule["rule_id"], {"enabled": False})["enabled"] is False
        alerts.update(ing.conn, rule["rule_id"], {"cooldown_seconds": 90})  # off: editable
        try:
            alerts.create(ing.conn, {"stream_id": "cam2"})
        except RuleError:
            pass
        else:
            raise AssertionError("new rule on a removed camera accepted")
        alerts.close()
        ing.conn.close()


def test_camera_api_envelopes():
    import asyncio
    from app.main import create_app

    with tempfile.TemporaryDirectory() as tmp:
        state, _ = _camera_state(tmp)
        app = create_app()
        for key, value in vars(state).items():
            setattr(app.state, key, value)
        app.state.db = state.ingest.conn

        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                listed = (await client.get("/api/cameras")).json()["data"]
                assert listed["enabled_count"] == 2
                added = await client.post("/api/cameras", json={"display_name": "Dock",
                                                                "source": {"kind": "publish"}})
                assert added.status_code == 201 and added.json()["data"]["stream_id"] == "cam3"
                more = await client.post("/api/cameras", json={"display_name": "Yard",
                                                               "source": {"kind": "publish"}})
                assert more.status_code == 201 and more.json()["data"]["stream_id"] == "cam4"
                assert (await client.post("/api/cameras", content="{")).status_code == 422
                renamed = await client.patch("/api/cameras/cam3", json={"display_name": "Dock 2"})
                assert renamed.status_code == 200
                gone = await client.delete("/api/cameras/cam3?confirm=cam3")
                assert gone.status_code == 200 and gone.json()["data"]["enabled_count"] == 3
                assert (await client.delete("/api/cameras/cam3?confirm=cam3")).status_code == 404
        asyncio.run(check())
        state.alerts.close()
        state.ingest.conn.close()


def test_architecture_page_names_every_deployed_service():
    import re
    import yaml

    repo = Path(__file__).resolve().parent.parent
    compose = yaml.safe_load((repo / "compose/handheld-multi-modal.yaml").read_text())
    page = (repo / "backend/app/static/architecture.html").read_text()
    listed = set(re.findall(r'data-service="([^"]+)"', page))
    assert listed == set(compose["services"]), (listed ^ set(compose["services"]))
    for service in compose["services"].values():
        image = service["image"].split("@")[0]
        if not image.startswith("handheld/"):
            assert image in page, image
    assert 'href="/architecture.html"' in (repo / "backend/app/static/index.html").read_text()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all checks passed")
