#!/usr/bin/env python3
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Configurable startup measurements without retaining subprocess logs."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache
import fcntl
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tomllib
import time
import uuid

@lru_cache(maxsize=1)
def config():
    path = Path(os.environ["STARTUP_TIMING_CONFIG"])
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if not data.get("groups") or not data.get("services") or not data.get("report", {}).get("directory"):
        raise ValueError(f"Incomplete startup timing configuration: {path}")
    return data


GROUPS = config()["groups"]
SERVICES = {name: (value["title"], value["description"])
            for name, value in config()["services"].items()}
CONTAINERS = {name: value["container"] for name, value in config()["services"].items()
              if value.get("container")}
IMAGE_SERVICES = config().get("image_services", {})
ELAPSED_STAGES = {item["group"]: item["title"] for item in config()["report"]["stages"]}
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def utc():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def journal(event):
    directory = os.environ.get("STARTUP_TIMING_DIR")
    if not directory:
        return
    # Only structured labels and measurements are retained, never commands,
    # environment variables, subprocess output or raw service logs.
    with (Path(directory) / "events.jsonl").open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(json.dumps(event) + "\n")
        handle.flush()


def begin(group, label):
    token = uuid.uuid4().hex
    journal({"event": "begin", "id": token, "group": group, "label": label,
             "monotonic": time.monotonic(), "utc": utc()})
    return token


def end(token, code=0, status=None, size=None):
    event = {"event": "end", "id": token, "monotonic": time.monotonic(),
             "utc": utc(), "status": status or ("ok" if code == 0 else "failed"),
             "returncode": code}
    if size is not None:
        event["bytes"] = size
    journal(event)


def cached(group, label):
    token = begin(group, label)
    end(token, status="cached")


