"""Rules, per-logical-track matching and verified Alert Agent dispatch (section 16).

SQLite is the authority. The Alert Agent's own dedup is an in-memory second
layer; its SSE stream is observability only and is never treated as the record.
"""
from __future__ import annotations

import calendar
import json
import logging
import sqlite3
import threading
import time
import uuid

import httpx

log = logging.getLogger("backend.alerts")

# Initial attempt plus three retries at 2, 4 and 8 s (section 16.3).
BACKOFF_SECONDS = (2.0, 4.0, 8.0)
MAX_ATTEMPTS = 1 + len(BACKOFF_SECONDS)
MAX_RULES = 50
SNOOZE_MINUTES = (5, 15, 60)
INCIDENT_ACTIONS = ("acknowledge", "snooze", "dismiss", "false_alarm")


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
        "enabled": bool(row["enabled"]), "cooldown_seconds": spec.get("cooldown_seconds", 60),
        "speak": spec.get("speak", True),
        "created_utc": row["created_utc"], "updated_utc": row["updated_utc"],
    }


def list_rules(conn: sqlite3.Connection) -> list[dict]:
    return [_row(r) for r in conn.execute("SELECT * FROM rules ORDER BY created_utc, rule_id")]


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
        self.threads: list[threading.Thread] = []
        self.reload()

    def _write(self, operation):
        result = operation(self.conn)
        self.conn.commit()
        return result

    # -- rules (API connections) -------------------------------------------
    def reload(self, conn: sqlite3.Connection | None = None) -> None:
        self.rules = tuple(r for r in list_rules(conn or self.conn) if r["enabled"])

    def validate(self, body: dict, current: dict | None = None) -> dict:
        allowed = {"name", "class_label", "stream_id", "min_confidence",
                   "cooldown_seconds", "speak", "enabled"}
        if not isinstance(body, dict) or set(body) - allowed:
            raise RuleError("unknown rule field")
        rule = {**(current or {"stream_id": None, "min_confidence": None,
                               "cooldown_seconds": 60, "speak": True, "enabled": True}), **body}
        label = rule.get("class_label")
        if not isinstance(label, str) or label not in self.labels():
            raise RuleError("class_label must be a detector class")
        if rule["stream_id"] is not None and rule["stream_id"] not in self.enabled_streams:
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
        name = rule.get("name") or f"{label} on {self.names.get(rule['stream_id'], 'any camera')}"
        if not isinstance(name, str) or not name.strip() or len(name) > 60:
            raise RuleError("name must be 1 to 60 characters")
        rule["name"] = name.strip()
        return rule

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
             json.dumps({"cooldown_seconds": rule["cooldown_seconds"], "speak": rule["speak"]}))))
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
                                        "cooldown_seconds", "speak", "enabled")}
        rule = self.validate(body, base)
        self.writer(lambda c: c.execute(
            """UPDATE rules SET name=?, class_label=?, stream_id=?, min_confidence=?,
                   enabled=?, updated_utc=?, spec_json=? WHERE rule_id=?""",
            (rule["name"], rule["class_label"], rule["stream_id"], rule["min_confidence"],
             int(rule["enabled"]), _utc(),
             json.dumps({"cooldown_seconds": rule["cooldown_seconds"], "speak": rule["speak"]}),
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
        return self.arbiter.say("alert", self.announcement(dict(row)))

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
                if now - _epoch(row["last_match_utc"]) < cooldowns.get(row["rule_id"], 60):
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
            # An unhandled incident is announced again only after a quiet spell.
            notify = now - _epoch(row["last_match_utc"]) >= rule["cooldown_seconds"]
            reopen = False
        else:
            # Acknowledged means "seen so far": the next sighting needs attention
            # at once. Only a snooze holds sightings back, until it ends.
            snoozed = row["snoozed_until_utc"] and now < _epoch(row["snoozed_until_utc"])
            notify = reopen = not snoozed
        conn.execute(
            """UPDATE alert_incidents SET last_match_utc=?, match_count=match_count+1,
                   counted_from_utc=CASE WHEN match_count=0 THEN ? ELSE counted_from_utc END,
                   last_track_id=?, head_match_id=CASE WHEN ? THEN ? ELSE head_match_id END,
                   state=CASE WHEN ? THEN 'open' ELSE state END,
                   acknowledged_utc=CASE WHEN ? THEN NULL ELSE acknowledged_utc END,
                   snoozed_until_utc=CASE WHEN ? THEN NULL ELSE snoozed_until_utc END
               WHERE incident_id=?""",
            (stamp, stamp, track_id, int(notify), match_id, int(reopen), int(reopen), int(reopen),
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
    def evaluate(self, conn: sqlite3.Connection, track_id: int, entry: dict) -> None:
        """Called by ingest per observation of a track. One match per (rule, track)."""
        matched = entry.setdefault("matched_rules", set())
        for rule in self.rules:
            if rule["rule_id"] in matched:
                continue
            if rule["class_label"] != entry["class_label"]:
                continue
            if rule["stream_id"] is not None and rule["stream_id"] != entry["stream_id"]:
                continue
            if rule["min_confidence"] is not None and (
                    entry.get("peak_confidence") or 0.0) < rule["min_confidence"]:
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
            incident_id, notify, reopened = self._attach(conn, rule, entry, track_id, match_id)
            # Only the match that opens (or re-announces) an incident is sent to
            # the Alert Agent and spoken; the rest are recorded as grouped.
            conn.execute("UPDATE rule_matches SET incident_id=?, dispatch_state=? WHERE match_id=?",
                         (incident_id, "pending" if notify else "grouped", match_id))
            conn.commit()
            log.info("rule %s matched track %s on %s (incident %s%s)", rule["rule_id"], track_id,
                     entry["stream_id"], incident_id, "" if notify else ", grouped")
            if not notify:
                continue
            self.wake.set()
            if rule["speak"] and self.arbiter is not None:
                # A sighting after Acknowledge is new information: the rule's
                # speech cooldown does not silence it (the global floor still applies).
                self.arbiter.say("alert", self.announcement(entry), ref=match_id,
                                 rule_id=rule["rule_id"],
                                 cooldown=0.0 if reopened else rule["cooldown_seconds"],
                                 group=incident_id)

    def announcement(self, entry: dict) -> str:
        text = entry["class_label"]
        if len(self.enabled_streams) > 1:
            text += " on " + self.names.get(entry["stream_id"], entry["stream_id"])
        return text + "."

    # -- dispatch (own thread, own connection) ------------------------------
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
                      "window_seconds": int(spec.get("cooldown_seconds", 60)),
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
