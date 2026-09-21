#!/usr/bin/env python3
"""Self-check for Backend logic that can fail silently. Run: python3 backend/test_backend.py

Deliberately framework-free and offline: it covers config derivation, the
max_streams guard, schema migration, stream sync and observation demultiplexing.
Service adapters and the pipeline lifecycle are not mocked here - they are proven
against the real pinned containers via /api/health and the Phase 2 evidence,
which are the only assertions that actually mean anything for them.
"""
import json
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))

from app.core import connect, envelope, load_config, migrate, request_id_var, sync_streams


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
    assert metrics["npu_memory_mib"]["value"] is None
    assert metrics["npu_memory_mib"]["state"] == "unavailable"

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
    pipelines._swept.clear()
    pipelines._failures.clear()
    pipelines._retry_after.clear()
    pipelines._accepted_at.clear()
    conn = connect(str(Path(tmp) / "app.db"))
    migrate(conn)
    sync_streams(conn, [{"stream_id": "cam1", "display_name": "cam1", "source_uri": "u",
                         "annotated_path": "annotated1", "enabled": True, "sort_order": 0}])
    cfg = {"services": {"dlsps": "http://d", "mediamtx": "http://m"},
           "crop_dir": tmp, "takeover_seconds": 1.0,
           "supervise_seconds": 10.0, "pipeline_start_timeout_seconds": 1.0,
           "pipeline_start_poll_seconds": 0.0, "feed_stale_seconds": 90.0}
    return pipelines, conn, cfg


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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all checks passed")