def execute(command):
    """Forward termination to the owned child and wait before reporting."""
    child = subprocess.Popen(command)
    previous = {}

    def forward(signum, _frame):
        child.send_signal(signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, forward)
    try:
        return child.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def pull_images(command):
    images = subprocess.check_output(command, text=True).splitlines()
    images = sorted(set(images) - set(config().get("pull", {}).get("exclude_images", [])))

    def pull(image):
        present = subprocess.run(["docker", "image", "inspect", image],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if present.returncode == 0:
            cached("container_download", image)
            print(f"Container cached: {image}", flush=True)
            return 0
        token = begin("container_download", image)
        code = subprocess.run(["docker", "pull", image]).returncode
        size = subprocess.run(["docker", "image", "inspect", "-f", "{{.Size}}", image],
                              capture_output=True, text=True).stdout.strip() if code == 0 else ""
        end(token, code, size=int(size) if size.isdigit() else None)
        return code

    # Preserve concurrent downloads. Per-image elapsed times overlap; the
    # report also calculates their union instead of adding them as wall time.
    with ThreadPoolExecutor(max_workers=max(1, len(images))) as pool:
        codes = list(pool.map(pull, images))
    return next((code for code in codes if code), 0)


def interval(group, label, start, finish, method, status="ok"):
    return {"group": group, "label": label, "started_at": start, "ended_at": finish,
            "seconds": max(0.0, timestamp(finish) - timestamp(start)),
            "method": method, "status": status}


class StockPhases:
    """Apply configured log boundaries; missing boundaries never become zero."""

    def __init__(self, service):
        self.service = service
        self.pending = {}
        self.steps = []
        self.counts = {}
        self.rules = [(rule, re.compile(rule["pattern"]))
                      for rule in config().get("log_rules", []) if rule["service"] == service]
        self.ready_pattern = config().get("ready_patterns", {}).get(service,
                                                                    config().get("ready_patterns", {}).get("all"))

    def start(self, key, group, label, when):
        if key not in self.pending:
            self.pending[key] = (group, label, when)

    def close(self, key, when, group=None, append=""):
        if key in self.pending:
            category, label, started = self.pending.pop(key)
            self.steps.append(interval(group or category, label + append, started, when,
                                       "stock-log-boundary"))

    def skip(self, group, label, when):
        self.steps.append(interval(group, label, when, when, "stock-log", "cached"))

    def feed(self, when, message):
        text = ANSI.sub("", message)
        for index, (rule, pattern) in enumerate(self.rules):
            match = pattern.search(text)
            if not match:
                continue
            action, key = rule["action"], rule.get("key")
            if rule.get("close"):
                self.close(rule["close"], when)
            if action == "start":
                labels = rule.get("labels")
                if labels:
                    self.counts[index] = self.counts.get(index, 0) + 1
                    label = labels[self.counts[index] - 1] if self.counts[index] <= len(labels) else str(self.counts[index])
                else:
                    label = rule["label"].format(**match.groupdict())
                self.start(key, rule["group"], label, when)
            elif action == "close":
                self.close(key, when, rule.get("group"), rule.get("append", ""))
            elif action == "skip":
                label = rule["label"].format(**match.groupdict())
                self.skip(rule["group"], label, when)
            elif action == "close_or_skip":
                if key in self.pending:
                    self.close(key, when)
                else:
                    self.skip(rule["group"], rule["label"], when)
            break
        if self.ready_pattern and re.search(self.ready_pattern, text):
            ready = config().get("ready_patterns", {})
            self.close(ready.get("close_key", "export"), when, ready.get("close_group", "model_combined"),
                       ready.get("append", ""))
            self.ready_at = when

    def finish(self):
        for group, label, start in self.pending.values():
            self.steps.append({"group": group, "label": label, "started_at": start,
                               "ended_at": None, "seconds": None, "status": "unavailable",
                               "method": "missing-completion-marker"})
        return self.steps


def log_entry(line):
    when, separator, message = line.partition(" ")
    if not separator:
        return None
    try:
        timestamp(when)
    except ValueError:
        return None
    if message.startswith("{"):
        try:
            value = json.loads(message)
        except json.JSONDecodeError:
            return None
        message = value.get("event", "")
    return (when, message) if isinstance(message, str) else None


def observe_services(start, finish):
    steps, warnings = [], []
    for service, container in CONTAINERS.items():
        try:
            inspection = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Status}} {{.State.StartedAt}}", container],
                capture_output=True, text=True)
        except OSError:
            warnings.append("Docker inspection unavailable; stock service timings were not collected.")
            break
        if inspection.returncode:
            warnings.append(f"{service}: no container to inspect; internal phases unavailable.")
            continue
        try:
            state, started = inspection.stdout.strip().split(maxsplit=1)
            timestamp(started)
        except ValueError:
            warnings.append(f"{service}: invalid Docker start timestamp; internal phases unavailable.")
            continue
        if timestamp(started) < timestamp(start):
            if state == "running":
                steps.append(interval("service_startup", f"{service}: already running",
                                      start, start, "docker-state", "reused"))
            else:
                warnings.append(f"{service}: not started during this run; internal phases not measured.")
            continue
        parser = StockPhases(service)
        # Consume and discard logs incrementally; the report contains only
        # allowlisted phase labels, never stock messages (which may expose env).
        try:
            process = subprocess.Popen(
                ["docker", "logs", "--timestamps", "--since", start, "--until", finish, container],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except OSError:
            warnings.append(f"{service}: Docker logs unavailable; internal split unavailable.")
            continue
        with process.stdout:
            for line in process.stdout:
                entry = log_entry(line.rstrip("\n"))
                if entry and timestamp(start) <= timestamp(entry[0]) <= timestamp(finish):
                    parser.feed(*entry)
        if process.wait():
            warnings.append(f"{service}: Docker logs unavailable; internal timings may be incomplete.")
        observed = parser.finish()
        steps.extend(observed)
        ready = getattr(parser, "ready_at", None)
        if ready:
            steps.append(interval("service_startup", f"{service}: start to application ready",
                                  started, ready, "docker-state-and-stock-log"))
        if not observed:
            warnings.append(f"{service}: no recognized model phase markers; internal split unavailable.")
    return steps, warnings


def read_steps(path, failed):
    active, steps = {}, []
    for line in path.read_text().splitlines():
        event = json.loads(line)
        if event["event"] == "begin":
            active[event["id"]] = event
        else:
            opening = active.pop(event["id"])
            steps.append({
                "group": opening["group"], "label": opening["label"],
                "started_at": opening["utc"], "ended_at": event["utc"],
                "monotonic_started_at": opening["monotonic"], "monotonic_ended_at": event["monotonic"],
                "seconds": event["monotonic"] - opening["monotonic"],
                "status": event["status"], "method": "monotonic-clock",
                "returncode": event["returncode"],
                **({"bytes": event["bytes"]} if "bytes" in event else {}),
            })
    for opening in active.values():
        steps.append({"group": opening["group"], "label": opening["label"],
                      "started_at": opening["utc"], "ended_at": None, "seconds": None,
                      "monotonic_started_at": opening["monotonic"], "monotonic_ended_at": None,
                      "status": "failed" if failed else "unavailable",
                      "method": "unfinished-step"})
    return steps


def union_seconds(steps):
    windows = sorted((timestamp(step["started_at"]), timestamp(step["ended_at"]))
                     for step in steps)
    total, until = 0.0, float("-inf")
    for start, finish in windows:
        total += max(0.0, finish - max(start, until))
        until = max(until, finish)
    return total


def summarize(steps):
    groups = {}
    for group, label in GROUPS.items():
        members = [step for step in steps if step["group"] == group]
        measured = [step for step in members
                    if step["seconds"] is not None and step["status"] not in {"cached", "reused"}]
        groups[group] = {
            "label": label,
            "summed_seconds": sum(step["seconds"] for step in measured) if measured else None,
            "active_wall_seconds": union_seconds(measured) if measured else None,
            "measured_steps": len(measured),
            "cached_or_reused_steps": sum(step["status"] in {"cached", "reused"} for step in members),
            "unavailable_steps": sum(step["seconds"] is None for step in members),
        }
    return groups


def elapsed_breakdown(report):
    """Partition the startup script's elapsed time, not background service work."""
    start = timestamp(report["started_at"])
    elapsed = report["overall_seconds"]
    finish = min(elapsed, timestamp(report["ended_at"]) - start)
    steps = assign_parents(report["steps"])
    intervals = []
    clock = report.get("monotonic_started_at")
    for step in steps:
        if step["method"] != "monotonic-clock" or step["seconds"] is None or step["ended_at"] is None:
            continue
        if clock is not None and step.get("monotonic_started_at") is not None and step.get("monotonic_ended_at") is not None:
            left, right = step["monotonic_started_at"] - clock, step["monotonic_ended_at"] - clock
            limit = elapsed
        else:
            left, right = timestamp(step["started_at"]) - start, timestamp(step["ended_at"]) - start
            limit = finish
        intervals.append((max(0.0, left), min(limit, right), step))
    by_id = {step.get("id"): step for step in steps if step.get("id")}
    windows = []
    for left, right, step in intervals:
        if right <= left:
            continue
        parent_id = step.get("parent_id")
        nested = False
        while parent_id and parent_id in by_id:
            parent = by_id[parent_id]
            if parent["method"] == "monotonic-clock":
                nested = True
                break
            parent_id = parent.get("parent_id")
        if nested:
            continue
        windows.append((left, right, step["group"]))
    boundaries = sorted({0.0, elapsed, *(edge for left, right, _ in windows for edge in (left, right))})
    totals = {}
    # ponytail: O(n²) overlap scan; use an interval sweep if traces grow large.
    for left, right in zip(boundaries, boundaries[1:]):
        active = {group for begin, end, group in windows if begin <= left and right <= end}
        group = next(iter(active)) if len(active) == 1 else "parallel" if active else "unmeasured"
        totals[group] = totals.get(group, 0.0) + right - left
    return totals


def rounded_partition(totals, overall, preferred=None):
    """Round an additive partition to tenths without breaking its displayed sum.

    ``preferred`` fixes a group's displayed units (for example the sum of its
    displayed steps) when the other groups can still absorb the remainder.
    """
    if not math.isclose(sum(totals.values()), overall, abs_tol=1e-6):
        raise ValueError("Elapsed-time breakdown does not match overall startup time")
    target = round(overall * 10)
    units = {group: math.floor(value * 10) for group, value in totals.items()}
    fixed = {group: value for group, value in (preferred or {}).items() if group in totals}
    free = [group for group in totals if group not in fixed]
    remaining = target - sum(units[group] for group in free) - sum(fixed.values())
    # Other rows may round down as well as up, but never below zero.
    if not free or abs(remaining) > len([group for group in free if remaining > 0 or units[group] > 0]):
        fixed, free, remaining = {}, list(totals), target - sum(units.values())
    units.update(fixed)
    order = sorted((group for group in free if remaining > 0 or units[group] > 0),
                   key=lambda group: totals[group] * 10 - units[group], reverse=remaining > 0)
    for group in order[:abs(remaining)]:
        units[group] += 1 if remaining > 0 else -1
    return units


def elapsed_rows(report):
    totals = elapsed_breakdown(report)
    # A sequential stage shows the same total as its rows elsewhere in the report.
    preferred = {}
    for group, value in totals.items():
        members = [step for step in report["steps"] if step["group"] == group]
        units = summed_units(members)
        if units is not None and abs(sum(step["seconds"] for step in members
                                         if step["seconds"] is not None) - value) < 0.05:
            preferred[group] = units
    displayed = rounded_partition(totals, report["overall_seconds"], preferred)
    order = [*ELAPSED_STAGES, *(key for key in totals if key not in ELAPSED_STAGES)]
    return [(ELAPSED_STAGES[key] if key in ELAPSED_STAGES else GROUPS[key], seconds(displayed[key] / 10))
            for key in order if key in displayed]


def summed_units(steps):
    measured = [step for step in steps
                if step["seconds"] is not None and step["status"] not in {"cached", "reused"}]
    return sum(round(step["seconds"] * 10) for step in measured) if measured else None


def seconds(value):
    if value is None:
        return "Not measured"
    value = round(value * 10) / 10
    if value < 60:
        return f"{value:.1f} s"
    minutes, remainder = divmod(round(value, 1), 60)
    if minutes < 60:
        return f"{int(minutes)}m {remainder:04.1f}s"
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours}h {minutes:02d}m {remainder:04.1f}s"


