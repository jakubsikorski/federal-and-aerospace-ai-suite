# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Observation ingest: one MQTT subscription, demultiplexed per camera.

DLSPS publishes every pipeline instance to one topic, so `crop_context.stream_id`
is the only thing separating cameras. Tracker IDs collide freely across cameras
(each pipeline runs its own tracker from 1), which is why every key here is
(stream_id, tracking_session_id, history_generation, tracker_id) and never
tracker_id alone.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections import Counter
from pathlib import Path

import paho.mqtt.client as mqtt

log = logging.getLogger("backend.ingest")

# A track seen again within this gap continues its interval; a longer gap starts
# a new one. ponytail: a fixed gap, not a per-class model. Tune if the tracker's
# real re-acquisition latency turns out to differ.
INTERVAL_GAP_SECONDS = 2.0
# No frame at all for this long means the input stopped, which is a different
# statement from "frames arrived and contained nothing".
SIGNAL_TIMEOUT_SECONDS = 5.0
COMMIT_INTERVAL_SECONDS = 1.0
# Expiry reflection and the sidecar recovery scan are directory walks, so they
# run on their own slower cadence than the 1 s health tick.
HOUSEKEEPING_SECONDS = 30.0
# The pinned tracker does not say whether a confidence is
# detector-fresh or predicted, so it is never presented as fresh inference.
FRESHNESS = "tracker_metadata_unverified"
# Stock short-term gvatrack writes exactly 1.0 on objects it predicts rather
# than detects (verified with a tracker confidence probe). The marker is
# only applied when the selected pipeline uses such a tracker (pipelines.
# PREDICTED_CONFIDENCE); zero-term detects every frame and keeps every score.

# Columns copied straight off the producer's sidecar. Listed once
# so the INSERT, the reconciliation scan and the tests cannot drift apart.
CANDIDATE_COLUMNS = (
    "candidate_id", "stream_id", "tracking_session_id", "history_generation",
    "tracker_id", "region_id", "frame_sequence", "source_frame_id",
    "observed_at_utc", "label", "confidence", "confidence_freshness",
    "detection_rect", "crop_rect", "frame_width", "frame_height",
    "image_width", "image_height", "quality_score", "sharpness",
    "object_touches_frame", "aspect_limited", "storage_key",
    "candidate_number", "expires_at_utc", "image_state",
)


def candidate_row(event: dict) -> dict | None:
    """Flatten a producer crop event into a `candidates` row.

    Returns None when the event cannot be attributed, which is treated as a drop
    rather than an error: an unattributable crop is not evidence of anything.
    """
    storage_key = event.get("storage_key")
    candidate_id = event.get("candidate_id")
    tracker_id = event.get("tracker_id")
    if not candidate_id or not storage_key or tracker_id is None:
        return None
    return {
        "candidate_id": candidate_id,
        "stream_id": event.get("stream_id"),
        "tracking_session_id": event.get("tracking_session_id") or "",
        "history_generation": event.get("history_generation") or event.get("generation") or "",
        "tracker_id": tracker_id,
        "region_id": event.get("region_id"),
        "frame_sequence": event.get("frame_sequence"),
        "source_frame_id": event.get("source_frame_id"),
        "observed_at_utc": _utc(float(event.get("observed_at") or time.time())),
        "label": event.get("label"),
        "confidence": event.get("confidence"),
        "confidence_freshness": event.get("confidence_freshness") or FRESHNESS,
        # Stored as JSON text: these are display/debug geometry, never joined on.
        "detection_rect": json.dumps(event.get("detection_rect")),
        "crop_rect": json.dumps(event.get("crop_rect")),
        "frame_width": event.get("frame_width"), "frame_height": event.get("frame_height"),
        "image_width": event.get("image_width"), "image_height": event.get("image_height"),
        "quality_score": event.get("quality_score"), "sharpness": event.get("sharpness"),
        "object_touches_frame": int(bool(event.get("object_touches_frame"))),
        "aspect_limited": int(bool(event.get("aspect_limited"))),
        "storage_key": storage_key,
        "candidate_number": event.get("candidate_number"),
        "expires_at_utc": _utc(float(event.get("expires_at") or time.time())),
        "image_state": "present",
    }


