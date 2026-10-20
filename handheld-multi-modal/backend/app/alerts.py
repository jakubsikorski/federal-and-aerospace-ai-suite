"""Rules, per-logical-track matching and verified Alert Agent dispatch (section 16).

SQLite is the authority. The Alert Agent's own dedup is an in-memory second
layer; its SSE stream is observability only and is never treated as the record.
"""
from __future__ import annotations

import calendar
from datetime import UTC, datetime
import json
import logging
import math
import sqlite3
import threading
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

log = logging.getLogger("backend.alerts")

# Initial attempt plus three retries at 2, 4 and 8 s (section 16.3).
BACKOFF_SECONDS = (2.0, 4.0, 8.0)
MAX_ATTEMPTS = 1 + len(BACKOFF_SECONDS)
MAX_RULES = 50
SNOOZE_MINUTES = (5, 15, 60)
INCIDENT_ACTIONS = ("acknowledge", "snooze", "dismiss", "false_alarm")
ZONE_MARGIN = 0.02
# Missed-detection tolerance for "in view now", in seconds of camera frames.
# Measured on the demo feed: detection of a visible dog drops out for up to
# about 1.0 s (most gaps are <=0.4 s), so shorter values flicker.
IN_VIEW_GAP = 1.2
MAX_MATCH_STATE = 5000


def _epoch(stamp: str) -> float:
    return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))


class RuleError(ValueError):
    pass


def _utc(epoch: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if epoch is None else epoch))


def _row(row: sqlite3.Row) -> dict:
    spec = json.loads(row["spec_json"] or "{}")
    return {
        "rule_id": row["rule_id"], "name": row["name"], "class_label": row["class_label"],
        "stream_id": row["stream_id"], "min_confidence": row["min_confidence"],
        "enabled": bool(row["enabled"]), "cooldown_seconds": spec.get("cooldown_seconds", 30),
        "speak": spec.get("speak", True),
        "dwell_seconds": spec.get("dwell_seconds"),
        "active_hours": spec.get("active_hours"),
        "zone": spec.get("zone"),
        "created_utc": row["created_utc"], "updated_utc": row["updated_utc"],
    }


def list_rules(conn: sqlite3.Connection) -> list[dict]:
    return [_row(r) for r in conn.execute("SELECT * FROM rules ORDER BY created_utc, rule_id")]


def _time_minutes(value: str) -> int:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise RuleError("active_hours start and end must be HH:MM")
    try:
        hour, minute = int(value[:2]), int(value[3:])
    except ValueError:
        raise RuleError("active_hours start and end must be HH:MM") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise RuleError("active_hours start and end must be HH:MM")
    return hour * 60 + minute