def service_for(step):
    group, label = step["group"], step["label"]
    if group == "container_download":
        image = label.split(" (", 1)[0]
        for prefix, service in sorted(IMAGE_SERVICES.items(), key=lambda item: len(item[0]), reverse=True):
            if image.startswith(prefix):
                return service
    for rule in config()["attribution"].get("groups", []):
        if group == rule["group"]:
            return rule["service"]
    for rule in config()["attribution"].get("labels", []):
        if re.search(rule["pattern"], label):
            return rule["service"]
    return config()["attribution"]["shared_service"]


def assign_parents(steps):
    steps = [dict(step, service=step.get("service") or service_for(step),
                  id=step.get("id", f"step-{index:04d}"), parent_id=step.get("parent_id"))
             for index, step in enumerate(steps, 1)]
    # ponytail: O(n²) matching is fine for startup traces; index intervals if reports grow large.
    for child in steps:
        if child["parent_id"] or child["ended_at"] is None:
            continue
        candidates = []
        for rule in config()["report"].get("parent_rules", []):
            if (child["service"] not in rule["child_services"]
                    or child["group"] not in rule["child_groups"]):
                continue
            for parent in steps:
                if parent["id"] == child["id"] or parent["group"] != rule["parent_group"]:
                    continue
                if parent["service"] != rule["parent_service"] or parent["ended_at"] is None:
                    continue
                if (timestamp(parent["started_at"]) <= timestamp(child["started_at"])
                        and timestamp(child["ended_at"]) <= timestamp(parent["ended_at"])):
                    span = timestamp(parent["ended_at"]) - timestamp(parent["started_at"])
                    candidates.append((span, parent["id"]))
        if candidates:
            child["parent_id"] = min(candidates)[1]
    return steps


