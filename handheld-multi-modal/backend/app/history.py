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


def _track_payload(row, *, state_source: str, snip: str | None = None) -> dict:
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
    }


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
    merged = {row["track_id"]: _track_payload(row, state_source="committed",
                                              snip=_snip_url(conn, row["track_id"]))
              for row in rows}

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
            "closure_reason": None, "state_source": "live", "snip_url": None,
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


def _snip_url(conn: sqlite3.Connection, track_id: int) -> str | None:
    """The selected candidate, or the best available one if none was selected."""
    row = conn.execute(
        """SELECT candidate_id FROM candidates
           WHERE track_id = ? AND image_state = 'present'
           ORDER BY quality_score DESC LIMIT 1""", (track_id,)).fetchone()
    return f"/api/tracks/{track_id}/snip" if row else None


def recent(conn: sqlite3.Connection, ingest, cfg: dict, stream_id: str,
           *, limit: int = 50, since_utc: str | None = None) -> dict:
    """Closed and open tracks for the history strip, newest first.

    Unlike `current` this is deliberately not filtered by freshness or feed
    health: it is history, and history stays true after the feed dies.
    """
    limit = max(1, min(int(limit), 200))
    identity = _identity(conn, stream_id, ingest)
    if identity is None:
        return {"stream_id": stream_id, "tracks": [], "truncated": False}
    args: list = [stream_id, identity[0], identity[1]]
    clause = ""
    if since_utc:
        clause = "AND last_observed_utc >= ? "
        args.append(since_utc)
    args.append(limit + 1)
    rows = conn.execute(
        f"""SELECT * FROM tracks
            WHERE stream_id = ? AND tracking_session_id = ? AND history_generation = ?
            {clause}
            ORDER BY last_observed_utc DESC LIMIT ?""", args).fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]

    tracks = []
    live_intervals = ingest.live_intervals(stream_id) if ingest else {}
    for row in rows:
        payload = _track_payload(
            row, state_source="committed" if row["closed_at"] else "live",
            snip=_snip_url(conn, row["track_id"]))
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
        payload["candidate_count"] = conn.execute(
            "SELECT count(*) AS n FROM candidates WHERE track_id = ? AND image_state = 'present'",
            (row["track_id"],)).fetchone()["n"]
        tracks.append(payload)
    return {"stream_id": stream_id, "tracks": tracks, "truncated": truncated}


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
