# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Shared host/stream naming + registry for multi-UAV support.

Each connected UAV host's cameras are registered as their own VSS live stream
(see vss_live_sync.py) under a host-qualified name, since two drones can reuse
the same camera name (e.g. both "uav-front-camera"). Camera names are
arbitrary operator-chosen strings, so a combined ``uav-<host_tag>-<camera>``
string can't be split back into its parts by regex alone -- this module keeps
a small in-memory registry instead: written by vss_live_sync.py as it
registers/deregisters streams, read by vss_search.py to resolve a stream_name
back to (host, camera). Lives here (not in either of those modules) to avoid
a circular import.
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional

_SANITIZE = re.compile(r"[^a-z0-9]+")
_SLUG_MAX = 24
_DIGEST_LEN = 8

# Loose "looks like a stream this feature manages" check -- not meant to be
# parsed back into (host_tag, camera); use the registry functions below for
# that. Generous length bound: host_tag + "--" + a 48-char camera name.
MANAGED_NAME = re.compile(r"^uav-[a-z0-9-]{1,40}--[A-Za-z0-9_-]{1,48}$")

# Pre-multi-UAV stream name convention (bare camera name, no embedded host).
# Kept recognizable so the live-sync loop still manages/cleans up streams
# registered under this older convention, but -- having no embedded host --
# it can never match a host-scoped search filter; see vss_search.py.
LEGACY_NAME = re.compile(r"^uav-(?!.*--)[A-Za-z0-9_-]{1,44}$")


def host_tag(host: str) -> str:
    """Bounded, collision-resistant tag for a host: a readable slug plus a
    short digest of the exact host, so "10.2.3.4" and "10-2-3-4" differ."""
    raw = (host or "").strip().lower()
    slug = _SANITIZE.sub("-", raw).strip("-")[:_SLUG_MAX].strip("-") or "host"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_DIGEST_LEN]
    return f"{slug}-{digest}"


def stream_name(host: str, camera: str) -> str:
    return f"uav-{host_tag(host)}--{camera}"


def is_managed_name(name: str) -> bool:
    return bool(MANAGED_NAME.fullmatch(name or "") or LEGACY_NAME.fullmatch(name or ""))


# --- (host, camera) registry, authoritative source for "whose stream is this" ---
# stream_name -> (host, camera). Written by vss_live_sync.py on every
# reconciliation pass; read by vss_search.py when a request asks to be
# scoped to one host's footage, or to attach camera/host metadata to a
# result so the UI can show "which drone/camera did this clip come from".
_registry: dict = {}


def register(name: str, host: str, camera: str) -> None:
    """Claim `name` for `host`. For a host-tagged name this is called every
    reconciliation pass; for a legacy (no embedded host) name, it's a
    one-time first-come claim so a different host's later reconciliation
    doesn't also treat the same orphaned stream as its own.
    """
    _registry[name] = (host, camera)


def unregister(name: str) -> None:
    _registry.pop(name, None)


def replace_host_registry(host: str, entries: dict) -> None:
    """Replace every host-tagged registry entry for `host` with `entries`
    (stream_name -> camera) -- used at the end of each reconciliation pass so
    a camera no longer configured drops out of the registry too. Leaves any
    legacy (no embedded host) entry claimed for this host alone, since
    `entries` never contains those names.
    """
    for name, (h, _camera) in list(_registry.items()):
        if h == host and MANAGED_NAME.fullmatch(name):
            del _registry[name]
    for name, camera in entries.items():
        _registry[name] = (host, camera)


def info_for(name: str) -> Optional[tuple]:
    """(host, camera) for a managed stream_name, or None if unknown (not
    registered by this feature, or registered before a restart of this
    container -- registry is in-memory only)."""
    return _registry.get(name)


def stream_names_for_host(host: str) -> set:
    return {name for name, (h, _camera) in _registry.items() if h == host}


def hosts_and_cameras() -> dict:
    """{host: sorted [camera, ...]} for every currently-managed stream in the
    registry -- i.e. every UAV host + camera combination that's currently
    live-indexed. Used to populate the search UI's "UAV" / "Camera" filter
    dropdowns (see vss_search.py's GET /api/search/sources)."""
    out: dict = {}
    for _name, (host, camera) in _registry.items():
        out.setdefault(host, set()).add(camera)
    return {host: sorted(cams) for host, cams in out.items()}