def markdown(value):
    return str(value).replace("|", r"\|").replace("\n", " ").replace("\r", " ")


def step_title(step):
    group, label = step["group"], step["label"]
    if group == "container_download":
        size = step.get("bytes")
        return f"Download container image: `{label}`" + (f" ({data_size(size)})" if size else "")
    if step["group"] == "service_startup" and step["status"] == "reused":
        return "Service status"
    for rule in config()["report"].get("step_titles", []):
        if rule.get("group") not in {None, group}:
            continue
        match = re.search(rule["pattern"], label)
        if match:
            fields = {"label": label, **match.groupdict()}
            return rule["title"].format(**fields)
    return label


def data_size(value):
    return f"{value / 1e9:.1f} GB" if value >= 1e9 else f"{value / 1e6:.0f} MB"


def offset(when, origin):
    """Time relative to the report start, for example +5:26."""
    minutes, remainder = divmod(max(0, int(timestamp(when) - origin)), 60)
    return f"+{minutes}:{remainder:02d}"


def headline(steps, origin):
    """Header lines: the slowest service step and how much work was cached."""
    lines = []
    measured = [step for step in steps
                if step["seconds"] is not None and step["status"] not in {"cached", "reused"}]
    parent_groups = {rule["parent_group"] for rule in config()["report"].get("parent_rules", [])}
    slowest = max((step for step in measured if step["group"] in parent_groups),
                  key=lambda step: step["seconds"], default=None)
    if slowest:
        title = SERVICES.get(slowest["service"], (slowest["service"],))[0]
        text = (f"**Slowest service:** {markdown(title)}, {seconds(slowest['seconds'])} "
                f"({offset(slowest['started_at'], origin)} to {offset(slowest['ended_at'], origin)})")
        parts = sorted((step for step in measured if step.get("parent_id") == slowest["id"]),
                       key=lambda step: step["seconds"], reverse=True)[:2]
        if parts:
            text += "; largest parts: " + "; ".join(
                f"{markdown(step_title(step))} {seconds(step['seconds'])}" for step in parts)
        lines.append(text + "  ")
    work_groups = {item["group"] for item in config()["report"].get("summaries", [])}
    work = [step for step in steps if step["group"] in work_groups and step["status"] != "unavailable"]
    reused = sum(step["status"] in {"cached", "reused"} for step in work)
    if work and not reused:
        lines.append(f"**Cache:** nothing reused; all {len(work)} download and preparation steps ran.  ")
    elif work and reused == len(work):
        lines.append("**Cache:** everything reused; no downloads or preparation were needed.  ")
    elif work:
        lines.append(f"**Cache:** {reused} of {len(work)} download and preparation steps reused.  ")
    return lines


