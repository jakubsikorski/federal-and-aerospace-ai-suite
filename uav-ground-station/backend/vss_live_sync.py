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
  * pipeline stopped, or host disconnected -> pause the VSS stream

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
# IPv4 address or DNS hostname; the value ends up inside an RTSP URL.
HOST_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"

Detector = Callable[[str], Awaitable[dict]]

router = APIRouter()

_detector: Optional[Detector] = None
_rtsp_port: int = 8555

_lock = asyncio.Lock()
_task: Optional[asyncio.Task] = None
_recreated_at: dict = {}
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


def _host_status(host: str) -> dict:
    state = _hosts.get(host, {})
    return {
        "host": host,
        "last_sync": state.get("last_sync"),
        "last_error": state.get("last_error"),
        "streams": state.get("streams", []),
    }


def _status() -> dict:
    hosts = {host: _host_status(host) for host in _hosts}
    first = next(iter(hosts.values()), None)
    return {
        "enabled": vss_search.VSS_ENABLED,
        "interval_seconds": SYNC_INTERVAL_SECONDS,
        "hosts": hosts,
        # Back-compat top-level mirror of a single host, for any caller not
        # yet updated to the "hosts" map (e.g. older cached frontend JS
        # during a rolling deploy) -- reflects whichever host was tracked
        # first/most recently, same shape as the pre-multi-UAV response.
        "host": first["host"] if first else None,
        "last_sync": first["last_sync"] if first else None,
        "last_error": first["last_error"] if first else None,
        "streams": first["streams"] if first else [],
    }


