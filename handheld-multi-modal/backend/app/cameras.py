# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Runtime camera registry: cameras added, renamed, turned off or removed in the console.

The `streams` table is the live camera list. Config cameras (`origin = 'config'`)
still follow backend.yaml and the demo camera count on every start, except that
the operator may turn one off (`operator_off`, kept across restarts); cameras
added here (`origin = 'operator'`) exist only in SQLite. Changing the list never
edits a pipeline: the supervisor starts or stops the one fixed DLSPS definition
per enabled camera, exactly as for config cameras.

Two source kinds, both keeping video on MediaMTX -> DLSPS:
- `publish`: something on this device publishes H.264 RTSP to camN (the
  existing MediaMTX regex path accepts it; no MediaMTX change).
- `pull`: MediaMTX pulls an allowlisted network RTSP camera into camN through
  its control API. Runtime paths do not survive a MediaMTX restart, so the
  supervisor re-adds them every pass.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import sqlite3
import threading
import time
from urllib.parse import quote, urlsplit

import httpx

log = logging.getLogger("backend.cameras")

NAME_MAX = 40
URL_MAX = 512
STREAM_ID = re.compile(r"cam([1-9][0-9]{0,3})")
_lock = threading.Lock()  # one camera change at a time, process-wide


class CameraError(ValueError):
    def __init__(self, message: str, status: int = 422, code: str = "invalid_request"):
        super().__init__(message)
        self.status = status
        self.code = code


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def validate_name(value) -> str:
    if not isinstance(value, str):
        raise CameraError("display_name must be text")
    name = " ".join(value.split())
    if not 1 <= len(name) <= NAME_MAX or any(ord(c) < 32 for c in name):
        raise CameraError(f"Name must be 1–{NAME_MAX} characters.")
    return name


