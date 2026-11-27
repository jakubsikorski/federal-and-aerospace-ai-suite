# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Build and normalise report.json (schema 2). Old reports without a schema still load."""
import json
from pathlib import Path

from .journal import parse_time

SCHEMA = 2
SKIPPED = {"cached", "reused"}


def union_seconds(windows):
    total, until = 0.0, float("-inf")
    for left, right in sorted(windows):
        total += max(0.0, right - max(left, until))
        until = max(until, right)
    return total


def _offsets(step, origin, clock):
    """(start, end) seconds after the run began; (None, None) if unknowable."""
    if clock is not None and step.get("monotonic_started_at") is not None:
        left = step["monotonic_started_at"] - clock
        done = step.get("monotonic_ended_at")
        return left, None if done is None else done - clock
    if not step.get("started_at"):
        return None, None
    left = parse_time(step["started_at"]) - origin
    return left, parse_time(step["ended_at"]) - origin if step.get("ended_at") else None


def normalize(report):
    """Fill ids, offsets and defaults so the renderer needs no special cases."""
    if "steps" not in report or "started_at" not in report or "overall_seconds" not in report:
        raise ValueError("not a startup timing report.json (missing steps, started_at or overall_seconds)")
    origin, clock = parse_time(report["started_at"]), report.get("monotonic_started_at")
    steps = []
    for index, raw in enumerate(report["steps"], 1):
        background = str(raw.get("method", "")).startswith(
            ("stock-log", "docker-state", "missing-completion"))  # also old-format reports
        step = {"id": f"step-{index:04d}", "parent_id": None, "service": None,
                "background": background, **raw}
        if step.get("offset") is None:
            left, right = _offsets(step, origin, clock)
            step["offset"] = None if left is None else max(0.0, left)
            if step.get("seconds") is None and right is not None:
                step["seconds"] = max(0.0, right - left)
        steps.append(step)
    _nest_background(steps)
    ids = {step["id"] for step in steps}
    for step in steps:
        if step["parent_id"] not in ids or step["parent_id"] == step["id"]:
            step["parent_id"] = None
    return {**report, "name": report.get("name") or report.get("target") or "startup",
            "status": report.get("status") or ("ok" if not report.get("returncode") else "failed"),
            "steps": steps, "warnings": report.get("warnings", [])}


def _nest_background(steps):
    """Service-log steps nest under the tightest step of the same service that holds them: a recorded
    step, or a strictly longer service-log step (a phase inside 'start to application ready')."""
    timed = [s for s in steps if s["seconds"] is not None and s["offset"] is not None and s["service"]]
    for step in steps:
        if not step["background"] or step["parent_id"] or step["seconds"] is None or step["offset"] is None:
            continue
        holders = [p for p in timed if p is not step and p["service"] == step["service"]
                   and (p["seconds"] > step["seconds"] if p["background"] else True)
                   and p["offset"] - 0.05 <= step["offset"]
                   and step["offset"] + step["seconds"] <= p["offset"] + p["seconds"] + 0.05]
        if holders:  # strictly longer holders cannot form a cycle; recorded steps are never nested in logs
            step["parent_id"] = min(holders, key=lambda p: p["seconds"])["id"]


def build(name, started_at, ended_at, elapsed, code, steps, warnings, clock_start=None):
    report = {"schema": SCHEMA, "name": name, "started_at": started_at, "ended_at": ended_at,
              "overall_seconds": elapsed, "returncode": code,
              "status": "ok" if code == 0 else "failed", "steps": steps, "warnings": warnings}
    if clock_start is not None:
        report["monotonic_started_at"] = clock_start
    report = normalize(report)
    for step in report["steps"]:  # offsets are stored; raw clocks are not needed again
        step.pop("monotonic_started_at", None)
        step.pop("monotonic_ended_at", None)
    report["groups"] = group_summary(report["steps"])
    report["idle_seconds"], report["overlap_seconds"] = idle_and_overlap(report)
    return report


def measured(steps):
    return [s for s in steps if s["seconds"] is not None and s["status"] not in SKIPPED]


def group_summary(steps):
    groups = {}
    for step in steps:
        item = groups.setdefault(step["group"], {"steps": 0, "summed_seconds": None,
                                                 "active_wall_seconds": None, "cached": 0,
                                                 "unavailable": 0, "failed": 0})
        item["steps"] += 1
        item["cached"] += step["status"] in SKIPPED
        item["failed"] += step["status"] == "failed"
        item["unavailable"] += step["seconds"] is None and step["status"] not in SKIPPED
    for group, item in groups.items():
        done = [s for s in measured(steps) if s["group"] == group and s["offset"] is not None]
        if done:
            item["summed_seconds"] = sum(s["seconds"] for s in done)
            item["active_wall_seconds"] = union_seconds(
                [(s["offset"], s["offset"] + s["seconds"]) for s in done])
    return groups


def idle_and_overlap(report):
    """Idle = run time covered by no recorded (non-background) step; overlap = parallel double count."""
    total = report["overall_seconds"]
    roots = [s for s in measured(report["steps"]) if s["offset"] is not None
             and not s["parent_id"] and not s["background"]]
    windows = [(min(s["offset"], total), min(s["offset"] + s["seconds"], total)) for s in roots]
    # A root step that never ended was still running: it covers the rest of the run.
    windows += [(min(s["offset"], total), total) for s in report["steps"]
                if s["seconds"] is None and s["offset"] is not None and not s["parent_id"]
                and not s["background"] and s.get("method") == "unfinished-step"]
    covered = union_seconds(windows)
    summed = sum(right - left for left, right in windows)
    return max(0.0, total - covered), max(0.0, summed - covered)


def timeline(steps):
    """(depth, step) rows: roots by start time, each followed by its children."""
    children = {}
    for step in steps:
        children.setdefault(step["parent_id"], []).append(step)
    key = lambda s: (s["offset"] if s["offset"] is not None else float("inf"), s["label"])
    rows = []

    def walk(parent, depth):
        for step in sorted(children.get(parent, []), key=key):
            rows.append((depth, step))
            walk(step["id"], depth + 1)

    walk(None, 0)
    return rows


def leaders(report, count=5):
    """Slowest measured steps, preferring leaf steps so a parent does not hide its parts."""
    steps = measured(report["steps"])
    parents = {s["parent_id"] for s in steps}
    leaves = [s for s in steps if s["id"] not in parents] or steps
    return sorted(leaves, key=lambda s: s["seconds"], reverse=True)[:count]


def facts(report):
    steps = report["steps"]
    counts = {}
    for step in steps:
        counts[step["status"]] = counts.get(step["status"], 0) + 1
    skipped = sum(counts.get(s, 0) for s in SKIPPED)
    slow = leaders(report, 1)
    return {"counts": counts, "skipped": skipped, "total_steps": len(steps),
            "slowest": slow[0] if slow else None,
            "temperature": "unknown" if not steps else "cold" if not skipped
            else "warm" if skipped == len(steps) else "partly warm"}


def load(path):
    """Read report.json (or a run directory) into a normalised report."""
    path = Path(path)
    if path.is_dir():
        path = path / "report.json"
    try:
        return normalize(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError, ValueError, KeyError) as error:
        raise ValueError(f"cannot load timing report {path}: {error}") from error