def step_rows(steps, origin):
    """Table rows; two or more short successful steps share one row."""
    limit = config()["report"].get("quick_step_seconds", 1.0)
    quick = [step for step in steps if step["status"] == "ok"
             and step["seconds"] is not None and step["seconds"] < limit]
    if len(quick) < 2:
        quick = []
    folded = {id(step) for step in quick}
    rows = []
    for step in steps:
        if id(step) not in folded:
            rows.append(f"| {markdown(step_title(step))} | {offset(step['started_at'], origin)} "
                        f"| {step_time(step)} |")
        elif step is quick[0]:
            names = ", ".join(step_title(item) for item in quick)
            rows.append(f"| {len(quick)} quick steps: {markdown(names)} | {offset(step['started_at'], origin)} "
                        f"| {seconds(summed_units(quick) / 10)} |")
    return rows


def step_time(step):
    status = step["status"]
    if status == "cached":
        return "Cached"
    if status == "reused":
        return "Already running"
    duration = seconds(step["seconds"])
    if status == "failed":
        return f"{duration} **(failed)**"
    return duration


def warning_text(warning):
    message = warning.split(": ", 1)[-1]
    for rule in config()["report"].get("warning_text", []):
        if re.search(rule["pattern"], message):
            return rule["title"]
    return message


def summary_time(members):
    if members and all(step["status"] in {"cached", "reused"} for step in members):
        return "Cached / reused"
    units = summed_units(members)
    return seconds(units / 10 if units is not None else None)