def validate_pull_url(url, allow: list[str]) -> str:
    """An RTSP URL MediaMTX may connect to, without credentials.

    Hostnames are accepted only when listed exactly: resolving them here would
    not bind MediaMTX to the same answer (DNS rebinding).
    """
    if not isinstance(url, str) or not url or len(url) > URL_MAX or any(
            c.isspace() or ord(c) < 32 for c in url):
        raise CameraError("Enter an rtsp:// or rtsps:// camera address.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise CameraError("Camera address is not a valid URL.") from exc
    if parts.scheme not in ("rtsp", "rtsps"):
        raise CameraError("Only rtsp:// or rtsps:// camera addresses are supported.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise CameraError("Addresses with a user name or password are not stored. "
                          "Add password-protected cameras in the MediaMTX configuration.")
    host = parts.hostname
    if not host or parts.fragment:
        raise CameraError("Camera address needs a host.")
    if port is not None and not 1 <= port <= 65535:
        raise CameraError("Camera port is out of range.")
    networks, hostnames = [], set()
    for entry in allow:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            hostnames.add(str(entry).lower())
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host.lower() not in hostnames:
            raise CameraError(f"Host {host} is not on the allowed camera list.", 403, "source_not_allowed")
        return url
    if (address.is_loopback or address.is_link_local or address.is_multicast
            or address.is_unspecified or address.is_reserved):
        raise CameraError("That address cannot be used for a camera.", 403, "source_not_allowed")
    if not any(address in network for network in networks):
        raise CameraError(f"Address {host} is outside the allowed camera networks.", 403,
                          "source_not_allowed")
    return url


def _audit(db: sqlite3.Connection, action: str, stream_id: str, detail: dict | None = None) -> None:
    db.execute("INSERT INTO camera_audit (at_utc, action, stream_id, detail) VALUES (?, ?, ?, ?)",
               (_utc(), action, stream_id, json.dumps(detail or {}, sort_keys=True)))


def listed(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    # A deployment camera the deployment has off (not selected by `make CAMERAS`,
    # or dropped from backend.yaml) cannot be turned on here, so it is not offered
    # at all; one the operator turned off is listed so it can be turned on again.
    return conn.execute("SELECT * FROM streams WHERE removed_utc IS NULL "
                        "AND (enabled = 1 OR origin = 'operator' OR operator_off = 1) "
                        "ORDER BY sort_order, stream_id").fetchall()


def next_stream_id(conn: sqlite3.Connection) -> str:
    """Lowest camN never used: a removed camera's id keeps its history, never reused."""
    used = {int(m.group(1)) for (sid,) in conn.execute("SELECT stream_id FROM streams")
            if (m := STREAM_ID.fullmatch(sid))}
    number = next(n for n in range(1, 10000) if n not in used)
    return f"cam{number}"


def refresh(state) -> list[dict]:
    """Publish the registry to every in-memory consumer after a change."""
    from .core import connect

    conn = connect(state.cfg["db_path"])
    try:
        rows = conn.execute("SELECT * FROM streams ORDER BY sort_order, stream_id").fetchall()
    finally:
        conn.close()
    extras = {s["stream_id"]: s for s in state.cfg.get("config_streams", state.cfg["streams"])}
    streams = []
    for row in rows:
        if row["removed_utc"] or (row["origin"] == "config" and row["stream_id"] not in extras):
            continue  # removed, or dropped from backend.yaml (history kept)
        stream = {**extras.get(row["stream_id"], {})} if row["origin"] == "config" else {}
        stream.update(stream_id=row["stream_id"], display_name=row["display_name"],
                      sort_order=row["sort_order"], enabled=bool(row["enabled"]),
                      source_uri=row["source_uri"], annotated_path=row["annotated_path"],
                      origin=row["origin"])
        streams.append(stream)
    state.cfg["streams"] = streams
    names = {row["stream_id"]: row["display_name"] for row in rows}
    enabled = [s["stream_id"] for s in streams if s["enabled"]]
    if getattr(state, "alerts", None) is not None:
        state.alerts.set_streams(names, enabled)
    if getattr(state, "ingest", None) is not None:
        state.ingest.set_known(enabled)
    return streams


# -- MediaMTX pull paths -----------------------------------------------------
def _path_url(mediamtx: str, verb: str, stream_id: str) -> str:
    return f"{mediamtx.rstrip('/')}/v3/config/paths/{verb}/{quote(stream_id)}"


def _mediamtx_error(reply: httpx.Response) -> str:
    try:
        return str(reply.json().get("error") or reply.text)[:200]
    except ValueError:
        return reply.text[:200]


def add_pull_path(client: httpx.Client, mediamtx: str, stream_id: str, url: str) -> None:
    body = {"source": url, "sourceOnDemand": False, "rtspTransport": "tcp"}
    reply = client.post(_path_url(mediamtx, "add", stream_id), json=body, timeout=5.0)
    if reply.status_code == 400 and "already exists" in reply.text:
        reply = client.patch(_path_url(mediamtx, "patch", stream_id), json=body, timeout=5.0)
    if reply.status_code >= 400:
        raise CameraError(f"MediaMTX refused the camera source: {_mediamtx_error(reply)}",
                          502, "source_rejected")


def delete_pull_path(client: httpx.Client, mediamtx: str, stream_id: str) -> None:
    reply = client.delete(_path_url(mediamtx, "delete", stream_id), timeout=5.0)
    if reply.status_code >= 400 and reply.status_code != 404:
        log.warning("MediaMTX kept pull path %s: %s", stream_id, _mediamtx_error(reply))


def ensure_pull_paths(conn: sqlite3.Connection, cfg: dict, client: httpx.Client) -> None:
    """Make MediaMTX's runtime paths match the enabled pull cameras."""
    mediamtx = cfg["services"]["mediamtx"]
    wanted = {row["stream_id"]: row["pull_url"] for row in conn.execute(
        "SELECT stream_id, pull_url FROM streams WHERE enabled = 1 AND removed_utc IS NULL "
        "AND pull_url IS NOT NULL")}
    try:
        reply = client.get(f"{mediamtx.rstrip('/')}/v3/config/paths/list", timeout=5.0)
        reply.raise_for_status()
        present = {item["name"]: item.get("source") for item in reply.json().get("items", [])
                   if STREAM_ID.fullmatch(str(item.get("name", "")))}
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("MediaMTX path list unavailable: %s", exc)
        return
    for stream_id, url in wanted.items():
        if present.get(stream_id) != url:
            try:
                add_pull_path(client, mediamtx, stream_id, url)
                log.info("camera %s: MediaMTX pull path restored", stream_id)
            except (CameraError, httpx.HTTPError) as exc:
                log.warning("camera %s: pull path not restored: %s", stream_id, exc)
    for stream_id in present.keys() - wanted.keys():
        delete_pull_path(client, mediamtx, stream_id)


# -- operations --------------------------------------------------------------
def view(conn: sqlite3.Connection, cfg: dict, feeds: dict) -> dict:
    rows = listed(conn)
    cameras = []
    for row in rows:
        source = ({"kind": "pull", "url": row["pull_url"]} if row["pull_url"]
                  else {"kind": "publish", "publish_url": f"rtsp://127.0.0.1:8554/{row['stream_id']}"})
        cameras.append({
            "stream_id": row["stream_id"], "display_name": row["display_name"],
            "enabled": bool(row["enabled"]), "origin": row["origin"], "source": source,
            "running": bool(row["dlsps_instance_id"]) and bool(row["enabled"]),
            "feed": feeds.get(row["stream_id"], {"ok": None, "reason": "unknown"})
            if row["enabled"] else {"ok": None, "reason": "off"},
        })
    enabled = sum(1 for row in rows if row["enabled"])
    return {"cameras": cameras, "enabled_count": enabled,
            "allowed_sources": list(cfg.get("camera_source_allow") or [])}


def _body(body, allowed: set[str]) -> dict:
    if not isinstance(body, dict) or not body or set(body) - allowed:
        raise CameraError(f"Provide only {', '.join(sorted(allowed))}.")
    return body


def _unique_name(conn, name: str, stream_id: str | None = None) -> None:
    for row in listed(conn):
        if row["stream_id"] != stream_id and row["display_name"].casefold() == name.casefold():
            raise CameraError(f"A camera is already named {row['display_name']}.", 409, "name_taken")


def _row(conn, stream_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM streams WHERE stream_id = ? AND removed_utc IS NULL",
                       (stream_id,)).fetchone()
    if row is None:
        raise CameraError("Unknown camera.", 404, "not_found")
    return row


def add(state, body) -> str:
    body = _body(body, {"display_name", "source"})
    cfg = state.cfg
    name = validate_name(body.get("display_name"))
    source = body.get("source")
    if not isinstance(source, dict) or source.get("kind") not in ("publish", "pull") or (
            set(source) - {"kind", "url"}) or (source["kind"] == "publish" and "url" in source):
        raise CameraError("source must be {kind: publish} or {kind: pull, url}.")
    url = (validate_pull_url(source.get("url"), cfg.get("camera_source_allow") or [])
           if source["kind"] == "pull" else None)
    with _lock:
        def insert(db):
            _unique_name(db, name)
            if url and db.execute("SELECT 1 FROM streams WHERE pull_url = ? AND removed_utc IS NULL",
                                  (url,)).fetchone():
                raise CameraError("That camera address is already added.", 409, "source_taken")
            stream_id = next_stream_id(db)
            order = db.execute("SELECT coalesce(max(sort_order), -1) + 1 FROM streams").fetchone()[0]
            db.execute(
                """INSERT INTO streams (stream_id, display_name, source_uri, annotated_path, enabled,
                       sort_order, origin, pull_url, name_locked, created_utc)
                   VALUES (?, ?, ?, ?, 1, ?, 'operator', ?, 1, ?)""",
                (stream_id, name, f"{cfg['rtsp_base']}/{stream_id}",
                 "annotated" + stream_id.removeprefix("cam"), order, url, _utc()))
            _audit(db, "added", stream_id, {"display_name": name, "source": source["kind"]})
            return stream_id
        stream_id = state.ingest.write(insert)
        if url:
            try:
                add_pull_path(state.services.client, cfg["services"]["mediamtx"], stream_id, url)
            except (CameraError, httpx.HTTPError) as exc:
                # Never started and has no history: undo the row entirely.
                def undo(db):
                    db.execute("DELETE FROM streams WHERE stream_id = ?", (stream_id,))
                    _audit(db, "add_failed", stream_id, {"error": str(exc)[:200]})
                state.ingest.write(undo)
                if isinstance(exc, CameraError):
                    raise
                raise CameraError("MediaMTX is unavailable; camera not added.", 503,
                                  "service_unavailable") from exc
        refresh(state)
    _wake()
    log.info("camera %s added (%s)", stream_id, source["kind"])
    return stream_id


def update(state, stream_id: str, body) -> None:
    body = _body(body, {"display_name", "enabled"})
    cfg = state.cfg
    name = validate_name(body["display_name"]) if "display_name" in body else None
    enabled = body.get("enabled")
    if "enabled" in body and not isinstance(enabled, bool):
        raise CameraError("enabled must be true or false")
    with _lock:
        def change(db):
            row = _row(db, stream_id)
            deployment = row["origin"] != "operator"
            if deployment and enabled and not row["enabled"] and not row["operator_off"]:
                raise CameraError("This camera is off in the deployment (backend.yaml / "
                                  "make CAMERAS) and cannot be turned on here.", 409, "config_camera")
            if name is not None and name != row["display_name"]:
                _unique_name(db, name, stream_id)
                db.execute("UPDATE streams SET display_name = ?, name_locked = 1 WHERE stream_id = ?",
                           (name, stream_id))
                _audit(db, "renamed", stream_id, {"display_name": name})
            if enabled is not None and enabled != bool(row["enabled"]):
                # operator_off keeps a deployment camera off across restarts.
                db.execute("UPDATE streams SET enabled = ?, operator_off = ? WHERE stream_id = ?",
                           (int(enabled), int(deployment and not enabled), stream_id))
                _audit(db, "enabled" if enabled else "disabled", stream_id)
            return row
        row = state.ingest.write(change)
        if enabled is not None and row["pull_url"]:
            mediamtx = cfg["services"]["mediamtx"]
            if enabled:
                try:
                    add_pull_path(state.services.client, mediamtx, stream_id, row["pull_url"])
                except (CameraError, httpx.HTTPError) as exc:
                    log.warning("camera %s pull path deferred to supervisor: %s", stream_id, exc)
            else:
                delete_pull_path(state.services.client, mediamtx, stream_id)
        refresh(state)
    _wake()


def remove(state, stream_id: str, confirm: str | None) -> None:
    if confirm != stream_id:
        raise CameraError("Confirm removal by repeating the camera id.")
    with _lock:
        def retire(db):
            row = _row(db, stream_id)
            if row["origin"] != "operator":
                raise CameraError("This camera is set by the deployment: turn it off here, "
                                  "or remove it from backend.yaml.", 409, "config_camera")
            db.execute("UPDATE streams SET enabled = 0, removed_utc = ? WHERE stream_id = ?",
                       (_utc(), stream_id))
            _audit(db, "removed", stream_id, {"display_name": row["display_name"]})
            return row
        row = state.ingest.write(retire)
        if row["pull_url"]:
            delete_pull_path(state.services.client, state.cfg["services"]["mediamtx"], stream_id)
        refresh(state)
    _wake()
    log.info("camera %s removed; history kept", stream_id)


def _wake() -> None:
    from . import pipelines
    pipelines.wake.set()