def _area(points: list[list[float]]) -> float:
    total = 0.0
    for index, (x1, y1) in enumerate(points):
        x2, y2 = points[(index + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _crosses(points: list[list[float]]) -> bool:
    """True when two non-adjacent edges of the closed outline intersect."""
    def turn(a, b, c):
        value = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return (value > 1e-12) - (value < -1e-12)

    def on(a, b, c):
        return min(a[0], b[0]) <= c[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= c[1] <= max(a[1], b[1])

    edges = [(points[i], points[(i + 1) % len(points)]) for i in range(len(points))]
    for i, (a, b) in enumerate(edges):
        for j in range(i + 1, len(edges)):
            if j == i + 1 or (i == 0 and j == len(edges) - 1):
                continue
            c, d = edges[j]
            t1, t2, t3, t4 = turn(a, b, c), turn(a, b, d), turn(c, d, a), turn(c, d, b)
            if (t1 != t2 and t3 != t4) or (t1 == 0 and on(a, b, c)) or (t2 == 0 and on(a, b, d)) \
                    or (t3 == 0 and on(c, d, a)) or (t4 == 0 and on(c, d, b)):
                return True
    return False


def _point_in_polygon(point: tuple[float, float], points: list[list[float]]) -> bool:
    x, y = point
    inside = False
    j = len(points) - 1
    for i, (xi, yi) in enumerate(points):
        xj, yj = points[j]
        if ((yi > y) != (yj > y)) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _edge_distance(point: tuple[float, float], points: list[list[float]]) -> float:
    x, y = point
    nearest = float("inf")
    for index, (x1, y1) in enumerate(points):
        x2, y2 = points[(index + 1) % len(points)]
        dx, dy = x2 - x1, y2 - y1
        length2 = dx * dx + dy * dy
        if length2:
            t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / length2))
            px, py = x1 + t * dx, y1 + t * dy
        else:
            px, py = x1, y1
        nearest = min(nearest, math.hypot(x - px, y - py))
    return nearest


class Alerts:
    """Rule cache for ingest, the dispatch worker and the agent SSE observer."""

    def __init__(self, cfg: dict, db_path: str, arbiter, labels, *, client: httpx.Client | None = None):
        from .core import connect

        self.cfg = cfg
        self.db_path = db_path
        self.arbiter = arbiter
        self.labels = labels  # callable -> detector taxonomy, read on each validation
        self.url = cfg["services"]["alert_agent"].rstrip("/")
        self.tools = list(cfg.get("alert_tools") or ["log_alert"])
        self.timeout = float(cfg.get("alert_dispatch_timeout_seconds", 15.0))
        self.client = client or httpx.Client(timeout=self.timeout)
        self.conn = connect(db_path)
        # Section 7: one writer. main.py points this at Ingest.write so dispatch
        # updates never contend with the ingest transaction for the WAL lock.
        self.writer = self._write
        self.names = {s["stream_id"]: s.get("display_name") or s["stream_id"]
                      for s in cfg["streams"]}
        self.enabled_streams = [s["stream_id"] for s in cfg["streams"] if s.get("enabled", True)]
        self.rules: tuple[dict, ...] = ()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.events = {"connected": False, "received": 0, "confirmed": 0,
                       "reconnects": 0, "last_event_utc": None, "last_error": None}
        self.confirmed: dict[str, str] = {}
        # match_id -> wall-clock stage times, process-local, for the latency readout.
        self.latency: dict[str, dict] = {}
        # Bumped when an operator-visible alert fact changes, so the live
        # stream pushes it at once instead of on the next 1 s data tick.
        self.version = 0
        # Alerted track_id -> head match_id of its incident (latency and crop push).
        self.alert_tracks: dict[int, str] = {}
        self.inside: dict[tuple[str, int], bool] = {}
        self.dwell: dict[tuple[str, int], dict] = {}
        self.threads: list[threading.Thread] = []
        self.reload()

    def _write(self, operation):
        result = operation(self.conn)
        self.conn.commit()
        return result

    # -- rules (API connections) -------------------------------------------
    def reload(self, conn: sqlite3.Connection | None = None) -> None:
        previous = {r["rule_id"]: r for r in getattr(self, "rules", ())}
        rules = [r for r in list_rules(conn or self.conn) if r["enabled"]]
        fields = ("class_label", "stream_id", "min_confidence", "dwell_seconds", "active_hours", "zone")
        for rule in rules:
            old = previous.get(rule["rule_id"])
            # Ingest invalidates pending timers on its own thread. Cosmetic edits
            # keep continuity; changed conditions and re-enabling start afresh.
            rule["match_revision"] = (old["match_revision"] if old and all(
                old[k] == rule[k] for k in fields) else uuid.uuid4().hex)
        self.rules = tuple(rules)

    def validate(self, body: dict, current: dict | None = None) -> dict:
        allowed = {"name", "class_label", "stream_id", "min_confidence",
                   "cooldown_seconds", "speak", "enabled",
                   "dwell_seconds", "active_hours", "zone"}
        if not isinstance(body, dict) or set(body) - allowed:
            raise RuleError("unknown rule field")
        rule = {**(current or {"stream_id": None, "min_confidence": None,
                               "cooldown_seconds": 30, "speak": True, "enabled": True,
                               "dwell_seconds": None, "active_hours": None,
                               "zone": None, "class_label": None}), **body}
        label = rule.get("class_label")
        if label is not None and (not isinstance(label, str) or label not in self.labels()):
            raise RuleError("class_label must be a detector class or null for any detected object")
        # A rule left on a camera that was turned off or removed can still be
        # edited into the off state; it cannot be (re)enabled or moved there.
        moved = current is None or rule["stream_id"] != current.get("stream_id")
        if (rule["stream_id"] is not None and rule["stream_id"] not in self.enabled_streams
                and (moved or rule["enabled"])):
            raise RuleError("stream_id must be an enabled camera or null for every camera")
        conf = rule["min_confidence"]
        if conf is not None and (isinstance(conf, bool) or not isinstance(conf, (int, float))
                                 or not 0.0 <= conf <= 1.0):
            raise RuleError("min_confidence must be between 0 and 1 or null")
        cooldown = rule["cooldown_seconds"]
        if isinstance(cooldown, bool) or not isinstance(cooldown, int) or not 5 <= cooldown <= 3600:
            raise RuleError("cooldown_seconds must be a whole number from 5 to 3600")
        for flag in ("speak", "enabled"):
            if not isinstance(rule[flag], bool):
                raise RuleError(f"{flag} must be true or false")
        dwell = rule["dwell_seconds"]
        if dwell is not None and (isinstance(dwell, bool) or not isinstance(dwell, int)
                                  or not 1 <= dwell <= 3600):
            raise RuleError("dwell_seconds must be a whole number from 1 to 3600 or null")
        active = rule["active_hours"]
        if active is not None:
            if not isinstance(active, dict) or set(active) != {"start", "end", "timezone"}:
                raise RuleError("active_hours must include start, end and timezone")
            start, end = _time_minutes(active["start"]), _time_minutes(active["end"])
            if start == end:
                raise RuleError("active_hours start and end must be different")
            tz = active["timezone"]
            if not isinstance(tz, str):
                raise RuleError("active_hours timezone must be an IANA timezone name")
            try:
                ZoneInfo(tz)
            except ZoneInfoNotFoundError:
                raise RuleError("active_hours timezone must be an IANA timezone name") from None
        zone = rule["zone"]
        if zone is not None:
            if not isinstance(zone, dict) or set(zone) != {"points"}:
                raise RuleError("zone must include points")
            points = zone["points"]
            if not isinstance(points, list) or not 3 <= len(points) <= 5:
                raise RuleError("zone points must contain 3 to 5 points")
            normalised = []
            for point in points:
                if (not isinstance(point, list) or len(point) != 2
                        or any(isinstance(v, bool) or not isinstance(v, (int, float))
                               or not math.isfinite(v) or not 0 <= v <= 1 for v in point)):
                    raise RuleError("zone points must be [x, y] values from 0 to 1")
                normalised.append([float(point[0]), float(point[1])])
            if len({(round(x, 6), round(y, 6)) for x, y in normalised}) < 3:
                raise RuleError("zone must have at least 3 distinct points")
            # Older saved outlines are left alone so they can still be turned off.
            if "zone" in body and _crosses(normalised):
                raise RuleError("zone outline must not cross itself")
            if _area(normalised) < 0.0001:
                raise RuleError("zone area is too small")
            rule["zone"] = {"points": normalised}
        camera = self.names.get(rule['stream_id'], 'any camera')
        default_name = (f"{label or 'Anything detected'} stays in {camera} for {dwell} seconds"
                        if dwell else f"{label or 'Anything detected'} on {camera}")
        name = rule.get("name") or default_name[:60]
        if not isinstance(name, str) or not name.strip() or len(name) > 60:
            raise RuleError("name must be 1 to 60 characters")
        rule["name"] = name.strip()
        return rule

    def _spec(self, rule: dict) -> dict:
        return {k: rule[k] for k in ("cooldown_seconds", "speak", "dwell_seconds",
                                     "active_hours", "zone")}

    def create(self, conn: sqlite3.Connection, body: dict) -> dict:
        rule = self.validate(body)
        if conn.execute("SELECT count(*) FROM rules").fetchone()[0] >= MAX_RULES:
            raise RuleError(f"at most {MAX_RULES} rules")
        now = _utc()
        rule_id = "r" + uuid.uuid4().hex[:8]
        self.writer(lambda c: c.execute(
            """INSERT INTO rules (rule_id, name, class_label, stream_id, min_confidence,
                   enabled, created_utc, updated_utc, spec_json) VALUES (?,?,?,?,?,?,?,?,?)""",
            (rule_id, rule["name"], rule["class_label"], rule["stream_id"],
             rule["min_confidence"], int(rule["enabled"]), now, now,
             json.dumps(self._spec(rule)))))
        self.reload(conn)
        return self.get(conn, rule_id)

    def get(self, conn: sqlite3.Connection, rule_id: str) -> dict | None:
        row = conn.execute("SELECT * FROM rules WHERE rule_id = ?", (rule_id,)).fetchone()
        return _row(row) if row else None

    def update(self, conn: sqlite3.Connection, rule_id: str, body: dict) -> dict | None:
        current = self.get(conn, rule_id)
        if current is None:
            return None
        base = {k: current[k] for k in ("name", "class_label", "stream_id", "min_confidence",
                                        "cooldown_seconds", "speak", "enabled",
                                        "dwell_seconds", "active_hours", "zone")}
        rule = self.validate(body, base)
        self.writer(lambda c: c.execute(
            """UPDATE rules SET name=?, class_label=?, stream_id=?, min_confidence=?,
                   enabled=?, updated_utc=?, spec_json=? WHERE rule_id=?""",
            (rule["name"], rule["class_label"], rule["stream_id"], rule["min_confidence"],
             int(rule["enabled"]), _utc(),
             json.dumps(self._spec(rule)),
             rule_id)))
        if not rule["enabled"]:
            self._close_rule_incidents(rule_id, "rule_disabled")
        if not (rule["enabled"] and rule["speak"]):
            self._silence(rule_id=rule_id)
        self.reload(conn)
        return self.get(conn, rule_id)

    def delete(self, conn: sqlite3.Connection, rule_id: str) -> bool:
        # Matches stay: an alert that was raised is history, not rule config.
        deleted = self.writer(lambda c: c.execute(
            "DELETE FROM rules WHERE rule_id = ?", (rule_id,)).rowcount)
        self._close_rule_incidents(rule_id, "rule_deleted")
        self._silence(rule_id=rule_id)
        self.reload(conn)
        return bool(deleted)

    def _silence(self, **target) -> None:
        # Historical matches and dispatch stay; only unheard speech is withdrawn.
        if self.arbiter is not None:
            self.arbiter.cancel(**target)

    def _close_rule_incidents(self, rule_id: str, closure: str) -> None:
        self.writer(lambda c: c.execute(
            """UPDATE alert_incidents SET state='closed', closed_utc=?, closure=?
               WHERE rule_id=? AND state!='closed'""", (_utc(), closure, rule_id)))

    # -- incidents (operator actions, API connections) ----------------------
    def act(self, incident_id: str, action: str, minutes: int | None = None) -> bool:
        """Apply an operator action to an incident that is not closed yet."""
        if action not in INCIDENT_ACTIONS:
            raise RuleError("unknown incident action")
        now = _utc()
        if action == "snooze":
            if minutes not in SNOOZE_MINUTES:
                raise RuleError(f"minutes must be one of {', '.join(map(str, SNOOZE_MINUTES))}")
            # Acknowledge and snooze mean "seen so far": the count restarts.
            sql = ("UPDATE alert_incidents SET state='acknowledged', acknowledged_utc=?, "
                   "snoozed_until_utc=?, match_count=0, counted_from_utc=?, "
                   "counted_after=(SELECT coalesce(max(rowid), 0) FROM rule_matches WHERE incident_id=alert_incidents.incident_id) "
                   "WHERE incident_id=? AND state!='closed'")
            args = (now, _utc(time.time() + minutes * 60), now, incident_id)
        elif action == "acknowledge":
            sql = ("UPDATE alert_incidents SET state='acknowledged', acknowledged_utc=?, "
                   "match_count=0, counted_from_utc=?, counted_after=(SELECT coalesce(max(rowid), 0) FROM rule_matches WHERE incident_id=alert_incidents.incident_id) "
                   "WHERE incident_id=? AND state='open'")
            args = (now, now, incident_id)
        else:
            sql = ("UPDATE alert_incidents SET state='closed', closed_utc=?, closure=?, "
                   "acknowledged_utc=coalesce(acknowledged_utc, ?) "
                   "WHERE incident_id=? AND state!='closed'")
            args = (now, "false_alarm" if action == "false_alarm" else "dismissed", now, incident_id)
        changed = bool(self.writer(lambda c: c.execute(sql, args).rowcount))
        if changed:
            self._silence(group=incident_id)
        return changed

    def remove(self, incident_id: str) -> bool:
        """Drop an incident from the alert list. Its matches stay as dispatch history;
        a later sighting opens a fresh incident."""
        removed = bool(self.writer(lambda c: c.execute(
            "DELETE FROM alert_incidents WHERE incident_id=?", (incident_id,)).rowcount))
        if removed:
            self._silence(group=incident_id)
        return removed

    def speak_test(self, conn: sqlite3.Connection, incident_id: str) -> dict | None:
        """Speak an incident's announcement on demand (operator test). No rule
        cooldown and no match ref, so the incident's own speech outcome is untouched."""
        row = conn.execute(
            "SELECT class_label, stream_id FROM alert_incidents WHERE incident_id=?",
            (incident_id,)).fetchone()
        if row is None or self.arbiter is None:
            return None
        return self.arbiter.say("alert", self.announcement(dict(row)), bypass_alert_floor=True)

    def speak_open(self, conn: sqlite3.Connection) -> dict | None:
        """Announce alerts still needing attention (e.g. when Audio is turned on)."""
        rows = conn.execute(
            """SELECT class_label, stream_id FROM alert_incidents WHERE state='open'
               ORDER BY last_match_utc DESC, opened_utc DESC""").fetchall()
        if not rows or self.arbiter is None:
            return None
        names = [self.announcement(dict(r)).rstrip(".") for r in rows[:3]]
        if len(rows) == 1:
            text = names[0] + "."
        else:
            more = len(rows) - len(names)
            text = (f"{len(rows)} alerts need attention: " + ", ".join(names)
                    + (f", and {more} more." if more else "."))
        return self.arbiter.say("alert", text, bypass_alert_floor=True)

    def close_quiet(self, now: float | None = None) -> int:
        """Handled incidents close after a quiet cooldown (and after any snooze).

        Unacknowledged incidents stay open until the operator acts, so a
        sighting is never cleared without someone having seen it.
        """
        now = time.time() if now is None else now
        cooldowns = {r["rule_id"]: r["cooldown_seconds"] for r in self.rules}
        stamp = _utc(now)

        def close(c):
            # Read and write in one writer call: a snooze or sighting applied
            # between a separate read and this UPDATE would otherwise be lost.
            rows = c.execute(
                """SELECT incident_id, rule_id, last_match_utc, snoozed_until_utc
                   FROM alert_incidents WHERE state='acknowledged'""").fetchall()
            closed = 0
            for row in rows:
                if now - _epoch(row["last_match_utc"]) < cooldowns.get(row["rule_id"], 30):
                    continue
                if row["snoozed_until_utc"] and _epoch(row["snoozed_until_utc"]) > now:
                    continue
                closed += c.execute(
                    """UPDATE alert_incidents SET state='closed', closed_utc=?, closure='quiet'
                       WHERE incident_id=? AND state='acknowledged' AND last_match_utc=?
                       AND snoozed_until_utc IS ?""",
                    (stamp, row["incident_id"], row["last_match_utc"],
                     row["snoozed_until_utc"])).rowcount
            return closed
        return self.writer(close)

    def _attach(self, conn, rule: dict, entry: dict, track_id: int, match_id: str) -> tuple[str, bool, bool]:
        """Join the rule+camera incident, or open one. Returns (id, notify, reopened)."""
        now = time.time()
        stamp = _utc(now)
        row = conn.execute(
            """SELECT * FROM alert_incidents WHERE rule_id=? AND stream_id=?
               AND state!='closed'""", (rule["rule_id"], entry["stream_id"])).fetchone()
        if row is None:
            incident_id = "i" + uuid.uuid4().hex[:12]
            conn.execute(
                """INSERT INTO alert_incidents (incident_id, rule_id, stream_id, class_label,
                       state, opened_utc, last_match_utc, match_count, last_track_id, head_match_id)
                   VALUES (?,?,?,?, 'open', ?,?, 1, ?,?)""",
                (incident_id, rule["rule_id"], entry["stream_id"], entry["class_label"],
                 stamp, stamp, track_id, match_id))
            return incident_id, True, False
        if row["state"] == "open":
            # A new object on an unhandled incident is announced again once the
            # cooldown has passed since the last announcement. Counting from the
            # last match kept busy scenes silent: every sighting restarted it.
            # A kind of object not yet announced in this incident (an "Anything"
            # rule seeing a person after a car) is announced at once.
            head = conn.execute("SELECT matched_utc FROM rule_matches WHERE match_id=?",
                                (row["head_match_id"],)).fetchone()
            announced = _epoch(head[0]) if head else _epoch(row["last_match_utc"])
            classes = {label for (label,) in conn.execute(
                """SELECT DISTINCT t.class_label FROM rule_matches m
                   JOIN tracks t ON t.track_id = m.track_id
                   WHERE m.incident_id = ? AND m.dispatch_state != 'grouped'""",
                (row["incident_id"],))}
            notify = (now - announced >= rule["cooldown_seconds"]
                      or entry["class_label"] not in classes)
            reopen = False
        else:
            # Acknowledged means "seen so far": the next sighting needs attention
            # at once. Only a snooze holds sightings back, until it ends.
            snoozed = row["snoozed_until_utc"] and now < _epoch(row["snoozed_until_utc"])
            notify = reopen = not snoozed
        conn.execute(
            """UPDATE alert_incidents SET class_label=CASE
                   WHEN match_count=0 OR class_label=? THEN ? ELSE 'object' END,
                   last_match_utc=?, match_count=match_count+1,
                   counted_from_utc=CASE WHEN match_count=0 THEN ? ELSE counted_from_utc END,
                   last_track_id=?, head_match_id=CASE WHEN ? THEN ? ELSE head_match_id END,
                   state=CASE WHEN ? THEN 'open' ELSE state END,
                   acknowledged_utc=CASE WHEN ? THEN NULL ELSE acknowledged_utc END,
                   snoozed_until_utc=CASE WHEN ? THEN NULL ELSE snoozed_until_utc END
               WHERE incident_id=?""",
            (entry["class_label"], entry["class_label"], stamp, stamp, track_id, int(notify), match_id, int(reopen), int(reopen), int(reopen),
             row["incident_id"]))
        return row["incident_id"], notify, reopen

    def retry(self, conn: sqlite3.Connection, match_id: str) -> bool:
        """Operator retry of an undelivered alert: a fresh round of attempts."""
        changed = self.writer(lambda c: c.execute(
            """UPDATE rule_matches SET dispatch_state='pending', attempts=0, last_error=NULL
               WHERE match_id=? AND dispatch_state='failed'""", (match_id,)).rowcount)
        self.wake.set()
        return bool(changed)

    # -- matching (ingest thread, ingest connection) ------------------------
    def drop_track(self, track_id: int) -> None:
        for key in list(self.inside):
            if key[1] == track_id:
                self.inside.pop(key, None)
        for key in list(self.dwell):
            if key[1] == track_id:
                self.dwell.pop(key, None)

    def _prune_match_state(self) -> None:
        for store in (self.inside, self.dwell):
            while len(store) > MAX_MATCH_STATE:
                store.pop(next(iter(store)))

    def _active_now(self, rule: dict, observed_at: float) -> bool:
        active = rule.get("active_hours")
        if active is None:
            return True
        local = datetime.fromtimestamp(observed_at, UTC).astimezone(ZoneInfo(active["timezone"]))
        now = local.hour * 60 + local.minute
        start, end = _time_minutes(active["start"]), _time_minutes(active["end"])
        if start < end:
            return start <= now < end
        return now >= start or now < end

    def _foot(self, entry: dict) -> tuple[float, float] | None:
        rect = entry.get("detection_rect")
        width, height = entry.get("frame_width"), entry.get("frame_height")
        if (not isinstance(rect, (list, tuple)) or len(rect) != 4
                or any(isinstance(v, bool) or not isinstance(v, (int, float))
                       or not math.isfinite(v) for v in rect)
                or isinstance(width, bool) or isinstance(height, bool)
                or not isinstance(width, (int, float)) or not isinstance(height, (int, float))
                or not math.isfinite(width) or not math.isfinite(height)
                or width <= 0 or height <= 0 or rect[2] <= 0 or rect[3] <= 0):
            return None
        x, y, w, h = rect
        return ((x + w / 2.0) / width, (y + h) / height)

    def _in_zone(self, rule: dict, track_id: int, entry: dict) -> bool:
        zone = rule.get("zone")
        if zone is None:
            return True
        point = self._foot(entry)
        if point is None:
            return False
        points = zone["points"]
        key = (rule["rule_id"], track_id)
        inside = _point_in_polygon(point, points)
        if inside:
            self.inside[key] = True
            return True
        if self.inside.get(key) and _edge_distance(point, points) <= ZONE_MARGIN:
            return True
        self.inside[key] = False
        return False

    def _dwell_ready(self, rule: dict, track_id: int, entry: dict, qualifies: bool) -> bool:
        dwell = rule.get("dwell_seconds")
        if dwell is None:
            return qualifies
        now = float(entry.get("last_at") or time.time())
        key = (rule["rule_id"], track_id)
        state = self.dwell.get(key)
        if state is not None and (now < state["last_qual"] or
                                  now - state["last_qual"] > max(2.0, dwell * 0.2)):
            self.dwell.pop(key, None)
            state = None
        if qualifies:
            if state is None:
                state = self.dwell[key] = {"since": now, "last_qual": now}
            else:
                state["last_qual"] = now
            return now - state["since"] >= dwell
        return False

    def evaluate(self, conn: sqlite3.Connection, track_id: int, entry: dict) -> None:
        """Called by ingest per observation of a track. One match per (rule, track)."""
        matched = entry.setdefault("matched_rules", set())
        self._prune_match_state()
        for rule in self.rules:
            key = (rule["rule_id"], track_id)
            revisions = entry.setdefault("rule_revisions", {})
            if revisions.get(rule["rule_id"]) != rule["match_revision"]:
                self.inside.pop(key, None)
                self.dwell.pop(key, None)
                revisions[rule["rule_id"]] = rule["match_revision"]
            if rule["rule_id"] in matched:
                continue
            if rule["class_label"] is not None and rule["class_label"] != entry["class_label"]:
                continue
            if rule["stream_id"] is not None and rule["stream_id"] != entry["stream_id"]:
                continue
            if rule["min_confidence"] is not None and (
                    entry.get("peak_confidence") or 0.0) < rule["min_confidence"]:
                continue
            if rule.get("zone") is not None and self._foot(entry) is None:
                self.inside.pop(key, None)
                self.dwell.pop(key, None)
                continue
            qualifies = self._active_now(rule, float(entry.get("last_at") or time.time()))
            qualifies = qualifies and self._in_zone(rule, track_id, entry)
            if not self._dwell_ready(rule, track_id, entry, qualifies):
                continue
            matched.add(rule["rule_id"])
            match_id = "m" + uuid.uuid4().hex[:12]
            inserted = conn.execute(
                """INSERT OR IGNORE INTO rule_matches
                       (match_id, rule_id, track_id, matched_utc, dispatch_state)
                   VALUES (?,?,?,?, 'pending')""",
                (match_id, rule["rule_id"], track_id, _utc())).rowcount
            if not inserted:
                continue  # already matched in an earlier process: never replayed
            self.latency[match_id] = {"detected_at": entry.get("first_at"),
                                      "qualified_at": entry.get("last_at"), "matched_at": time.time()}
            while len(self.latency) > 500:
                self.latency.pop(next(iter(self.latency)))
            incident_id, notify, reopened = self._attach(conn, rule, entry, track_id, match_id)
            head = match_id if notify else conn.execute(
                "SELECT head_match_id FROM alert_incidents WHERE incident_id=?",
                (incident_id,)).fetchone()[0]
            self.alert_tracks[track_id] = head
            while len(self.alert_tracks) > 2000:
                self.alert_tracks.pop(next(iter(self.alert_tracks)))
            if entry.get("first_crop_at") is not None or conn.execute(
                    """SELECT 1 FROM rule_matches m JOIN candidates c ON c.track_id = m.track_id
                       WHERE m.incident_id = ? AND c.image_state = 'present' LIMIT 1""",
                    (incident_id,)).fetchone():
                # A reopened alert already shows an earlier sighting's image.
                self.crop_arrived(track_id)
            # Only the match that opens (or re-announces) an incident is sent to
            # the Alert Agent and spoken; the rest are recorded as grouped.
            conn.execute("UPDATE rule_matches SET incident_id=?, dispatch_state=? WHERE match_id=?",
                         (incident_id, "pending" if notify else "grouped", match_id))
            conn.commit()
            self.version += 1
            log.info("rule %s matched track %s on %s (incident %s%s)", rule["rule_id"], track_id,
                     entry["stream_id"], incident_id, "" if notify else ", grouped")
            if not notify:
                continue
            self.wake.set()
            if rule["speak"] and self.arbiter is not None:
                # The incident decided this sighting is announced, so speech adds
                # no cooldown of its own: a per-rule one silenced the same rule
                # on another camera. The global alert floor still spaces alerts.
                self.arbiter.say("alert", self.announcement(entry), ref=match_id,
                                 rule_id=rule["rule_id"], group=incident_id)

    def mark_in_view(self, incidents: list[dict], ingest, cutoff: float) -> None:
        """Set `in_view` on each incident: what its rule asks for is on camera now.

        Any tracked object meeting the rule (camera, object type, confidence,
        area) counts, not only the tracks that matched: the tracker often gives
        the same object a new id, and that track has not matched yet.
        "Now" is the camera's newest frame, not the wall clock, so delivery
        delay does not keep an object "in view" after it left the video. An
        object counts while it was in the frames of the last IN_VIEW_GAP
        seconds, and only while the camera is still delivering (`cutoff`).
        Read-only: zone hysteresis state belongs to matching.
        """
        rules = {rule["rule_id"]: rule for rule in self.rules}
        live: dict[str, list[dict]] = {}
        for incident in incidents:
            incident["in_view"] = False
            rule = rules.get(incident["rule_id"])
            if incident["state"] == "closed" or ingest is None or rule is None:
                continue
            stream = incident["stream_id"]
            if stream not in live:
                newest = ingest.newest_frame_at(stream)
                live[stream] = [] if newest is None or newest < cutoff else [
                    entry for entry in ingest.live_tracks(stream, newest - IN_VIEW_GAP)
                    if entry.get("track_id") is not None]
            zone = rule.get("zone")
            for entry in live[stream]:
                if rule["class_label"] is not None and entry["class_label"] != rule["class_label"]:
                    continue
                if rule["min_confidence"] is not None and (
                        entry.get("peak_confidence") or 0.0) < rule["min_confidence"]:
                    continue
                point = self._foot(entry) if zone else None
                if zone is None or point is not None and (
                        _point_in_polygon(point, zone["points"])
                        or _edge_distance(point, zone["points"]) <= ZONE_MARGIN):
                    incident["in_view"] = True
                    break

    def crop_arrived(self, track_id: int) -> bool:
        """First image for an alert: record when it became available. True if alerted."""
        head = self.alert_tracks.get(track_id)
        if head is None:
            return False
        record = self.latency.get(head)
        if record is not None and "image_at" not in record:
            record["image_at"] = time.time()
        return True  # the caller bumps `version` once the crop is committed

    def announcement(self, entry: dict) -> str:
        text = entry["class_label"]
        if len(self.enabled_streams) > 1:
            text += " on " + self.names.get(entry["stream_id"], entry["stream_id"])
        return text + "."

    # -- dispatch (own thread, own connection) ------------------------------
    def set_streams(self, names: dict[str, str], enabled: list[str]) -> None:
        """Camera registry changed; replaced whole so readers never see a partial list."""
        self.names = dict(names)
        self.enabled_streams = list(enabled)

    def start(self) -> None:
        # A process that died mid-request left rows in `dispatching`. Their
        # outcome is unknown, so they are sent again (agent dedup may then
        # report `suppressed`). Delivered/suppressed rows are never re-sent.
        recovered = self.writer(lambda c: c.execute(
            "UPDATE rule_matches SET dispatch_state='pending' WHERE dispatch_state='dispatching'"
        ).rowcount)
        if recovered:
            log.warning("%d interrupted alert dispatch(es) will be resent", recovered)
        for target, name in ((self._dispatch_loop, "alert-dispatch"),
                             (self._observe_loop, "alert-agent-events")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self.threads.append(thread)

    def close(self) -> None:
        self.stopping.set()
        self.wake.set()
        # Closing the client aborts the long-lived event stream and any
        # in-flight dispatch, so shutdown is not held for a read timeout.
        self.client.close()
        for thread in self.threads:
            thread.join(timeout=2.0)
        self.conn.close()

    def _due(self) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            """SELECT m.*, r.name AS rule_name, r.spec_json, t.stream_id, t.class_label,
                      t.peak_confidence
               FROM rule_matches m JOIN tracks t ON t.track_id = m.track_id
               LEFT JOIN rules r ON r.rule_id = m.rule_id
               WHERE m.dispatch_state = 'pending'
                  OR (m.dispatch_state = 'failed' AND m.attempts < ?)
               ORDER BY m.matched_utc LIMIT 20""", (MAX_ATTEMPTS,)).fetchall()
        now = time.time()
        due = []
        for row in rows:
            if row["dispatch_state"] == "failed":
                last = calendar.timegm(time.strptime(row["last_attempt_utc"], "%Y-%m-%dT%H:%M:%SZ"))
                if now - last < BACKOFF_SECONDS[row["attempts"] - 1]:
                    continue
            due.append(row)
        return due

    def _dispatch_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                for row in self._due():
                    if self.stopping.is_set():
                        return
                    self.dispatch(row)
                self.close_quiet()
            except Exception:
                log.exception("alert dispatch pass failed")
            self.wake.wait(1.0)
            self.wake.clear()

    def request_body(self, row) -> dict:
        spec = json.loads(row["spec_json"] or "{}")
        metadata = {"track_id": row["track_id"], "rule_id": row["rule_id"],
                    "match_id": row["match_id"], "stream_id": row["stream_id"],
                    "class_label": row["class_label"], "matched_utc": row["matched_utc"],
                    "peak_confidence": row["peak_confidence"]}
        camera = self.names.get(row["stream_id"], row["stream_id"])
        return {"data": {
            "source_id": row["stream_id"],
            # Agent dedup is keyed per alert name, so each rule gets its own.
            "alert_name": "HANDHELD_" + row["rule_id"].upper(),
            "answer": "YES",
            "event_id": row["match_id"],
            "reason": f"{row['rule_name'] or row['rule_id']}: {row['class_label']} "
                      f"track {row['track_id']} on {camera}",
            "tools": self.tools,
            "dedup": {"enabled": True, "strategy": "field_hash",
                      "fields": ["metadata.track_id"],
                      "window_seconds": int(spec.get("cooldown_seconds", 30)),
                      "on_missing": "skip"},
            "metadata": metadata,
        }, "original_payload": metadata}

    def dispatch(self, row) -> str:
        attempts = row["attempts"] + 1
        self.writer(lambda c: c.execute(
            """UPDATE rule_matches SET dispatch_state='dispatching', attempts=?,
                   last_attempt_utc=? WHERE match_id=?""", (attempts, _utc(), row["match_id"])))
        state, error, started = "failed", None, time.monotonic()
        try:
            response = self.client.post(self.url + "/api/v1/actions/execute",
                                        json=self.request_body(row), timeout=self.timeout)
            if response.status_code != 200:
                error = f"HTTP {response.status_code}"
            else:
                body = response.json()
                actions = body.get("actions_taken") if isinstance(body, dict) else None
                if not isinstance(actions, list):
                    error = "response without actions_taken"
                else:
                    state = "delivered" if actions else "suppressed"
        except (httpx.HTTPError, ValueError) as exc:
            error = type(exc).__name__
        self.writer(lambda c: c.execute(
            "UPDATE rule_matches SET dispatch_state=?, last_error=? WHERE match_id=?",
            (state, error, row["match_id"])))
        if state == "delivered" and row["match_id"] in self.latency:
            self.latency[row["match_id"]].setdefault("delivered_at", time.time())
        self.version += 1
        record = {"ts": _utc(), "match_id": row["match_id"], "rule_id": row["rule_id"],
                  "track_id": row["track_id"], "stream_id": row["stream_id"],
                  "attempt": attempts, "state": state, "error": error,
                  "duration_ms": int((time.monotonic() - started) * 1000)}
        log.info("alert dispatch %s", json.dumps(record))
        return state

    # -- agent SSE (observability only) -------------------------------------
    def _observe_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                with self.client.stream("GET", self.url + "/api/v1/events",
                                        timeout=httpx.Timeout(10.0, read=40.0)) as response:
                    response.raise_for_status()
                    self.events["connected"] = True
                    self.events["last_error"] = None
                    # No replay on this stream: a reconnect resyncs from SQLite.
                    self.wake.set()
                    event = None
                    for line in response.iter_lines():
                        if self.stopping.is_set():
                            return
                        if line.startswith("event:"):
                            event = line[6:].strip()
                        elif line.startswith("data:") and event == "alert_action":
                            self._observed(line[5:].strip())
            except Exception as exc:
                self.events["last_error"] = type(exc).__name__
            if self.events["connected"]:
                self.events["reconnects"] += 1
                log.warning("alert agent event stream lost; resync from SQLite on reconnect")
            self.events["connected"] = False
            self.stopping.wait(2.0)

    def _observed(self, data: str) -> None:
        try:
            body = json.loads(data)
        except ValueError:
            return
        self.events["received"] += 1
        self.events["last_event_utc"] = _utc()
        event_id = body.get("event_id") if isinstance(body, dict) else None
        if isinstance(event_id, str) and event_id.startswith("m"):
            self.confirmed[event_id] = body.get("timestamp") or _utc()
            self.events["confirmed"] += 1
            while len(self.confirmed) > 500:
                self.confirmed.pop(next(iter(self.confirmed)))

    def status(self) -> dict:
        return {"agent_events": dict(self.events), "max_attempts": MAX_ATTEMPTS,
                "backoff_seconds": list(BACKOFF_SECONDS), "tools": self.tools}