def _summary(stream: dict, host: Optional[str]) -> dict:
    name = stream.get("stream_name")
    info = uav_naming.info_for(str(name or ""))
    # Same reasoning as vss_search.py's _camera_and_host(): a legacy
    # stream_name IS the bare camera name verbatim, not something to strip a
    # "uav-" prefix off of.
    camera = info[1] if info else (str(name or "") if uav_naming.LEGACY_NAME.fullmatch(str(name or "")) else None)
    stats = stream.get("stats") if isinstance(stream.get("stats"), dict) else {}
    return {
        "camera": camera,
        "stream_name": name,
        "stream_id": stream.get("stream_id"),
        "state": stream.get("state"),
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
    info = uav_naming.info_for(stream_name_)
    if info is not None:
        return info[0] == host
    return bool(uav_naming.LEGACY_NAME.fullmatch(stream_name_))


async def _reconcile_host(client: httpx.AsyncClient, host: str) -> None:
    """One reconciliation pass for a single host; see the module docstring."""
    state = _hosts.setdefault(host, {"last_sync": None, "last_error": None, "streams": []})

    if _detector is None:
        raise RuntimeError("live sync is not configured")
    detected = await _detector(host)
    if not detected.get("reachable"):
        # Same policy as the dashboard: a transient network blip must not
        # pause indexing, so leave VSS untouched until the host answers.
        state["last_error"] = f"UAV host {host} is not reachable"
        return
    active_cameras = sorted({c["camera"] for c in detected.get("cameras", []) if isinstance(c, dict) and c.get("camera")})

    # Update the registry before touching the VSS API below: vss_search.py's
    # /api/search/sources and per-result attribution both read straight from
    # it, so a mid-pass failure below must not leave it unpopulated.
    wanted = {uav_naming.stream_name(host, camera): camera for camera in active_cameras}
    uav_naming.replace_host_registry(host, wanted)

    listing = await _call(client, "GET", "/streams")
    managed: dict = {}
    for stream in (listing or {}).get("streams", []):
        name = stream.get("stream_name") if isinstance(stream, dict) else None
        if name and uav_naming.is_managed_name(name) and _is_managed_for_host(name, host):
            managed.setdefault(name, stream)
            # Legacy (no embedded host) streams are unclaimed-for-anyone until
            # the first host to see them claims it here, so a different host's
            # later pass doesn't also treat the same orphaned stream as its own.
            if uav_naming.LEGACY_NAME.fullmatch(name) and uav_naming.info_for(name) is None:
                uav_naming.register(name, host, name)

    errors = []
    ready = set()  # names in `wanted` whose canonical VSS stream exists and is running this pass
    for name, camera in wanted.items():
        url = f"rtsp://{host}:{_rtsp_port}/{camera}"
        stream = managed.get(name)
        try:
            if stream:
                failed = stream.get("state") in ("error", "stopped")
                moved = str(stream.get("stream_url", "")).rstrip("/") != url
                if moved or failed:
                    if failed and time.monotonic() - _recreated_at.get(name, 0.0) < RECREATE_BACKOFF_SECONDS:
                        continue
                    await _call(client, "DELETE", f"/streams/{stream['stream_id']}")
                    logger.info("VSS live stream %s deregistered (%s)", name, "host changed" if moved else stream.get("state"))
                    stream = None
                elif stream.get("state") == "paused":
                    await _call(client, "PATCH", f"/streams/{stream['stream_id']}", json={"state": "running"})
                    logger.info("VSS live stream %s resumed", name)
                    ready.add(name)
                    continue
            if not stream:
                await _call(client, "POST", "/streams", json={
                    "stream_url": url,
                    "stream_name": name,
                    "sensor_id": name,
                    "description": f"UAV {host} camera {camera} feed (registered by uav-ground-station)",
                    "tags": ["uav", host, camera],
                })
                _recreated_at[name] = time.monotonic()
                logger.info("VSS live stream %s registered for %s", name, url)
            ready.add(name)
        except (RuntimeError, httpx.HTTPError) as exc:
            errors.append(str(exc))

    for name, stream in managed.items():
        if name in wanted or stream.get("state") in ("paused", "error", "stopped"):
            continue
        try:
            await _call(client, "PATCH", f"/streams/{stream['stream_id']}", json={"state": "paused"})
            logger.info("VSS live stream %s paused (pipeline not running)", name)
        except (RuntimeError, httpx.HTTPError) as exc:
            errors.append(str(exc))

    # Clean up any other VSS stream pointing at the same RTSP URL as a camera
    # we just confirmed is registered under its canonical host-qualified name.
    wanted_urls = {
        f"rtsp://{host}:{_rtsp_port}/{camera}".rstrip("/"): name
        for name, camera in wanted.items()
        if name in ready
    }
    for stream in (listing or {}).get("streams", []):
        if not isinstance(stream, dict):
            continue
        canonical_name = wanted_urls.get(str(stream.get("stream_url", "")).rstrip("/"))
        dup_name = stream.get("stream_name")
        if canonical_name is None or dup_name == canonical_name:
            continue
        try:
            await _call(client, "DELETE", f"/streams/{stream['stream_id']}")
            logger.info(
                "VSS duplicate live stream %s deregistered (duplicate source URL of %s)", dup_name, canonical_name,
            )
            uav_naming.unregister(str(dup_name or ""))
        except (RuntimeError, httpx.HTTPError) as exc:
            errors.append(str(exc))

    listing = await _call(client, "GET", "/streams")
    state["streams"] = sorted(
        (_summary(s, host) for s in (listing or {}).get("streams", [])
         if isinstance(s, dict) and uav_naming.is_managed_name(str(s.get("stream_name") or ""))
         and _is_managed_for_host(str(s.get("stream_name") or ""), host)),
        key=lambda s: s["stream_name"],
    )
    state["last_sync"] = datetime.now(timezone.utc).isoformat()
    state["last_error"] = "; ".join(errors)[:500] or None


async def _run_once(host: Optional[str] = None) -> None:
    """Reconcile one host, or every currently-tracked host if `host` is None
    (used by the background loop)."""
    hosts = [host] if host else list(_hosts.keys())
    async with _lock, vss_search._client(timeout=20.0) as client:
        for h in hosts:
            try:
                await _reconcile_host(client, h)
            except (RuntimeError, httpx.HTTPError, ValueError) as exc:
                _hosts.setdefault(h, {"last_sync": None, "last_error": None, "streams": []})
                _hosts[h]["last_error"] = str(exc)[:500]
                logger.warning("VSS live sync failed for %s: %s", h, exc)


async def _loop() -> None:
    while True:
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)
        if _hosts:
            await _run_once()


def _ensure_loop() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


def _require_enabled() -> None:
    if not vss_search.VSS_ENABLED:
        raise HTTPException(status_code=404, detail="VSS search is not enabled on this ground station.")


@router.get("/api/search/live-sync")
async def live_sync_status():
    _require_enabled()
    return _status()


@router.post("/api/search/live-sync")
async def live_sync_start(req: LiveSyncRequest):
    _require_enabled()
    _hosts.setdefault(req.host, {"last_sync": None, "last_error": None, "streams": []})
    _ensure_loop()
    await _run_once(req.host)
    return _status()


@router.delete("/api/search/live-sync")
async def live_sync_stop(req: Optional[LiveSyncRequest] = None):
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
                                and stream.get("state") not in ("paused", "error", "stopped"):
                            await _call(client, "PATCH", f"/streams/{stream['stream_id']}", json={"state": "paused"})
                except (RuntimeError, httpx.HTTPError) as exc:
                    logger.warning("VSS live sync: failed to pause streams for %s on disconnect: %s", host, exc)
            _hosts.pop(host, None)
            uav_naming.replace_host_registry(host, {})
    else:
        # Back-compat: no host specified -- stop tracking every host (the
        # pre-multi-UAV behaviour).
        for host in list(_hosts.keys()):
            await live_sync_stop(LiveSyncRequest(host=host))
    return _status()