def preparation_rows(parent, phases):
    """Reconcile internal phases with their inclusive preparation duration."""
    measured = [step for step in phases
                if step["seconds"] is not None and step["status"] not in {"cached", "reused"}]
    if any(timestamp(step["started_at"]) < timestamp(parent["started_at"])
           or timestamp(step["ended_at"]) > timestamp(parent["ended_at"]) for step in measured):
        return None
    overlap = sum(step["seconds"] for step in measured) - union_seconds(measured)
    overlap_units = round(max(0.0, overlap) * 10)
    other_units = round(parent["seconds"] * 10) - (summed_units(phases) or 0) + overlap_units
    if overlap_units < 10:  # sub-second overlap is netted into Other rather than shown
        other_units, overlap_units = other_units - overlap_units, 0
    rows = []
    if overlap_units:
        rows.append(f"| Concurrent phases counted twice (subtract) | | -{seconds(overlap_units / 10)} |")
    if other_units > 0:
        rows.append(f"| Other initialization / unmeasured phases | | {seconds(other_units / 10)} |")
    elif other_units < 0:
        rows.append(f"| Rounding adjustment | | -{seconds(-other_units / 10)} |")
    rows.append(f"| **Preparation total** | | **{step_time(parent)}** |")
    return rows


def summary_counts(members):
    counts = []
    cached_count = sum(step["status"] in {"cached", "reused"} for step in members)
    failed_count = sum(step["status"] == "failed" for step in members)
    missing_count = sum(step["seconds"] is None for step in members)
    if cached_count:
        counts.append(f"{cached_count} cached/reused")
    if failed_count:
        counts.append(f"{failed_count} failed")
    if missing_count:
        counts.append(f"{missing_count} not measured")
    return "; ".join(counts)


