# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Track history reads: `tracks_current` and the recent list.

Two rules drive everything here and both exist to stop the UI stating more than
the data supports:

* A successful SQLite query on a dead feed is still a successful query. It
  reports `data_ok: true, feed_ok: false` and `current_presence: unknown` -
  never "no person", which would be a claim the input cannot support.
* Committed rows and the ingest thread's in-memory map are merged against the
  same cutoff, and every track says which side it came from via `state_source`.
"""
from __future__ import annotations

import json
import sqlite3
import time
from urllib.parse import quote

# Feed health is per camera, so one camera going dark
# never makes another camera's answer unknown.
UNKNOWN = "unknown"


def _utc(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _identity(conn: sqlite3.Connection, stream_id: str, ingest=None) -> tuple[str, str] | None:
    """The camera's current scoped identity.

    The ingest thread's in-memory value wins when it has one: it adopts a new
    session on the first observation but only checkpoints that to `streams` on
    the next commit, so trusting SQLite alone would report `no_session_yet` for
    a camera that is demonstrably producing tracks.
    """
    if ingest is not None:
        live = ingest.identity.get(stream_id)
        if live and live[0]:
            return live
    row = conn.execute(
        "SELECT tracking_session_id, history_generation FROM streams WHERE stream_id = ?",
        (stream_id,)).fetchone()
    if row is None or not row["tracking_session_id"]:
        return None
    return row["tracking_session_id"], row["history_generation"] or ""


def _track_payload(row, *, state_source: str, snip: str | None = None,
                   snip_state: str = "none") -> dict:
    return {
        "track_id": row["track_id"],
        "stream_id": row["stream_id"],
        "class_label": row["class_label"],
        "first_observed_utc": row["first_observed_utc"],
        "last_observed_utc": row["last_observed_utc"],
        "observation_count": row["observation_count"],
        "peak_confidence": row["peak_confidence"],
        # The pinned tracker never says whether a confidence is a fresh
        # detection or a prediction, so it is always carried with its caveat.
        "confidence_freshness": row["confidence_freshness"],
        "closure_reason": row["closure_reason"] if "closure_reason" in row.keys() else None,
        "state_source": state_source,
        "snip_url": snip,
        "snip_state": snip_state,
    }


def _candidate_count(conn: sqlite3.Connection, track_id: int) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM candidates WHERE track_id = ? AND image_state = 'present'",
        (track_id,)).fetchone()["n"]


def current(conn: sqlite3.Connection, ingest, cfg: dict, stream_id: str) -> dict:
    """All four conditions, not the insufficient v20 predicate."""
    window = float(cfg.get("freshness_window_seconds", 3.0))
    now = time.time()
    cutoff_epoch = now - window
    cutoff = _utc(cutoff_epoch)

    feed = (ingest.snapshot() if ingest else {}).get(stream_id) or {}
    feed_ok = bool(feed.get("ok"))
    identity = _identity(conn, stream_id, ingest)
    if identity is None:
        return {"stream_id": stream_id, "data_ok": True, "feed_ok": feed_ok,
                "current_presence": UNKNOWN, "reason": "no_session_yet",
                "tracks": [], "counts": {}}

    rows = conn.execute(
        """SELECT * FROM tracks
           WHERE stream_id = ? AND tracking_session_id = ? AND history_generation = ?
             AND closed_at IS NULL AND last_observed_utc >= ?
           ORDER BY last_observed_utc DESC""",
        (stream_id, identity[0], identity[1], cutoff)).fetchall()
    merged = {}
    for row in rows:
        snip, snip_state = _snip(conn, row["track_id"])
        payload = _track_payload(
            row, state_source="committed", snip=snip, snip_state=snip_state)
        payload["candidate_count"] = _candidate_count(conn, row["track_id"])
        merged[row["track_id"]] = payload

    # Overlay tracks the writer has not checkpointed yet. Keyed by
    # tracker id because a live track may have no committed row - and therefore
    # no track_id - at all.
    seen_trackers = {}
    for row in rows:
        seen_trackers[row["tracker_id"]] = row["track_id"]
    live = ingest.live_tracks(stream_id, cutoff_epoch) if ingest else []
    for entry in live:
        if entry["tracker_id"] in seen_trackers:
            continue
        merged[f"live:{entry['tracker_id']}"] = {
            "track_id": None, "stream_id": stream_id,
            "class_label": entry["class_label"],
            "first_observed_utc": entry["first_observed_utc"],
            "last_observed_utc": entry["last_observed_utc"],
            "observation_count": entry["observation_count"],
            "peak_confidence": entry["peak_confidence"],
            "confidence_freshness": entry["confidence_freshness"],
            "closure_reason": None, "state_source": "live",
            "snip_url": None, "snip_state": "pending", "candidate_count": 0,
        }

    tracks = sorted(merged.values(), key=lambda t: t["last_observed_utc"], reverse=True)
    counts: dict[str, int] = {}
    for track in tracks:
        counts[track["class_label"]] = counts.get(track["class_label"], 0) + 1

    if not feed_ok:
        # The query succeeded; the input did not. Saying "nothing is there" here
        # would be a fabrication, so presence is explicitly unknown and the
        # tracks are still returned, flagged stale by the caller's feed_ok.
        return {"stream_id": stream_id, "data_ok": True, "feed_ok": False,
                "current_presence": UNKNOWN,
                "reason": feed.get("reason") or "feed_not_advancing",
                "tracks": tracks, "counts": counts}

    return {"stream_id": stream_id, "data_ok": True, "feed_ok": True,
            "current_presence": "present" if tracks else "absent",
            "reason": None, "tracks": tracks, "counts": counts}


def _snip(conn: sqlite3.Connection, track_id: int) -> tuple[str | None, str]:
    """Return the best available image URL and the truthful retained state."""
    row = conn.execute(
        """SELECT c.image_state, c.candidate_id FROM candidates c
           JOIN tracks t ON t.track_id = c.track_id WHERE c.track_id = ?
           ORDER BY (c.image_state = 'present') DESC,
                    (c.candidate_id = coalesce(t.selected_candidate_id, '')) DESC,
                    c.quality_score DESC, c.candidate_id DESC LIMIT 1""",
        (track_id,)).fetchone()
    if row is None:
        return None, "none"
    state = row["image_state"]
    # Keep the missing-file fallback endpoint, but invalidate the browser cache
    # and keyed thumbnail when a better candidate becomes available.
    version = quote(row["candidate_id"], safe="")
    return (f"/api/tracks/{track_id}/snip?v={version}" if state == "present" else None), state


def recent(conn: sqlite3.Connection, ingest, cfg: dict, stream_id: str,
           *, limit: int = 50, offset: int = 0, since_utc: str | None = None,
           class_labels: tuple[str, ...] = (), has_image: bool = False,
           prioritize: bool = True) -> dict:
    """Closed and open tracks for the history strip, newest first.

    Unlike `current` this is deliberately not filtered by freshness or feed
    health: it is history, and history stays true after the feed dies.
    """
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    identity = _identity(conn, stream_id, ingest)
    filters = {"classes": list(class_labels), "has_image": bool(has_image)}
    scope = {"kind": "tracking_session", "since_utc": since_utc, "limit": limit, "offset": offset}
    if identity is None:
        return {
            "stream_id": stream_id, "tracks": [], "truncated": False,
            "matched_count": 0, "class_counts": {}, "filters": filters, "scope": scope,
        }

    clauses = [
        "t.stream_id = ?",
        "t.tracking_session_id = ?",
        "t.history_generation = ?",
    ]
    base_args: list = [stream_id, identity[0], identity[1]]
    if since_utc:
        clauses.append("t.last_observed_utc >= ?")
        base_args.append(since_utc)
    base_where = " AND ".join(clauses)
    class_counts = {
        row["class_label"]: row["n"]
        for row in conn.execute(
            f"""SELECT t.class_label, count(*) AS n FROM tracks t
                WHERE {base_where} GROUP BY t.class_label ORDER BY t.class_label""",
            base_args)
    }

    filtered_clauses = list(clauses)
    filtered_args = list(base_args)
    if class_labels:
        filtered_clauses.append(
            f"t.class_label IN ({','.join('?' for _ in class_labels)})")
        filtered_args.extend(class_labels)
    if has_image:
        filtered_clauses.append(
            """EXISTS (SELECT 1 FROM candidates c
                       WHERE c.track_id = t.track_id AND c.image_state = 'present')""")
    filtered_where = " AND ".join(filtered_clauses)
    # A stationary chair is re-observed continuously and so outranks a person
    # seen minutes ago. Recency alone therefore buries the classes an operator
    # is looking for, and buries them below the result limit as well. Demote
    # rather than hide: every detection stays counted and reachable.
    configured = cfg.get("evidence_priority_classes", ["person"])
    priority = [label for label in (configured or []) if isinstance(label, str)]
    priority_order, priority_args = "", []
    if prioritize and priority and not class_labels:
        placeholders = ",".join("?" for _ in priority)
        priority_order = f"CASE WHEN t.class_label IN ({placeholders}) THEN 0 ELSE 1 END, "
        priority_args = priority
    scope["priority_classes"] = priority_args
    matched_count = conn.execute(
        f"SELECT count(*) AS n FROM tracks t WHERE {filtered_where}",
        filtered_args).fetchone()["n"]
    rows = conn.execute(
        f"""SELECT t.* FROM tracks t WHERE {filtered_where}
            ORDER BY {priority_order}t.last_observed_utc DESC, t.track_id DESC LIMIT ? OFFSET ?""",
        [*filtered_args, *priority_args, limit + 1, offset]).fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]

    tracks = []
    live_intervals = ingest.live_intervals(stream_id) if ingest else {}
    for row in rows:
        snip, snip_state = _snip(conn, row["track_id"])
        payload = _track_payload(
            row, state_source="committed" if row["closed_at"] else "live",
            snip=snip, snip_state=snip_state)
        payload["closed_at"] = row["closed_at"]
        intervals = [
            {"started_utc": i["started_utc"], "ended_utc": i["ended_utc"],
             "observation_count": i["observation_count"]}
            for i in conn.execute(
                """SELECT started_utc, ended_utc, observation_count FROM track_intervals
                   WHERE track_id = ? ORDER BY interval_index""", (row["track_id"],))]
        # As above: an object that is still visible has its current
        # interval buffered, not committed. Merge it rather than reporting a
        # live track as having been seen zero times.
        open_interval = live_intervals.get(row["tracker_id"]) if not row["closed_at"] else None
        if open_interval and (not intervals
                              or intervals[-1]["started_utc"] != open_interval["started_utc"]):
            intervals.append(open_interval)
        elif open_interval:
            intervals[-1] = open_interval
        payload["intervals"] = intervals
        payload["candidate_count"] = _candidate_count(conn, row["track_id"])
        tracks.append(payload)
    return {
        "stream_id": stream_id,
        "tracks": tracks,
        "truncated": truncated,
        "matched_count": matched_count,
        "class_counts": class_counts,
        "filters": filters,
        "scope": scope,
    }


def candidates(conn: sqlite3.Connection, track_id: int) -> list[dict]:
    """Alternative crops for one track, best first."""
    rows = conn.execute(
        """SELECT candidate_id, observed_at_utc, quality_score, sharpness, image_state,
                  image_width, image_height, label, confidence, confidence_freshness,
                  candidate_number, object_touches_frame, aspect_limited, detection_rect
           FROM candidates WHERE track_id = ?
           ORDER BY quality_score DESC""", (track_id,)).fetchall()
    return [{
        "candidate_id": row["candidate_id"],
        "observed_at_utc": row["observed_at_utc"],
        "quality_score": row["quality_score"],
        "sharpness": row["sharpness"],
        "image_state": row["image_state"],
        "image_width": row["image_width"], "image_height": row["image_height"],
        "label": row["label"], "confidence": row["confidence"],
        "confidence_freshness": row["confidence_freshness"],
        "candidate_number": row["candidate_number"],
        "object_touches_frame": bool(row["object_touches_frame"]),
        "aspect_limited": bool(row["aspect_limited"]),
        "detection_rect": json.loads(row["detection_rect"] or "null"),
        # Absent images keep their row: the track fact survives the file.
        "image_url": (f"/api/candidates/{row['candidate_id']}/image"
                      if row["image_state"] == "present" else None),
    } for row in rows]


def alerts(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict]:
    """Recent persisted rule matches with their dispatch state."""
    limit = max(1, min(int(limit), 100))
    rows = conn.execute(
        """SELECT m.match_id, m.rule_id, m.track_id, m.matched_utc,
                  m.dispatch_state, m.attempts, m.last_attempt_utc, m.last_error,
                  r.name AS rule_name, t.stream_id, t.class_label
           FROM rule_matches m
           JOIN tracks t ON t.track_id = m.track_id
           LEFT JOIN rules r ON r.rule_id = m.rule_id
           ORDER BY m.matched_utc DESC LIMIT ?""", (limit,)).fetchall()
    return [{
        "match_id": row["match_id"],
        "rule_id": row["rule_id"],
        "rule_name": row["rule_name"] or row["rule_id"],
        "track_id": row["track_id"],
        "stream_id": row["stream_id"],
        "class_label": row["class_label"],
        "matched_utc": row["matched_utc"],
        "dispatch_state": row["dispatch_state"],
        "attempts": row["attempts"],
        "last_attempt_utc": row["last_attempt_utc"],
        "last_error": row["last_error"],
        "snip_url": _snip(conn, row["track_id"])[0],
    } for row in rows]


def incidents(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict]:
    """Operator alert incidents: needs-attention first, then handled, then closed."""
    limit = max(1, min(int(limit), 100))
    now = _utc(time.time())
    rows = conn.execute(
        """SELECT i.*, r.name AS rule_name, r.enabled AS rule_enabled,
                  m.dispatch_state, m.attempts, m.last_error
           FROM alert_incidents i
           LEFT JOIN rules r ON r.rule_id = i.rule_id
           LEFT JOIN rule_matches m ON m.match_id = i.head_match_id
           ORDER BY CASE i.state WHEN 'open' THEN 0 WHEN 'acknowledged' THEN 1 ELSE 2 END,
                    i.last_match_utc DESC LIMIT ?""", (limit,)).fetchall()
    result = []
    for row in rows:
        track = conn.execute(
            """SELECT track_id, stream_id, class_label, first_observed_utc, last_observed_utc,
                      observation_count, peak_confidence, confidence_freshness, closure_reason
               FROM tracks WHERE track_id = ?""", (row["last_track_id"],)).fetchone()
        snip, snip_state = _snip(conn, row["last_track_id"]) if track else (None, "none")
        shown = snip
        if snip is None:
            # A new sighting has no crop yet: keep showing the alert's latest image
            # (incident level only; the track keeps its own truthful state).
            imaged = conn.execute(
                """SELECT m.track_id FROM rule_matches m WHERE m.incident_id = ?
                   AND m.track_id != ? AND EXISTS (SELECT 1 FROM candidates c
                       WHERE c.track_id = m.track_id AND c.image_state = 'present')
                   ORDER BY m.matched_utc DESC, m.rowid DESC LIMIT 1""",
                (row["incident_id"], row["last_track_id"])).fetchone()
            if imaged:
                shown = _snip(conn, imaged["track_id"])[0]
        snoozed = row["state"] == "acknowledged" and (row["snoozed_until_utc"] or "") > now
        result.append({
            "incident_id": row["incident_id"], "rule_id": row["rule_id"],
            "rule_name": row["rule_name"] or row["rule_id"],
            "rule_enabled": bool(row["rule_enabled"]) if row["rule_enabled"] is not None else None,
            "stream_id": row["stream_id"], "class_label": row["class_label"],
            "state": "snoozed" if snoozed else row["state"],
            "opened_utc": row["opened_utc"], "last_match_utc": row["last_match_utc"],
            "match_count": row["match_count"], "acknowledged_utc": row["acknowledged_utc"],
            "counted_from_utc": row["counted_from_utc"] or row["opened_utc"],
            "acknowledged_count": row["counted_after"] > 0,
            "snoozed_until_utc": row["snoozed_until_utc"] if snoozed else None,
            "closed_utc": row["closed_utc"], "closure": row["closure"],
            "head_match_id": row["head_match_id"], "dispatch_state": row["dispatch_state"],
            "attempts": row["attempts"], "last_error": row["last_error"],
            "snip_url": shown,
            "track": _track_payload(track, state_source="history", snip=snip,
                                    snip_state=snip_state) if track else None,
        })
    return result


def incident_counts(conn: sqlite3.Connection) -> dict:
    """Totals independent of the incident page size, so a summary never undercounts."""
    row = conn.execute(
        """SELECT count(*) AS total, coalesce(sum(state='open'), 0) AS open,
                  coalesce(sum(state!='closed'), 0) AS active FROM alert_incidents""").fetchone()
    return {"open": row["open"], "active": row["active"], "total": row["total"]}


def incident_tracks(conn: sqlite3.Connection, incident_id: str, *, limit: int = 200) -> list[dict] | None:
    """Tracks that matched into one incident, newest match first (bounded)."""
    if conn.execute("SELECT 1 FROM alert_incidents WHERE incident_id=?", (incident_id,)).fetchone() is None:
        return None
    rows = conn.execute(
        """SELECT t.track_id, t.stream_id, t.class_label, t.first_observed_utc, t.last_observed_utc,
                  t.observation_count, t.peak_confidence, t.confidence_freshness, t.closure_reason,
                  m.matched_utc
           FROM rule_matches m JOIN tracks t ON t.track_id = m.track_id
           JOIN alert_incidents i ON i.incident_id = m.incident_id
           WHERE m.incident_id = ? AND m.rowid > i.counted_after
           ORDER BY m.matched_utc DESC, m.rowid DESC LIMIT ?""",
        (incident_id, limit)).fetchall()
    result = []
    for row in rows:
        snip, snip_state = _snip(conn, row["track_id"])
        track = _track_payload(row, state_source="history", snip=snip, snip_state=snip_state)
        track["matched_utc"] = row["matched_utc"]
        track["candidate_count"] = _candidate_count(conn, row["track_id"])
        result.append(track)
    return result
