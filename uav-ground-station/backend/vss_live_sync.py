# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Keep VSS live-stream indexing in step with the UAV's running pipelines.

When the dashboard connects to a UAV host, every camera (a RUNNING pipeline
with an RTSP stream) is registered with VSS as a live RTSP stream so its
footage becomes searchable while the drone flies. A background loop
reconciles every ``VSS_LIVE_SYNC_INTERVAL_SECONDS``, independent of any open
browser.

**Multiple UAV hosts at once**: each connected host's cameras are tracked and
reconciled independently (``_hosts: dict[host, state]``). Since camera names
are arbitrary operator-chosen strings, two hosts could both have a
"uav-front-camera" -- streams are registered as ``uav-<host_tag>--<camera>``
(see uav_naming.py) so every host's cameras get their own namespace, and
uav_naming's registry remembers which (host, camera) a stream_name belongs to.

Per host, per reconciliation pass:
  * pipeline running, no VSS stream     -> register it (``POST /streams``)
  * pipeline running, VSS stream paused -> resume it (``PATCH state=running``)
  * pipeline running, stream URL differs (host changed) or stream gave up
    (``error``/``stopped``) -> deregister and register again (a VSS stream's
    URL can't be changed in place)
  * pipeline stopped, or host disconnected -> pause the VSS stream (also one VSS
    is stuck retrying); a host that stays unreachable is paused after
    UNREACHABLE_PAUSE_SECONDS

VSS runs at most VSS_LIVE_STREAM_MAX_CONCURRENT live streams. Feeds are started
host by host, the tab being viewed first, and any beyond the limit wait as
"queued" until a slot frees up, instead of failing every pass.

A hand-registered ``uav-<host_tag>--<camera>`` stream is adopted instead of
duplicated. The older, pre-multi-UAV convention (bare ``uav-<camera>``, no
embedded host) is still recognized and reconciled, it just can't be
attributed to a specific host for search scoping (see
uav_naming.LEGACY_NAME). Other VSS streams are never touched. Deregistering
keeps the already-indexed footage and embeddings (no purge).

Routes:
  * ``GET    /api/search/live-sync`` -- all tracked hosts, their last sync and streams.
  * ``POST   /api/search/live-sync`` -- ``{"host": ...}`` start/retarget sync for one host.
  * ``DELETE /api/search/live-sync`` -- ``{"host": ...}`` stop sync for one host and pause its streams.
"""

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import uav_naming
import vss_search

logger = logging.getLogger("viz-backend.vss-live-sync")

SYNC_INTERVAL_SECONDS = max(5.0, float(os.environ.get("VSS_LIVE_SYNC_INTERVAL_SECONDS", "15")))
# Minimum gap before re-registering a stream that VSS reported as failed, so an
# unreachable RTSP source isn't re-created on every pass.
RECREATE_BACKOFF_SECONDS = 60.0
# VSS dataprep refuses more live streams than MM_DATAPREP_LIVE_STREAM_MAX_CONCURRENT
# (HTTP 503); keep this equal to it so extra feeds wait as "queued" instead of failing.
LIVE_STREAM_MAX = max(1, int(os.environ.get("VSS_LIVE_STREAM_MAX_CONCURRENT", "8")))
# An unreachable host keeps indexing through a network blip, then its streams are
# paused so they stop occupying live-stream slots.
UNREACHABLE_PAUSE_SECONDS = 60.0
DETECT_TIMEOUT_SECONDS = 8.0
# How long a POST waits for the pass it triggered before answering with the current state.
KICK_WAIT_SECONDS = 3.0
# While a feed is still "starting" (or a host has not been synced yet) passes run this
# often, so "running" shows up as soon as VSS reports it instead of up to a full interval later.
FAST_SYNC_SECONDS = 3.0
ACTIVE_STATES = ("running", "starting", "reconnecting")
IDLE_STATES = ("paused", "error", "stopped")
# VSS re-opens a live stream every few segments and reports "starting" for the
# moment that takes; within this long of a "running" report it is shown as running.
RESTART_GRACE_SECONDS = 120.0
# IPv4 address or DNS hostname; the value ends up inside an RTSP URL.
HOST_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"

Detector = Callable[[str], Awaitable[dict]]

router = APIRouter()

_detector: Optional[Detector] = None
_rtsp_port: int = 8555

_lock = asyncio.Lock()
_task: Optional[asyncio.Task] = None
_kick = asyncio.Event()
_pass_count = 0
# Tab the operator is looking at: its feeds get free live-stream slots first.
_priority_host: Optional[str] = None
_capacity = {"limit": LIVE_STREAM_MAX, "active": 0}
_recreated_at: dict = {}
_last_running: dict = {}
# host -> {"last_sync": ..., "last_error": ..., "streams": [...]}. A host stays
# in this dict (and keeps being reconciled) until explicitly removed via
# DELETE, even if the browser tab for it is closed -- indexing should keep
# running in the background, same as before this multi-host change.
_hosts: dict = {}


def configure(detector: Detector, rtsp_port: int) -> None:
    """Called once by main.py with its pipeline detection helpers."""
    global _detector, _rtsp_port
    _detector = detector
    _rtsp_port = rtsp_port


class LiveSyncRequest(BaseModel):
    host: str = Field(..., min_length=1, max_length=253, pattern=HOST_PATTERN)
    # Set by a tab restored in the background (browser bfcache/multi-tab
    # restore): it still needs its host tracked so its feeds get indexed, but
    # must not steal scheduler priority away from whichever tab the user is
    # actually looking at.
    background: bool = False


def _host_status(host: str) -> dict:
    state = _hosts.get(host, {})
    return {
        "host": host,
        "last_sync": state.get("last_sync"),
        "last_error": state.get("last_error"),
        "streams": state.get("streams", []),
    }


def _status() -> dict:
    return {
        "enabled": vss_search.VSS_ENABLED,
        "interval_seconds": SYNC_INTERVAL_SECONDS,
        "capacity": dict(_capacity),
        "hosts": {host: _host_status(host) for host in _hosts},
    }


def _summary(stream: dict, host: Optional[str]) -> dict:
    name = stream.get("stream_name")
    info = uav_naming.info_for(str(name or ""))
    # Same reasoning as vss_search.py's _camera_and_host(): a legacy
    # stream_name IS the bare camera name verbatim, not something to strip a
    # "uav-" prefix off of.
    camera = (info[1] if info else uav_naming.camera_of(str(name or ""))
              or (str(name or "") if uav_naming.LEGACY_NAME.fullmatch(str(name or "")) else None))
    stats = stream.get("stats") if isinstance(stream.get("stats"), dict) else {}
    state = stream.get("state")
    now = time.monotonic()
    if state == "running":
        _last_running[name] = now
    elif state == "starting":
        if name not in _last_running and stats.get("embeddings_created"):
            # Already produced embeddings before this process started tracking it: a restart, not a first start.
            _last_running[name] = now
        if now - _last_running.get(name, -RESTART_GRACE_SECONDS) < RESTART_GRACE_SECONDS:
            state = "running"
    return {
        "camera": camera,
        "stream_name": name,
        "stream_id": stream.get("stream_id"),
        "state": state,
        "last_error": stream.get("last_error"),
        "embeddings_created": stats.get("embeddings_created"),
    }


async def _call(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> Any:
    resp = await client.request(method, url, **kwargs)
    if resp.status_code >= 400:
        raise RuntimeError(f"{method} {url} -> {resp.status_code}: {vss_search._upstream_message(resp)}")
    try:
        return resp.json()
    except ValueError:
        return None


def _is_managed_for_host(stream_name_: str, host: str) -> bool:
    """True if `stream_name_` is a VSS stream this host's reconciliation pass
    owns -- either host-qualified for this exact host, or a legacy
    (pre-multi-UAV) name, which is adopted by whichever single host happens
    to be tracked for it when no other host has already claimed it."""
    if uav_naming.owns(stream_name_, host):
        return True
    info = uav_naming.info_for(stream_name_)
    if info is not None:
        return info[0] == host
    return bool(uav_naming.LEGACY_NAME.fullmatch(stream_name_))


async def _detect(host: str) -> dict:
    if _detector is None:
        raise RuntimeError("live sync is not configured")
    try:
        return await asyncio.wait_for(_detector(host), DETECT_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, httpx.HTTPError, ValueError):
        return {"reachable": False, "cameras": []}


def _plan_host(host: str, detected: dict, streams: list, now: float) -> dict:
    """Decide what one host's feeds need; nothing is sent to VSS here."""
    state = _hosts[host]
    owned: dict = {}
    for stream in streams:
        name = stream.get("stream_name")
        if name and uav_naming.is_managed_name(name) and _is_managed_for_host(name, host):
            owned.setdefault(name, stream)
            # Legacy (no embedded host) streams are unclaimed-for-anyone until
            # the first host to see them claims it here, so a different host's
            # later pass doesn't also treat the same orphaned stream as its own.
            if uav_naming.LEGACY_NAME.fullmatch(name) and uav_naming.info_for(name) is None:
                uav_naming.register(name, host, name)
    plan = {"host": host, "owned": owned, "wanted": {}, "delete": [], "pause": [], "start": [], "error": None}

    if not detected.get("reachable"):
        # A network blip must not pause indexing, but a host that stays away
        # would hold live-stream slots (VSS caps them) for nothing.
        since = state.get("unreachable_since")
        if since is None:
            since = state["unreachable_since"] = now
        plan["error"] = f"UAV host {host} is not reachable"
        if now - since >= UNREACHABLE_PAUSE_SECONDS:
            plan["pause"] = [s for s in owned.values() if s.get("state") in ACTIVE_STATES]
        return plan
    state["unreachable_since"] = None

    cameras = sorted({c["camera"] for c in detected.get("cameras", []) if isinstance(c, dict) and c.get("camera")})
    # Update the registry first: vss_search.py's per-result attribution reads it.
    wanted = {uav_naming.stream_name(host, camera): camera for camera in cameras}
    uav_naming.replace_host_registry(host, wanted)
    plan["wanted"] = wanted

    for name, camera in wanted.items():
        url = f"rtsp://{host}:{_rtsp_port}/{camera}"
        stream = owned.get(name)
        if stream:
            failed = stream.get("state") in ("error", "stopped")
            moved = str(stream.get("stream_url", "")).rstrip("/") != url
            if moved or failed:
                if failed and not moved and now - _recreated_at.get(name, -RECREATE_BACKOFF_SECONDS) < RECREATE_BACKOFF_SECONDS:
                    continue
                plan["delete"].append(stream)
                plan["start"].append(("register", name, camera, None))
            elif stream.get("state") == "paused":
                plan["start"].append(("resume", name, camera, stream))
        else:
            plan["start"].append(("register", name, camera, None))
    # Feeds of cameras that stopped: pause them, including ones VSS is stuck
    # retrying ("reconnecting"), which would otherwise hold a slot forever.
    plan["pause"] = [s for n, s in owned.items() if n not in wanted and s.get("state") in ACTIVE_STATES]
    return plan


async def _start_stream(client: httpx.AsyncClient, host: str, kind: str, name: str, camera: str, stream: Optional[dict]) -> None:
    if kind == "resume":
        await _call(client, "PATCH", f"/streams/{stream['stream_id']}", json={"state": "running"})
        stream["state"] = "running"
        logger.info("VSS live stream %s resumed", name)
        return
    url = f"rtsp://{host}:{_rtsp_port}/{camera}"
    await _call(client, "POST", "/streams", json={
        "stream_url": url,
        "stream_name": name,
        "sensor_id": name,
        "description": f"UAV {host} camera {camera} feed (registered by uav-ground-station)",
        # 4th tag (the host-qualified stream name, same value as sensor_id
        # above) lets vss_search.py query one exact (host, camera) pair --
        # the bare `camera` tag alone is shared across UAVs that reuse the
        # same camera name (e.g. every drone has a "nadir"/"forward"/"rear"),
        # so an OR-tag query on it alone can't distinguish whose footage is
        # whose. _attribute_result() only reads tags[1]/tags[2], so this is
        # additive and doesn't change host/camera attribution.
        "tags": ["uav", host, camera, name],
    })
    _recreated_at[name] = time.monotonic()
    logger.info("VSS live stream %s registered for %s", name, url)


async def _reconcile(client: httpx.AsyncClient, hosts: list, detections: list) -> None:
    """One reconciliation pass over every tracked host; see the module docstring.
    Streams are paused first, then feeds are started host by host (the tab being
    viewed first) while VSS has free live-stream slots; the rest wait as "queued"."""
    listing = await _call(client, "GET", "/streams")
    streams = [s for s in (listing or {}).get("streams", []) if isinstance(s, dict)]
    now = time.monotonic()
    plans = [_plan_host(h, d, streams, now) for h, d in zip(hosts, detections) if h in _hosts]
    free = LIVE_STREAM_MAX - sum(1 for s in streams if s.get("state") in ACTIVE_STATES)
    errors: dict = {p["host"]: [] for p in plans}
    queued: dict = {p["host"]: {} for p in plans}

    for plan in plans:
        host = plan["host"]
        for stream in plan["delete"]:
            try:
                await _call(client, "DELETE", f"/streams/{stream['stream_id']}")
                logger.info("VSS live stream %s deregistered (%s)", stream.get("stream_name"), stream.get("state"))
                free += stream.get("state") in ACTIVE_STATES
            except (RuntimeError, httpx.HTTPError) as exc:
                errors[host].append(str(exc))
        for stream in plan["pause"]:
            try:
                await _call(client, "PATCH", f"/streams/{stream['stream_id']}", json={"state": "paused"})
                logger.info("VSS live stream %s paused (pipeline not running)", stream.get("stream_name"))
                stream["state"] = "paused"
                free += 1
            except (RuntimeError, httpx.HTTPError) as exc:
                errors[host].append(str(exc))

    # Feeds left running by a host nobody tracks any more (for example after a
    # restart of this backend) would hold slots with nothing managing them.
    for stream in streams:
        name = str(stream.get("stream_name") or "")
        tags = stream.get("tags") if isinstance(stream.get("tags"), list) else []
        if (stream.get("state") in ACTIVE_STATES and len(tags) >= 3 and tags[0] == "uav"
                and tags[1] not in _hosts and uav_naming.owns(name, str(tags[1]))):
            try:
                await _call(client, "PATCH", f"/streams/{stream['stream_id']}", json={"state": "paused"})
                logger.info("VSS live stream %s paused (host %s is no longer tracked)", name, tags[1])
                free += 1
            except (RuntimeError, httpx.HTTPError) as exc:
                logger.warning("VSS live sync: could not pause orphaned stream %s: %s", name, exc)

    # Another VSS stream pointing at the same RTSP URL as a camera's canonical
    # host-qualified name is a duplicate -- e.g. a legacy/manually-created
    # stream from before this naming scheme. Clean these up *before*
    # allocating slots below: if left until after, a duplicate holding the
    # last free slot would keep the canonical name permanently "queued" (it
    # never starts, so it's exempt from after-the-fact cleanup, and the
    # duplicate is never recognised as "owned" so it's never paused either --
    # a deadlock). Deleting it here frees its slot in the same pass, so the
    # canonical registration can take over immediately below with at most one
    # reconcile pass's gap, not forever.
    for plan in plans:
        host = plan["host"]
        wanted_urls = {
            f"rtsp://{host}:{_rtsp_port}/{camera}".rstrip("/"): name
            for name, camera in plan["wanted"].items()
        }
        for stream in list(streams):
            canonical = wanted_urls.get(str(stream.get("stream_url", "")).rstrip("/"))
            dup_name = stream.get("stream_name")
            if canonical is None or dup_name == canonical:
                continue
            try:
                await _call(client, "DELETE", f"/streams/{stream['stream_id']}")
                logger.info("VSS duplicate live stream %s deregistered (duplicate source URL of %s)", dup_name, canonical)
                uav_naming.unregister(str(dup_name or ""))
                if stream.get("state") in ACTIVE_STATES:
                    free += 1
                streams.remove(stream)
            except (RuntimeError, httpx.HTTPError) as exc:
                errors[host].append(str(exc))

    for plan in plans:
        host = plan["host"]
        for kind, name, camera, stream in plan["start"]:
            if free <= 0:
                queued[host][name] = camera
                continue
            try:
                await _start_stream(client, host, kind, name, camera, stream)
                free -= 1
            except (RuntimeError, httpx.HTTPError) as exc:
                if "Maximum of" in str(exc):  # VSS's limit is lower than LIVE_STREAM_MAX
                    free = 0
                    queued[host][name] = camera
                else:
                    errors[host].append(str(exc))

    listing = await _call(client, "GET", "/streams")
    fresh = [s for s in (listing or {}).get("streams", []) if isinstance(s, dict)]
    _capacity["active"] = sum(1 for s in fresh if s.get("state") in ACTIVE_STATES)
    stamp = datetime.now(timezone.utc).isoformat()
    for plan in plans:
        host = plan["host"]
        summaries, kept_cameras = {}, []
        for s in fresh:
            name = str(s.get("stream_name") or "")
            if uav_naming.is_managed_name(name) and _is_managed_for_host(name, host):
                entry = _summary(s, host)
                if entry["camera"]:
                    kept_cameras.append(entry["camera"])
                # Feeds of cameras this UAV no longer runs (an earlier deployment) stay searchable but are not listed.
                if name in plan["wanted"] or s.get("state") in ACTIVE_STATES:
                    summaries[name] = entry
        for name, camera in queued[host].items():
            entry = summaries.setdefault(name, {
                "camera": camera, "stream_name": name, "stream_id": None, "last_error": None, "embeddings_created": None,
            })
            entry["state"] = "queued"
            kept_cameras.append(camera)
        uav_naming.note_known(host, kept_cameras)
        state = _hosts[host]
        state["streams"] = sorted(summaries.values(), key=lambda e: e["stream_name"])
        state["last_sync"] = stamp
        state["last_error"] = "; ".join([e for e in [plan["error"]] if e] + errors[host])[:500] or None


async def _run_once() -> None:
    """Reconcile every tracked host. Host lookups run concurrently (each with a
    timeout) so an unreachable host can't delay the others or the VSS calls."""
    global _pass_count
    hosts = sorted(_hosts, key=lambda h: h != _priority_host)
    if not hosts:
        return
    try:
        detections = await asyncio.gather(*(_detect(h) for h in hosts))
        async with _lock, vss_search._client(timeout=20.0) as client:
            await _reconcile(client, hosts, detections)
    except (RuntimeError, httpx.HTTPError, ValueError) as exc:
        for h in hosts:
            if h in _hosts:
                _hosts[h]["last_error"] = str(exc)[:500]
        logger.warning("VSS live sync failed: %s", exc)
    finally:
        _pass_count += 1


def _settling() -> bool:
    return any(
        state["last_sync"] is None or any(s.get("state") == "starting" for s in state["streams"])
        for state in _hosts.values()
    )


async def _loop() -> None:
    while True:
        try:
            await asyncio.wait_for(_kick.wait(), FAST_SYNC_SECONDS if _settling() else SYNC_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
        _kick.clear()
        try:
            await _run_once()
        except Exception:  # noqa: BLE001 -- the loop must survive any single bad pass
            logger.exception("VSS live sync pass crashed")
        await asyncio.sleep(1.0)  # bounds how often rapid tab switches can trigger passes


def _ensure_loop() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


def _new_host_state() -> dict:
    return {"last_sync": None, "last_error": None, "streams": [], "unreachable_since": None}


def _require_enabled() -> None:
    if not vss_search.VSS_ENABLED:
        raise HTTPException(status_code=404, detail="VSS search is not enabled on this ground station.")


@router.get("/api/search/live-sync")
async def live_sync_status():
    _require_enabled()
    return _status()


@router.post("/api/search/live-sync")
async def live_sync_start(req: LiveSyncRequest):
    """Track `req.host` and make its tab the one that gets free live-stream slots
    first. Never blocks on VSS: it triggers a pass and answers within
    KICK_WAIT_SECONDS with whatever state is current."""
    global _priority_host
    _require_enabled()
    _hosts.setdefault(req.host, _new_host_state())
    if not req.background:
        _priority_host = req.host
    _ensure_loop()
    before = _pass_count
    _kick.set()
    deadline = time.monotonic() + KICK_WAIT_SECONDS
    while _pass_count == before and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    return _status()


@router.delete("/api/search/live-sync")
async def live_sync_stop(req: Optional[LiveSyncRequest] = None):
    global _priority_host
    _require_enabled()
    if req and req.host:
        host = req.host
        if host in _hosts:
            # Pause this host's streams before dropping it from tracking, so
            # footage already indexed stays put but new indexing stops.
            async with _lock, vss_search._client(timeout=20.0) as client:
                try:
                    listing = await _call(client, "GET", "/streams")
                    for stream in (listing or {}).get("streams", []):
                        name = stream.get("stream_name") if isinstance(stream, dict) else None
                        if name and uav_naming.is_managed_name(name) and _is_managed_for_host(name, host) \
                                and stream.get("state") not in IDLE_STATES:
                            await _call(client, "PATCH", f"/streams/{stream['stream_id']}", json={"state": "paused"})
                except (RuntimeError, httpx.HTTPError) as exc:
                    logger.warning("VSS live sync: failed to pause streams for %s on disconnect: %s", host, exc)
            _hosts.pop(host, None)
            uav_naming.replace_host_registry(host, {})
            uav_naming.forget_host(host)
            if _priority_host == host:
                _priority_host = None
            _kick.set()  # let queued feeds of other hosts take the freed slots
    else:
        # Back-compat: no host specified -- stop tracking every host (the
        # pre-multi-UAV behaviour).
        for host in list(_hosts.keys()):
            await live_sync_stop(LiveSyncRequest(host=host))
    return _status()