def render_markdown(report):
    groups, steps = report["groups"], report["steps"]
    steps = assign_parents(steps)
    origin = timestamp(report["started_at"])
    started = datetime.fromisoformat(report["started_at"].replace("Z", "+00:00"))
    started = started.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M:%S UTC")
    result = ("Ready" if report["status"] == "ok" else "Failed")
    report_config = config()["report"]
    lines = [
        "# Startup timing report", "",
        f"**Result:** {result} after **{seconds(report['overall_seconds'])}**"
        + (f" (exit {report['returncode']})" if report["returncode"] else "") + "  ",
        f"**{report_config.get('target_label', 'Target')}:** "
        f"`{markdown(report_config.get('target_format', '{target}').format(target=report['target']))}`  ",
        *headline(steps, origin),
        f"**Started:** {started}", "",
        "## Where did the time go?", "",
        report_config.get("overview_intro",
                          "**These rows add up to the overall startup time.** Each moment is counted once."), "",
        "| Startup stage | Elapsed time |",
        "|---|---:|",
    ]
    for label, duration in elapsed_rows(report):
        lines.append(f"| {label} | {duration} |")
    lines += [
        f"| **Total startup time** | **{seconds(report['overall_seconds'])}** |",
        "", report_config.get("elapsed_note",
                              "Uninstrumented time includes gaps between timers and timer overhead."),
        "", "## Work totals across services", "",
        report_config.get("work_intro",
                          "Each total below is the **sum of its individual step durations**. "
                          "These work measurements are not additional startup time."), "",
        "| Activity | Sum of individual steps | What this includes |",
        "|---|---:|---|",
    ]
    has_partial = False
    for summary in report_config.get("summaries", []):
        key, label, meaning = summary["group"], summary["title"], summary["meaning"]
        members = [step for step in steps if step["group"] == key]
        duration = summary_time(members)
        partial_labels = summary.get("partial_if_group_labels", {})
        partial = (any(step["group"] in summary.get("partial_if_groups", [])
                   and step["status"] not in {"cached", "reused"} for step in steps)
               or any(step["group"] == group and text in step["label"].lower()
                      and step["status"] not in {"cached", "reused"}
                      for group, text in partial_labels.items() for step in steps)
                   or summary.get("partial_if_unavailable", False) and groups[key]["unavailable_steps"] > 0
                   or summary.get("partial_if_warning", False) and bool(report["warnings"]))
        if partial:
            duration += " **(partial)**"
            has_partial = True
        counts = summary_counts(members)
        if counts:
            meaning += f" {counts}."
        lines.append(f"| {label} | {duration} | {meaning} |")
    lines += [
        "", "**Cached** means no new work was needed. **Not measured** does not mean zero.",
    ]
    if has_partial:
        lines += ["", report_config.get("partial_note",
                                        "**Partial totals:** some work could not be separated or measured; "
                                        "see the affected service details.")]
    if any(step["status"] == "failed" for step in steps):
        lines += ["", report_config.get("failure_note",
                                        "**Attention:** a step failed; its duration is included where available "
                                        "and marked in the service details.")]
    lines += [
        "", "## Details by service", "",
        report_config.get("details_intro",
                          "Nested rows are included in their parent total and must not be added to it."), "",
    ]
    rendered_ids = set()
    for service, (title, purpose) in SERVICES.items():
        members = [step for step in steps if step["service"] == service and step["id"] not in rendered_ids]
        warnings = [warning for warning in report["warnings"] if warning.startswith(service + ":")]
        if not members and not warnings:
            continue
        if lines[-1] != "":
            lines.append("")
        lines += [f"### {title}", "", purpose, ""]
        if members:
            child_ids = {step["parent_id"] for step in members if step.get("parent_id")}
            parents = [step for step in members if step["id"] in child_ids]
            for parent in parents:
                rule = next((item for item in report_config.get("parent_rules", [])
                             if item["parent_group"] == parent["group"]
                             and item["parent_service"] == service), {})
                children = sorted((step for step in steps if step.get("parent_id") == parent["id"]),
                                  key=lambda item: timestamp(item["started_at"]))
                # A child that mostly repeats another kind of child's time (a wait around an
                # install) would be counted twice in the table; it is listed under Other steps.
                # ponytail: heuristic; mark wrappers explicitly in the config if it misfires.
                children = [step for step in children if not any(
                    other["group"] != step["group"] and step["ended_at"] and other["ended_at"]
                    and other["seconds"] < step["seconds"]
                    and min(timestamp(step["ended_at"]), timestamp(other["ended_at"]))
                    - max(timestamp(step["started_at"]), timestamp(other["started_at"])) >= step["seconds"] / 2
                    for other in children)]
                lines += [f"**{markdown(rule.get('preparation_title', step_title(parent)))}:** "
                          f"{step_time(parent)}, {offset(parent['started_at'], origin)} to "
                          f"{offset(parent['ended_at'], origin) if parent['ended_at'] else 'unfinished'}.", ""]
                lines += ["| Included step | Starts | Time |", "|---|---:|---:|"]
                lines += step_rows(children, origin)
                measured = parent["seconds"] is not None and parent["status"] not in {"cached", "reused"}
                rows = preparation_rows(parent, children) if measured else None
                if rows is not None:
                    lines += rows
                lines.append("")
                if measured and rows is None:
                    lines += ["**Cannot reconcile:** child timestamps fall outside the parent interval.", ""]
                rendered_ids.update({parent["id"], *(step["id"] for step in children)})
            for total in report_config.get("service_totals", []):
                subtotal = [step for step in members if step["group"] == total["group"]]
                if subtotal:
                    partial = (total.get("partial_if_unavailable", False)
                               and any(step["seconds"] is None or step["status"] == "failed"
                                       for step in subtotal))
                    lines += [f"**{total['title']}:** {summary_time(subtotal)}"
                              f"{' (partial)' if partial else ''}.", ""]
            remaining = [step for step in members
                         if step["id"] not in rendered_ids and step["id"] not in {item["id"] for item in parents}]
            if remaining:
                if parents:
                    lines += ["**Other steps** (may overlap the parent total):", ""]
                lines += ["| Step | Starts | Time |", "|---|---:|---:|"]
                remaining = sorted(remaining, key=lambda item: (timestamp(item["started_at"]), item["label"]))
                lines += step_rows(remaining, origin)
                rendered_ids.update(step["id"] for step in remaining)
                lines.append("")
        for note in report_config.get("service_notes", []):
            if note["service"] == service and any(step["group"] == note["group"] for step in members):
                lines += [note["text"], ""]
        for warning in warnings:
            lines += [f"**Not measured:** {markdown(warning_text(warning))}", ""]
    unmatched = [warning for warning in report["warnings"]
                 if not any(warning.startswith(service + ":") for service in SERVICES)]
    if unmatched:
        lines += ["## Collection issues", "", *("- " + markdown(warning) for warning in unmatched), ""]
    lines += [
        "## Measurement notes", "",
        *("- " + note for note in report_config.get("measurement_notes", [])),
        "- **Starts** is the time after the startup command began, in minutes and seconds.",
    ]
    if sum(step["group"] == "container_download" and step["status"] == "ok" for step in steps) > 1:
        lines.append("- Container images download in parallel and share bandwidth, so each image's time "
                     "includes waiting for the others; its size is the unpacked image size.")
    lines += [
        "- Displayed values use tenths of a second; the elapsed-time partition is rounded together "
        "so its rows still add up.",
        "- Detailed durations and timestamped sources are in `report.json`.", "",
    ]
    return "\n".join(lines)


