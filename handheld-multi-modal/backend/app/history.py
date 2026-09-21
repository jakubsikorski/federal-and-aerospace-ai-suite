"""Track history reads: `tracks_current` (section 6.5) and the recent list.

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

# Section 6.5 condition 4. Feed health is per camera, so one camera going dark
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
        # D10: the pinned tracker never says whether a confidence is a fresh
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
    """Section 6.5. All four conditions, not the insufficient v20 predicate."""
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

    # Section 6.3: overlay tracks the writer has not checkpointed yet. Keyed by
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
        """SELECT image_state FROM candidates WHERE track_id = ?
           ORDER BY (image_state = 'present') DESC, quality_score DESC LIMIT 1""",
        (track_id,)).fetchone()
    if row is None:
        return None, "none"
    state = row["image_state"]
    return (f"/api/tracks/{track_id}/snip" if state == "present" else None), state


def recent(conn: sqlite3.Connection, ingest, cfg: dict, stream_id: str,
           *, limit: int = 50, since_utc: str | None = None,
           class_labels: tuple[str, ...] = (), has_image: bool = False) -> dict:
    """Closed and open tracks for the history strip, newest first.

    Unlike `current` this is deliberately not filtered by freshness or feed
    health: it is history, and history stays true after the feed dies.
    """
    limit = max(1, min(int(limit), 200))
    identity = _identity(conn, stream_id, ingest)
    filters = {"classes": list(class_labels), "has_image": bool(has_image)}
    scope = {"kind": "tracking_session", "since_utc": since_utc, "limit": limit}
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
    matched_count = conn.execute(
        f"SELECT count(*) AS n FROM tracks t WHERE {filtered_where}",
        filtered_args).fetchone()["n"]
    rows = conn.execute(
        f"""SELECT t.* FROM tracks t WHERE {filtered_where}
            ORDER BY t.last_observed_utc DESC LIMIT ?""",
        [*filtered_args, limit + 1]).fetchall()
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
        # Section 6.3 again: an object that is still visible has its current
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
    """Alternative crops for one track, best first (section 19.3)."""
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
    """Recent persisted rule matches. Rule evaluation itself belongs to Phase 8."""
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
