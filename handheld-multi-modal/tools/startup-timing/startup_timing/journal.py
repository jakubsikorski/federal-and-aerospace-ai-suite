# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Recorder: an append-only journal of begin/end events shared by every process of a run."""
import fcntl
import json
import os
from pathlib import Path
import sys
import time
import uuid
from datetime import datetime, timezone


_warned = False


def utc():
    return datetime.now(timezone.utc).isoformat()


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _write(event):
    directory = os.environ.get("STARTUP_TIMING_DIR")
    if not directory:
        return
    # Only labels and measurements are kept: never commands, environment or output.
    try:
        with (Path(directory) / "events.jsonl").open("a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.write(json.dumps(event) + "\n")
    except OSError as error:  # timing must never change what the caller does
        global _warned
        if not _warned:
            _warned = True
            print(f"startup timing: cannot record ({error}); timings are incomplete", file=sys.stderr)


def begin(group, label, service=None, parent=None):
    """Start a step and return its token. A no-op token outside a timed run is still returned."""
    token = uuid.uuid4().hex
    parent = parent or os.environ.get("STARTUP_TIMING_PARENT") or None
    event = {"event": "begin", "id": token, "group": group, "label": label,
             "monotonic": time.monotonic(), "utc": utc()}
    if service:
        event["service"] = service
    if parent:
        event["parent_id"] = parent
    _write(event)
    return token


def end(token, code=0, status=None, size=None):
    event = {"event": "end", "id": token, "monotonic": time.monotonic(), "utc": utc(),
             "status": status or ("ok" if code == 0 else "failed"), "returncode": code}
    if size is not None:
        event["bytes"] = size
    _write(event)


def cached(group, label, service=None):
    end(begin(group, label, service), status="cached")


def read_steps(path, failed=False):
    """Pair begin/end events. A step with no end has no duration ('unavailable', never zero)."""
    active, steps = {}, []
    for line in Path(path).read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn final line from a killed process
        if event["event"] == "begin":
            active[event["id"]] = event
        elif event["id"] in active:
            steps.append(_step(active.pop(event["id"]), event))
    for opening in active.values():
        steps.append(_step(opening, None, failed))
    return steps


def _step(opening, closing, failed=False):
    step = {"id": opening["id"], "parent_id": opening.get("parent_id"),
            "group": opening["group"], "label": opening["label"],
            "service": opening.get("service"), "started_at": opening["utc"],
            "monotonic_started_at": opening["monotonic"], "method": "monotonic-clock"}
    if closing:
        step.update(ended_at=closing["utc"], monotonic_ended_at=closing["monotonic"],
                    seconds=closing["monotonic"] - opening["monotonic"],
                    status=closing["status"], returncode=closing["returncode"])
        if "bytes" in closing:
            step["bytes"] = closing["bytes"]
    else:
        step.update(ended_at=None, monotonic_ended_at=None, seconds=None,
                    status="failed" if failed else "unavailable", method="unfinished-step")
    return step