def write_report(directory, target, start, finish, elapsed, code, steps, warnings, clock_start=None):
    steps = assign_parents([dict(step, service=service_for(step)) for step in steps])
    groups = summarize(steps)
    report = {"target": target, "started_at": start, "ended_at": finish,
              "overall_seconds": elapsed, "returncode": code,
              "status": "ok" if code == 0 else "failed", "groups": groups,
              "steps": steps, "warnings": warnings}
    if clock_start is not None:
        report["monotonic_started_at"] = clock_start
    report["elapsed_breakdown"] = elapsed_breakdown(report)
    (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    (directory / "report.md").write_text(render_markdown(report))
    return report


def run(target, command):
    report_config = config()["report"]
    root_env = report_config.get("root_env", "ASSET_ROOT")
    root = Path(os.environ.get(root_env, report_config.get("root_default", "."))) / report_config["directory"]
    directory = root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True, mode=0o700)
    os.environ["STARTUP_TIMING_DIR"] = str(directory)
    (directory / "events.jsonl").touch(mode=0o600)
    start, clock = utc(), time.monotonic()
    code = 1
    try:
        code = execute(command)
    finally:
        finish, elapsed = utc(), time.monotonic() - clock
        steps = read_steps(directory / "events.jsonl", code != 0)
        observed, warnings = observe_services(start, finish)
        steps.extend(observed)
        report = write_report(directory, target, start, finish, elapsed, code, steps, warnings, clock_start=clock)
        print(f"\nStartup timing report available: {directory / 'report.md'}", flush=True)
        print(f"Machine-readable report: {directory / 'report.json'}", flush=True)
        print(f"Overall {report['status']}: {seconds(elapsed)}", flush=True)
        print("Elapsed startup breakdown (adds up to overall):", flush=True)
        for label, duration in elapsed_rows(report):
            print(f"  {label}: {duration}", flush=True)
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    runner = sub.add_parser("run")
    runner.add_argument("--target", choices=config()["report"].get("target_names"), required=True)
    runner.add_argument("command", nargs=argparse.REMAINDER)
    opening = sub.add_parser("begin")
    opening.add_argument("group", choices=GROUPS)
    opening.add_argument("label")
    closing = sub.add_parser("end")
    closing.add_argument("token")
    closing.add_argument("--code", type=int, default=0)
    closing.add_argument("--status", choices=("ok", "failed", "cached", "reused"))
    pulling = sub.add_parser("pull")
    pulling.add_argument("command", nargs=argparse.REMAINDER)
    rendering = sub.add_parser("render", help="Regenerate Markdown from an existing report.json; no startup")
    rendering.add_argument("reports", type=Path, nargs="+")
    args = parser.parse_args()
    if args.action == "render":
        for path in args.reports:
            report = json.loads(path.read_text())
            report["steps"] = [dict(step, service=service_for(step)) for step in report["steps"]]
            output = path.with_suffix(".md")
            output.write_text(render_markdown(report))
            print(f"Startup timing report available: {output}")
        return 0
    if args.action == "begin":
        print(begin(args.group, args.label))
        return 0
    if args.action == "end":
        end(args.token, args.code, args.status)
        return 0
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a command is required after --")
    if args.action == "pull":
        return pull_images(command)
    return run(args.target, command)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"startup timing failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