def _utc(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class Ingest:
    """Owns its own SQLite connection; the API threads never share this one."""

    def __init__(self, cfg: dict, db_path: str):
        from .core import connect

        self.cfg = cfg
        self.conn = connect(db_path)
        self.lock = threading.Lock()
        self.known = {s["stream_id"] for s in cfg["streams"] if s.get("enabled", True)}
        self.started_at = time.monotonic()
        # Cameras added after startup get their own "starting" grace period.
        self.known_since: dict[str, float] = {}
        # Held as attributes rather than module constants so
        # a deployment can retune them without a code change.
        self.gap = float(cfg.get("absence_split_seconds", INTERVAL_GAP_SECONDS))
        self.track_timeout = float(cfg.get("track_timeout_seconds", 10.0))
        self.pending_ttl = float(cfg.get("pending_association_seconds", 120.0))
        self.min_observations = max(1, int(cfg.get("min_track_observations", 3)))
        # Set by main from the selected pipeline's tracker; None keeps every score.
        self.predicted_confidence: float | None = cfg.get("tracker_predicted_confidence")
        # stream_id -> last accepted crop_context.observed_at. Monotonic guard:
        # Mosquitto runs without persistence but QoS-1 can still redeliver, and a
        # replay from before a clear must not resurrect a retired generation.
        self.last_observed: dict[str, float] = {}
        self.identity: dict[str, tuple[str, str]] = {}
        # Adopt what the previous process recorded. Without this a restart would
        # accept a retired generation until the first live message re-seeded it,
        # which is exactly the replay window the reset handshake closes.
        for row in self.conn.execute(
                "SELECT stream_id, tracking_session_id, history_generation FROM streams"):
            if row["stream_id"] in self.known and row["tracking_session_id"]:
                self.identity[row["stream_id"]] = (row["tracking_session_id"],
                                                   row["history_generation"] or "")
        self.last_frame: dict[str, float] = {}
        # Newest frame time (observed_at) per camera, on the producer's clock.
        self.newest_frame: dict[str, float] = {}
        self.health: dict[str, tuple[bool, str]] = {}
        self.open_intervals: dict[tuple, dict] = {}
        # Active track state, checkpointed to SQLite but readable
        # immediately. Reads merge this over the committed result set so a query
        # never has to wait for the next commit to see a track that exists.
        self.active: dict[tuple, dict] = {}
        # Tracks seen fewer than min_track_observations times. Held in memory
        # only: they are never written, shown, matched by rules or given crops.
        self.tentative: dict[tuple, dict] = {}
        # Scoped identity -> monotonic deadline. A crop can arrive
        # before the observation that creates its track, so the row is parked
        # with track_id NULL and attached later. Bounded by age alone.
        self.parked: dict[tuple, float] = {}
        # Scoped identity -> wall time a crop arrived before its track was
        # admitted; moved onto the track entry for the alert latency readout.
        self.first_crop: dict[tuple, float] = {}
        # Cameras mid-clear. Their messages are discarded
        # so a drain cannot repopulate the history being deleted.
        self.draining: set[str] = set()
        # stream_id -> sidecar stems seen on the last reconciliation scan, so a
        # steady-state pass costs one directory listing and no SQLite at all.
        self.scanned: dict[str, set[str]] = {}
        self.counters: Counter = Counter()
        # Rule matcher; evaluated per observation of a logical track.
        self.alerts = None
        self.frames_since_commit = 0
        self.stopping = threading.Event()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    # -- lifecycle ---------------------------------------------------------
    def recover(self) -> int:
        """Close tracks a previous process left open.

        Must run before any current-state query is served, otherwise a restart
        would present a dead process's open tracks as currently visible.
        """
        with self.lock:
            closed = sum(self._close_tracks(s, "startup_recovery") for s in self.known)
            self._reflect_expiry()
            self.conn.commit()
        return closed

    def start(self) -> None:
        self.client.connect(self.cfg["mqtt"]["host"], self.cfg["mqtt"]["port"], keepalive=30)
        self.client.loop_start()
        self.watchdog = threading.Thread(target=self._watch, name="feed-health", daemon=True)
        self.watchdog.start()

    def close(self) -> None:
        self.stopping.set()
        # Order matters: disconnect while the network loop is still running so
        # the DISCONNECT actually goes out, then loop_stop() joins that thread
        # and join() the watchdog. Only once no thread can call _apply again is
        # it safe to close the connection - both of them take self.lock, so
        # closing first would let a waiting thread wake onto a dead handle.
        try:
            self.client.disconnect()
        finally:
            self.client.loop_stop()
            watchdog = getattr(self, "watchdog", None)
            if watchdog is not None:
                watchdog.join(timeout=5.0)
            with self.lock:
                self._flush_intervals(force=True)
                for stream_id in self.known:
                    self._close_tracks(stream_id, "shutdown")
                self.active.clear()
                self.tentative.clear()
                self.conn.commit()
                self.conn.close()

    def _on_connect(self, client, _userdata, _flags, reason, _props=None):
        topics = [(self.cfg["detections_topic"], 1), (self.cfg["snips_topic"], 1)]
        log.info("mqtt connected (%s), subscribing to %s", reason,
                 ", ".join(t for t, _ in topics))
        client.subscribe(topics)

    # -- ingest ------------------------------------------------------------
    def _on_message(self, _client, _userdata, message):
        try:
            payload = json.loads(message.payload)
        except (ValueError, UnicodeDecodeError):
            log.warning("undecodable message on %s", message.topic)
            self.counters["undecodable"] += 1
            return
        if not isinstance(payload, dict):
            return
        # The crop producer publishes its sidecar verbatim, so a crop event is a
        # flat dict tagged `event`, while observations are wrapped in `metadata`.
        if payload.get("event") == "crop_ready":
            with self.lock:
                self._apply_crop(payload)
            return
        meta = payload.get("metadata")
        if not isinstance(meta, dict):
            return
        context = meta.get("crop_context")
        if not isinstance(context, dict):
            # Without crop_context there is no camera attribution, and guessing
            # one would be worse than dropping the frame.
            log.warning("observation without crop_context dropped")
            return
        stream_id = context.get("stream_id")
        if stream_id not in self.known:
            log.warning("observation for unregistered camera %r dropped", stream_id)
            return

        observed_at = float(context.get("observed_at") or time.time())
        if observed_at < self.last_observed.get(stream_id, 0.0):
            return
        self.last_observed[stream_id] = observed_at

        with self.lock:
            if stream_id in self.draining:
                self.counters["dropped_draining"] += 1
                return
            self._apply(stream_id, context, meta, observed_at)

    def _apply(self, stream_id, context, meta, observed_at) -> None:
        identity = (context.get("tracking_session_id") or "", context.get("history_generation")
                    or context.get("generation") or "")
        if self.identity.get(stream_id) != identity:
            previous = self.identity.get(stream_id)
            self.identity[stream_id] = identity
            self.conn.execute(
                "UPDATE streams SET tracking_session_id = ?, history_generation = ? "
                "WHERE stream_id = ?", (identity[0], identity[1], stream_id))
            if previous is not None:
                # An old session's tracks must never be shown as current (6.4).
                reason = ("generation_change" if previous[1] != identity[1]
                          else "session_change")
                self._close_tracks(stream_id, reason, identity=identity)
                for key in [k for k in self.open_intervals if k[0] == stream_id]:
                    self.open_intervals.pop(key, None)
                for key in [k for k in self.active if k[0] == stream_id]:
                    entry = self.active.pop(key, None)
                    if (self.alerts is not None and entry is not None
                            and entry.get("track_id") is not None):
                        self.alerts.drop_track(entry["track_id"])
                for key in [k for k in self.tentative if k[0] == stream_id]:
                    self.tentative.pop(key, None)
                for key in [k for k in self.parked if k[0] == stream_id]:
                    self.parked.pop(key, None)
            log.info("camera %s adopted session=%s generation=%s", stream_id, *identity)

        self.last_frame[stream_id] = time.monotonic()
        self.newest_frame[stream_id] = max(observed_at, self.newest_frame.get(stream_id, observed_at))
        self._set_health(stream_id, True, None, observed_at)

        stamp = _utc(observed_at)
        for obj in meta.get("objects") or []:
            tracker_id = obj.get("id")
            detection = obj.get("detection") or {}
            label = detection.get("label")
            if tracker_id is None or not label:
                # Untracked detections are counted, not stored: without an ID
                # there is nothing to accumulate them onto.
                self._bump_untracked(stream_id)
                continue
            confidence = detection.get("confidence")
            if (self.predicted_confidence is not None and isinstance(confidence, (int, float))
                    and confidence >= self.predicted_confidence):
                confidence = None
            key = (stream_id, identity[0], identity[1], tracker_id)
            first_stamp, first_at, count = stamp, observed_at, 1
            first_frame = (context.get("frame_sequence"), context.get("source_frame_id"))
            replay = [(obj, observed_at, stamp)]
            if key not in self.active:
                seed = self.tentative.get(key)
                if seed is None or observed_at - seed["last_at"] > self.gap:
                    seed = self.tentative[key] = {"stamp": stamp, "at": observed_at,
                                                  "count": 0, "peak": None, "frame": first_frame,
                                                  "attributes": []}
                seed["count"] += 1
                # Classifier output seen before admission is replayed, with its own
                # timestamp, once the track is stored (at most min_observations entries).
                seed["attributes"].append(({name: value for name, value in obj.items()
                                            if name != "detection" and isinstance(value, dict)},
                                           observed_at, stamp))
                seed["last_at"] = observed_at
                if confidence is not None:
                    seed["peak"] = max(seed["peak"] or 0.0, confidence)
                if seed["count"] < self.min_observations:
                    continue
                del self.tentative[key]
                first_stamp, first_at, count = seed["stamp"], seed["at"], seed["count"]
                first_frame, confidence = seed["frame"], seed["peak"]
                replay = seed["attributes"]
            self.conn.execute(
                """INSERT INTO tracks (stream_id, tracking_session_id, history_generation,
                        tracker_id, class_label, class_id, first_observed_utc,
                        last_observed_utc, observation_count, peak_confidence,
                        confidence_freshness, frame_sequence_first, source_frame_id_first)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(stream_id, tracking_session_id, history_generation, tracker_id)
                   DO UPDATE SET last_observed_utc = excluded.last_observed_utc,
                        observation_count = observation_count + excluded.observation_count,
                        peak_confidence = max(coalesce(peak_confidence, 0),
                                              coalesce(excluded.peak_confidence, 0))""",
                (stream_id, identity[0], identity[1], tracker_id, label,
                 detection.get("label_id"), first_stamp, stamp, count, confidence, FRESHNESS,
                 *first_frame))
            if key in self.parked:
                self._attach_parked(key)
            entry = self.active.get(key)
            if entry is None:
                self.active[key] = entry = {
                    "stream_id": stream_id, "tracker_id": tracker_id, "class_label": label,
                    "first_observed_utc": first_stamp, "observation_count": 0,
                    "peak_confidence": confidence, "confidence_freshness": FRESHNESS,
                    "first_at": first_at,
                }
                if key in self.first_crop:
                    entry["first_crop_at"] = self.first_crop.pop(key)
            entry["last_observed_utc"] = stamp
            entry["last_at"] = observed_at
            entry["observation_count"] += count
            if confidence is not None:
                entry["peak_confidence"] = max(entry["peak_confidence"] or 0.0, confidence)
            rect = obj.get("detection_rect")
            if rect is None and all(k in obj for k in ("x", "y", "w", "h")):
                rect = [obj.get(k) for k in ("x", "y", "w", "h")]
            entry["detection_rect"] = rect
            # DLSPS publishes the frame size as metadata width/height.
            for field, meta_field in (("frame_width", "width"), ("frame_height", "height")):
                value = obj.get(field, context.get(field, meta.get(meta_field)))
                entry[field] = value
            entry["object_touches_frame"] = bool(
                obj.get("object_touches_frame", context.get("object_touches_frame", False)))
            if count > 1:
                self._touch_interval(key, first_at, first_stamp, count - 1)
            self._touch_interval(key, observed_at, stamp)
            for attributes, seen_at, seen_stamp in replay:
                self._store_attributes(attributes, key, entry, seen_at, seen_stamp)
            if self.alerts is not None and self.alerts.rules:
                if entry.get("track_id") is None:
                    entry["track_id"] = self._track_id(key)
                if entry["track_id"] is not None:
                    self.alerts.evaluate(self.conn, entry["track_id"], entry)

        self.frames_since_commit += 1
        if self.frames_since_commit >= 30:
            self._flush_intervals()
            self.conn.commit()
            self.frames_since_commit = 0

    def _store_attributes(self, obj, key, entry, observed_at, stamp):
        # Stock classification dictionaries sit beside detection; names are data.
        source = {}
        for name, value in obj.items():
            if name == "detection" or not isinstance(value, dict):
                continue
            label = value.get("label")
            if (isinstance(name, str) and 0 < len(name) <= 128
                    and isinstance(label, str) and 0 < len(label) <= 256):
                model = value.get("model")
                model = model.get("name") if isinstance(model, dict) else None
                source[name] = {"label": label, "model": str(model)[:256] if model else None}
        if not source or len(source) > 32:
            entry.pop("attribute_sample", None)
            return
        signature = json.dumps(source, sort_keys=True, separators=(",", ":"))
        previous = entry.get("attribute_sample")
        if previous and previous[0] == signature and 0 <= observed_at - previous[2] <= self.gap:
            sample_id = previous[1]
            self.conn.execute("UPDATE attribute_samples SET ended_utc=?, observation_count=observation_count+1 WHERE sample_id=?",
                              (stamp, sample_id))
        else:
            cursor = self.conn.execute("""INSERT INTO attribute_samples
                (track_id, started_utc, ended_utc, attributes_json, source_json)
                VALUES (?,?,?,?,?)""", (self._track_id(key), stamp, stamp,
                json.dumps({name: value["label"] for name, value in source.items()}, sort_keys=True), signature))
            sample_id = cursor.lastrowid
        entry["attribute_sample"] = (signature, sample_id, observed_at)

    def _apply_crop(self, event: dict) -> None:
        """Attach a finished crop to its track, or park it."""
        stream_id = event.get("stream_id")
        if stream_id not in self.known:
            self.counters["crop_unregistered_camera"] += 1
            return
        if stream_id in self.draining:
            self.counters["dropped_draining"] += 1
            return
        row = candidate_row(event)
        if row is None:
            self.counters["crop_malformed"] += 1
            return
        identity = (row["tracking_session_id"], row["history_generation"])
        current = self.identity.get(stream_id)
        if current is not None and current[1] and identity[1] != current[1]:
            # Step 9 of the reset handshake. A QoS-1 message queued before a
            # clear still carries the retired generation, so this is a string
            # comparison and not a heuristic. Only the generation is compared:
            # after a clear the new session id is not known until the restarted
            # pipeline announces one, and the generation alone already retires
            # the old backlog.
            self.counters["stale_generation_rejected"] += 1
            return
        key = (stream_id, identity[0], identity[1], row["tracker_id"])
        row["track_id"] = self._track_id(key)
        cols = ", ".join(CANDIDATE_COLUMNS) + ", track_id"
        holes = ", ".join(":" + c for c in CANDIDATE_COLUMNS) + ", :track_id"
        # Dedup is mandatory, not defensive: the producer republishes on restart
        # after a broker ack, and the reconciliation scan replays sidecars too.
        cursor = self.conn.execute(
            f"INSERT INTO candidates ({cols}) VALUES ({holes}) "
            "ON CONFLICT(candidate_id) DO NOTHING", row)
        if not cursor.rowcount:
            self.counters["crop_duplicate"] += 1
            return
        self.counters["crop_ingested"] += 1
        entry = self.active.get(key)
        if entry is not None:
            entry.setdefault("first_crop_at", time.time())
        else:
            self.first_crop.setdefault(key, time.time())
            while len(self.first_crop) > 5000:
                self.first_crop.pop(next(iter(self.first_crop)))
        if self.alerts is not None and row["track_id"] is not None and self.alerts.crop_arrived(row["track_id"]):
            # An alerted track's image is shown at once, not after the batched commit.
            # Commit before announcing, so the pushed alert already sees the crop.
            self.conn.commit()
            self.alerts.version += 1
        if row["track_id"] is None:
            # No track yet. Park by scoped identity and attach when it appears.
            self.parked.setdefault(key, time.monotonic() + self.pending_ttl)
            self.counters["crop_parked"] += 1

    def _track_id(self, key) -> int | None:
        stream_id, session, generation, tracker_id = key
        found = self.conn.execute(
            """SELECT track_id FROM tracks WHERE stream_id=? AND tracking_session_id=?
               AND history_generation=? AND tracker_id=?""",
            (stream_id, session, generation, tracker_id)).fetchone()
        return found["track_id"] if found else None

    def _attach_parked(self, key) -> None:
        """A track just appeared; adopt any crops that beat it here."""
        if self.parked.pop(key, None) is None:
            return
        track_id = self._track_id(key)
        if track_id is None:
            return
        stream_id, session, generation, tracker_id = key
        cursor = self.conn.execute(
            """UPDATE candidates SET track_id = ? WHERE track_id IS NULL AND stream_id = ?
               AND tracking_session_id = ? AND history_generation = ? AND tracker_id = ?""",
            (track_id, stream_id, session, generation, tracker_id))
        if cursor.rowcount:
            self.counters["crop_attached"] += 1
            log.debug("attached %d parked crop(s) to track %s", cursor.rowcount, track_id)

    def _expire_parked(self) -> None:
        now = time.monotonic()
        for key, deadline in list(self.parked.items()):
            if now > deadline:
                self.parked.pop(key, None)
                self.counters["crop_parked_expired"] += 1

    # -- closure -----------------------------------------------------------
    def _close_tracks(self, stream_id: str, reason: str, *, before_utc: str | None = None,
                      identity: tuple[str, str] | None = None) -> int:
        """Close open tracks. Valid history is never deleted, only closed."""
        sql = ["UPDATE tracks SET closed_at = ?, closure_reason = ?",
               "WHERE closed_at IS NULL AND stream_id = ?"]
        args: list = [_utc(time.time()), reason, stream_id]
        if before_utc is not None:
            sql.append("AND last_observed_utc < ?")
            args.append(before_utc)
        if identity is not None:
            # Session/generation change: close what the *previous* identity left
            # open, never the rows the new one is about to create.
            sql.append("AND (tracking_session_id != ? OR history_generation != ?)")
            args.extend(identity)
        closed = self.conn.execute(" ".join(sql), args).rowcount
        if closed:
            log.info("camera %s closed %d track(s) as %s", stream_id, closed, reason)
            self.counters["closed_" + reason] += closed
        return closed

    def _close_timed_out(self) -> None:
        cutoff = _utc(time.time() - self.track_timeout)
        for stream_id in self.known:
            # The in-memory map is pruned whether or not SQLite had a row to
            # close, otherwise a timed-out track would stay "live" forever.
            for key, entry in list(self.active.items()):
                if key[0] == stream_id and _utc(entry["last_at"]) < cutoff:
                    self.active.pop(key, None)
                    if self.alerts is not None and entry.get("track_id") is not None:
                        self.alerts.drop_track(entry["track_id"])
            for key, seed in list(self.tentative.items()):
                if key[0] == stream_id and _utc(seed["last_at"]) < cutoff:
                    self.tentative.pop(key, None)
                    self.counters["short_tracks_discarded"] += 1
            if not self._close_tracks(stream_id, "timeout", before_utc=cutoff):
                continue
            # Drop the buffered interval too, so a tracker id that is reused
            # later opens a new interval instead of extending a closed track.
            for key, span in list(self.open_intervals.items()):
                if key[0] == stream_id and _utc(span["last_at"]) < cutoff:
                    self._write_interval(key, span)
                    self.open_intervals.pop(key, None)

    def _reflect_expiry(self) -> None:
        """The producer expires files; Backend reflects that.

        Track facts survive their image. A candidate whose retention has passed
        becomes `expired`, which the image API turns into a typed 404 so the UI
        shows a state instead of a broken image.
        """
        changed = self.conn.execute(
            "UPDATE candidates SET image_state = 'expired' "
            "WHERE image_state = 'present' AND expires_at_utc <= ?",
            (_utc(time.time()),)).rowcount
        if changed:
            self.counters["crop_expired"] += changed
        self._reap_unattached()

    def _reap_unattached(self) -> None:
        """Delete crops that never found a track.

        A parked crop is stored with `track_id` NULL so the image is durable
        before its observation arrives. If the observation never comes -- the
        feed dropped between the two topics, or a producer published snips with
        nothing publishing observations -- `_expire_parked` forgets the
        in-memory entry but the row stays, unreachable through every read path
        and never cleaned up. 406 such rows were observed accumulating.

        The cutoff is twice the parking window, not the window itself: the
        in-memory deadline is set when the crop *arrives*, while this compares
        the producer's `observed_at`, which is already ~20 s older.
        Reaping on the window alone would delete rows ~20 s before
        `_attach_parked` stops trying to use them.
        """
        self.conn.execute("DELETE FROM attribute_samples WHERE ended_utc < ?",
                          (_utc(time.time() - self.cfg.get("history_retention_seconds", 604800)),))
        removed = self.conn.execute(
            "DELETE FROM candidates WHERE track_id IS NULL AND observed_at_utc < ?",
            (_utc(time.time() - self.pending_ttl * 2),)).rowcount
        if removed:
            self.counters["crop_unattached_reaped"] += removed
            log.info("reaped %d crop(s) that never found a track", removed)

    def _touch_interval(self, key, observed_at, stamp, count=1) -> None:
        span = self.open_intervals.get(key)
        if span and observed_at - span["last_at"] <= self.gap:
            span["last_at"], span["ended"] = observed_at, stamp
            span["count"] += count
            return
        if span:
            self._write_interval(key, span)
        self.open_intervals[key] = {
            "index": (span["index"] + 1) if span else 0,
            "started": stamp, "ended": stamp, "last_at": observed_at, "count": count,
        }

    def _write_interval(self, key, span) -> None:
        stream_id, session, generation, tracker_id = key
        row = self.conn.execute(
            """SELECT track_id FROM tracks WHERE stream_id=? AND tracking_session_id=?
               AND history_generation=? AND tracker_id=?""",
            (stream_id, session, generation, tracker_id)).fetchone()
        if not row:
            return
        self.conn.execute(
            """INSERT INTO track_intervals (track_id, interval_index, stream_id, started_utc,
                    ended_utc, observation_count) VALUES (?,?,?,?,?,?)
               ON CONFLICT(track_id, interval_index) DO UPDATE SET
                    ended_utc = excluded.ended_utc,
                    observation_count = excluded.observation_count""",
            (row["track_id"], span["index"], stream_id, span["started"], span["ended"],
             span["count"]))

    def _flush_intervals(self, force: bool = False) -> None:
        for key, span in list(self.open_intervals.items()):
            # Checkpoint live spans too, without closing them. History reads must
            # include continuously visible tracks, not wait for an absence.
            self._write_interval(key, span)
            if force:
                self.open_intervals.pop(key, None)

    def _bump_untracked(self, stream_id: str) -> None:
        self.conn.execute(
            """UPDATE feed_health SET untracked_detections = coalesce(untracked_detections,0) + 1
               WHERE stream_id = ? AND ended_utc IS NULL""", (stream_id,))

    # -- feed health -------------------------------------------------------
    def _set_health(self, stream_id: str, ok: bool, reason: str | None, at: float) -> None:
        """Only transitions are written, so the table stays a timeline of gaps."""
        if self.health.get(stream_id) == (ok, reason):
            if ok:
                self.conn.execute(
                    """UPDATE feed_health SET observed_frames = coalesce(observed_frames,0) + 1
                       WHERE stream_id = ? AND ended_utc IS NULL""", (stream_id,))
            return
        stamp = _utc(at)
        self.conn.execute(
            "UPDATE feed_health SET ended_utc = ? WHERE stream_id = ? AND ended_utc IS NULL",
            (stamp, stream_id))
        self.conn.execute(
            """INSERT OR REPLACE INTO feed_health (stream_id, started_utc, ended_utc, ok,
                    reason, observed_frames, untracked_detections) VALUES (?,?,NULL,?,?,0,0)""",
            (stream_id, stamp, int(ok), reason))
        self.health[stream_id] = (ok, reason)
        log.info("camera %s feed %s%s", stream_id, "ok" if ok else "down",
                 f" ({reason})" if reason else "")

    def _watch(self) -> None:
        """No-signal is a timeout, not an absence of detections."""
        housekeeping = 0.0
        while not self.stopping.wait(1.0):
            now, wall = time.monotonic(), time.time()
            with self.lock:
                for stream_id in self.known:
                    seen = self.last_frame.get(stream_id)
                    if seen is None:
                        self._set_health(stream_id, False,
                                         self.initial_feed_reason(now, stream_id), wall)
                    elif now - seen > SIGNAL_TIMEOUT_SECONDS:
                        self._set_health(stream_id, False, "no_signal", wall)
                self._flush_intervals()
                self._close_timed_out()
                self._expire_parked()
                due = now - housekeeping >= HOUSEKEEPING_SECONDS
                if due:
                    housekeeping = now
                    self._reflect_expiry()
                self.conn.commit()
            # Outside the lock on purpose: this walks the crop directories and
            # reads files, and holding the lock across that starves MQTT intake.
            if due:
                self.reconcile_sidecars()

    # -- reconciliation ----------------------------------------------------
    def reconcile_sidecars(self) -> int:
        """Recover crops the broker never delivered.

        Mosquitto runs with `persistence false`, so a broker restart silently
        drops the queued QoS-1 backlog. The producer's own `.json` sidecars are
        the durable record, so this scan is the recovery path and is required,
        not an optimisation. Ingest is idempotent by candidate_id, so replaying
        an already-seen sidecar costs one conflicting insert and nothing else.

        Called WITHOUT the ingest lock held, and it takes the lock only to touch
        SQLite or shared state. The directory walk and the file reads are the
        slow parts and they must not block MQTT intake: an earlier version did
        the whole scan under the lock and stalled ingest for seconds at a time,
        which showed up as a sawtooth lag on every read path.
        """
        root = Path(self.cfg["crop_dir"])
        recovered = 0
        for stream_id in self.known:
            try:
                stems = {p.stem for p in (root / stream_id).glob("*.json")
                         if p.name != "status.json"}
            except OSError:
                continue
            # Only names that appeared since the last scan are candidates for
            # recovery. The set is bounded by the producer's own rotation cap,
            # and rotation removes entries from both sides, so it cannot grow
            # without limit. In the steady state `fresh` is empty and this
            # whole function is one directory listing.
            fresh = stems - self.scanned.get(stream_id, frozenset())
            self.scanned[stream_id] = stems
            if not fresh:
                continue
            known = set()
            ordered = sorted(fresh)
            with self.lock:
                for start in range(0, len(ordered), 500):
                    chunk = ordered[start:start + 500]
                    holes = ",".join("?" * len(chunk))
                    known.update(row["candidate_id"] for row in self.conn.execute(
                        f"SELECT candidate_id FROM candidates WHERE candidate_id IN ({holes})",
                        chunk))
            events = []
            for stem in ordered:
                if stem in known:
                    continue
                try:
                    event = json.loads((root / stream_id / (stem + ".json")).read_text())
                except (OSError, ValueError):
                    self.scanned[stream_id].discard(stem)
                    continue
                if isinstance(event, dict) and event.get("event") == "crop_ready":
                    events.append(event)
            if not events:
                continue
            with self.lock:
                before = self.counters["crop_ingested"]
                for event in events:
                    self._apply_crop(event)
                recovered += self.counters["crop_ingested"] - before
        if recovered:
            with self.lock:
                self.counters["crop_recovered"] += recovered
            log.info("recovered %d crop(s) from sidecars the broker did not deliver", recovered)
        return recovered

    def write(self, operation):
        """Run a write through the single writer, serialized with ingest.

        There is one writer task. A second connection issuing a bulk
        DELETE while this one commits every second does not merely contend, it
        fails: WAL allows one writer, and the loser gets `database is locked`
        as soon as the delete outlives busy_timeout. Callers that mutate history
        (the reset handshake) come through here instead of opening their own.
        """
        with self.lock:
            try:
                result = operation(self.conn)
                self.conn.commit()
                return result
            except Exception:
                self.conn.rollback()
                raise

    # -- reset handshake ---------------------------------------------------
    def set_draining(self, stream_id: str, draining: bool) -> None:
        """While draining, this camera's messages are discarded and counted."""
        with self.lock:
            if draining:
                self.draining.add(stream_id)
            else:
                self.draining.discard(stream_id)

    def forget(self, stream_id: str, generation: str | None) -> None:
        """Drop every in-memory trace of a cleared camera and adopt its new
        generation, so a message still in flight from before the clear cannot
        re-seed the old identity."""
        with self.lock:
            for store in (self.active, self.tentative, self.open_intervals, self.parked):
                for key in [k for k in store if k[0] == stream_id]:
                    store.pop(key, None)
            self.identity.pop(stream_id, None)
            if generation:
                # Session is unknown until the restarted pipeline announces one;
                # the generation alone is enough to reject the retired backlog.
                self.identity[stream_id] = ("", generation)
            self.last_observed.pop(stream_id, None)
            self.health.pop(stream_id, None)

    def set_known(self, stream_ids) -> None:
        """Camera registry changed at runtime (cameras.refresh).

        A camera that leaves closes its open tracks and feed-health interval,
        exactly as a shutdown would, so nothing it saw is reported as current.
        A camera that joins adopts its recorded identity and starts a fresh
        grace period. The set is replaced, never mutated, because other
        threads iterate it without the lock.
        """
        wanted = set(stream_ids)
        with self.lock:
            self._flush_intervals()
            now, wall = time.monotonic(), time.time()
            for stream_id in self.known - wanted:
                self._close_tracks(stream_id, "camera_disabled")
                for store in (self.open_intervals, self.tentative, self.parked):
                    for key in [k for k in store if k[0] == stream_id]:
                        store.pop(key, None)
                for key in [k for k in self.active if k[0] == stream_id]:
                    entry = self.active.pop(key)
                    if self.alerts is not None and entry.get("track_id") is not None:
                        self.alerts.drop_track(entry["track_id"])
                self.conn.execute("UPDATE feed_health SET ended_utc = ? "
                                  "WHERE stream_id = ? AND ended_utc IS NULL",
                                  (_utc(wall), stream_id))
                for store in (self.health, self.last_frame, self.known_since):
                    store.pop(stream_id, None)
            for stream_id in wanted - self.known:
                row = self.conn.execute("SELECT tracking_session_id, history_generation "
                                        "FROM streams WHERE stream_id = ?", (stream_id,)).fetchone()
                if row is not None and row["tracking_session_id"]:
                    self.identity[stream_id] = (row["tracking_session_id"],
                                                row["history_generation"] or "")
                self.known_since[stream_id] = now
            self.known = wanted
            self.conn.commit()

    # -- reads -------------------------------------------------------------
    def live_tracks(self, stream_id: str, cutoff: float) -> list[dict]:
        """Active tracks seen since `cutoff` (epoch), for the in-memory merge.

        A copy under the lock: the caller is an API thread and the ingest thread
        mutates these dicts in place on every frame.
        """
        with self.lock:
            identity = self.identity.get(stream_id)
            if identity is None:
                return []
            return [dict(entry, tracking_session_id=key[1], history_generation=key[2])
                    for key, entry in self.active.items()
                    if key[0] == stream_id and key[1:3] == identity
                    and entry["last_at"] >= cutoff]

    def newest_frame_at(self, stream_id: str) -> float | None:
        with self.lock:
            return self.newest_frame.get(stream_id)

    def live_intervals(self, stream_id: str) -> dict[int, dict]:
        """Open visibility intervals not yet checkpointed, by tracker id.

        A continuously visible object keeps its interval buffered until it goes
        quiet, so without this a live track reports zero visibility.
        """
        with self.lock:
            identity = self.identity.get(stream_id)
            if identity is None:
                return {}
            return {key[3]: {"started_utc": span["started"], "ended_utc": span["ended"],
                             "observation_count": span["count"]}
                    for key, span in self.open_intervals.items()
                    if key[0] == stream_id and key[1:3] == identity}

    def stats(self) -> dict[str, int]:
        with self.lock:
            return dict(self.counters)

    def initial_feed_reason(self, now: float, stream_id: str | None = None) -> str:
        since = self.known_since.get(stream_id, self.started_at)
        return ("starting" if now - since < float(
            self.cfg.get("pipeline_start_timeout_seconds", 180)) else "no_signal_yet")

    def snapshot(self) -> dict[str, dict]:
        return {
            stream_id: {"ok": ok, "reason": reason}
            for stream_id, (ok, reason) in self.health.items()
        }

    def stale(self, seconds: float) -> set[str]:
        """Cameras with no observation for `seconds` - including never.

        DLSPS reports a pipeline as RUNNING after its RTSP source reconnects even
        when no frame ever arrives again, so pipeline state alone cannot be used
        to decide a camera is alive.
        """
        now = time.monotonic()
        return {s for s in self.known
                if now - self.last_frame.get(s, float("-inf")) > seconds}
