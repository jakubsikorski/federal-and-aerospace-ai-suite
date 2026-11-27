# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Optional plugin: phases of stock service containers read from their logs.

Loaded only when the config defines [[log_rules]] or a service `container`. Rules are
data (see the Handheld startup-timing.toml); a missing end marker is reported as
unavailable, never as zero. Raw log text is parsed and discarded, never stored.
"""
import json
import re
import subprocess

from .journal import parse_time

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def interval(group, label, start, finish, method, status="ok", service=None):
    return {"group": group, "label": label, "service": service, "started_at": start,
            "ended_at": finish, "seconds": max(0.0, parse_time(finish) - parse_time(start)),
            "method": method, "status": status, "background": True}


class StockPhases:
    def __init__(self, cfg, service):
        self.service, self.pending, self.steps, self.counts = service, {}, [], {}
        self.ready_at = None
        self.ready = cfg.get("ready_patterns", {})
        self.ready_pattern = self.ready.get(service, self.ready.get("all"))
        self.rules = [(rule, re.compile(rule["pattern"]))
                      for rule in cfg.get("log_rules", []) if rule["service"] == service]

    def start(self, key, group, label, when):
        self.pending.setdefault(key, (group, label, when))

    def close(self, key, when, group=None, append=""):
        if key in self.pending:
            category, label, started = self.pending.pop(key)
            self.steps.append(interval(group or category, label + append, started, when,
                                       "stock-log-boundary", service=self.service))

    def skip(self, group, label, when):
        self.steps.append(interval(group, label, when, when, "stock-log", "cached", self.service))

    def feed(self, when, message):
        text = ANSI.sub("", message)
        for index, (rule, pattern) in enumerate(self.rules):
            match = pattern.search(text)
            if match:
                self.apply(index, rule, match, when)
                break
        if self.ready_pattern and re.search(self.ready_pattern, text):
            self.close(self.ready.get("close_key", "export"), when,
                       self.ready.get("close_group", "model_combined"), self.ready.get("append", ""))
            self.ready_at = when

    def apply(self, index, rule, match, when):
        action, key = rule["action"], rule.get("key")
        if rule.get("close"):
            self.close(rule["close"], when)
        if action == "start":
            labels = rule.get("labels")
            if labels:
                self.counts[index] = self.counts.get(index, 0) + 1
                label = labels[self.counts[index] - 1] if self.counts[index] <= len(labels) \
                    else str(self.counts[index])
            else:
                label = rule["label"].format(**match.groupdict())
            self.start(key, rule["group"], label, when)
        elif action == "close":
            self.close(key, when, rule.get("group"), rule.get("append", ""))
        elif action == "skip":
            self.skip(rule["group"], rule["label"].format(**match.groupdict()), when)
        elif action == "close_or_skip":
            if key in self.pending:
                self.close(key, when)
            else:
                self.skip(rule["group"], rule["label"], when)

    def finish(self):
        for group, label, start in self.pending.values():
            self.steps.append({"group": group, "label": label, "service": self.service,
                               "started_at": start, "ended_at": None, "seconds": None,
                               "status": "unavailable", "method": "missing-completion-marker",
                               "background": True})
        return self.steps


def log_entry(line):
    when, separator, message = line.partition(" ")
    if not separator:
        return None
    try:
        parse_time(when)
    except ValueError:
        return None
    if message.startswith("{"):
        try:
            message = json.loads(message).get("event", "")
        except (json.JSONDecodeError, AttributeError):
            return None
    return (when, message) if isinstance(message, str) else None


def observe_services(cfg, start, finish):
    """Return (steps, warnings) for every configured container started during [start, finish]."""
    steps, warnings = [], []
    for service, item in cfg.get("services", {}).items():
        container = item.get("container")
        if not container:
            continue
        try:
            found = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Status}} {{.State.StartedAt}}", container],
                capture_output=True, text=True)
        except OSError:
            warnings.append("Docker inspection unavailable; stock service timings were not collected.")
            break
        if found.returncode:
            warnings.append(f"{service}: no container to inspect; internal phases unavailable.")
            continue
        try:
            state, started = found.stdout.strip().split(maxsplit=1)
            parse_time(started)
        except ValueError:
            warnings.append(f"{service}: invalid Docker start timestamp; internal phases unavailable.")
            continue
        if parse_time(started) < parse_time(start):
            if state == "running":
                steps.append(interval("service_startup", f"{service}: already running",
                                      start, start, "docker-state", "reused", service))
            else:
                warnings.append(f"{service}: not started during this run; internal phases not measured.")
            continue
        steps.extend(_read_logs(cfg, service, container, started, start, finish, warnings))
    return steps, warnings


def _read_logs(cfg, service, container, started, start, finish, warnings):
    parser = StockPhases(cfg, service)
    try:
        process = subprocess.Popen(
            ["docker", "logs", "--timestamps", "--since", start, "--until", finish, container],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace")
    except OSError:
        warnings.append(f"{service}: Docker logs unavailable; internal split unavailable.")
        return []
    with process.stdout:  # consumed and discarded line by line
        for line in process.stdout:
            entry = log_entry(line.rstrip("\n"))
            if entry and parse_time(start) <= parse_time(entry[0]) <= parse_time(finish):
                parser.feed(*entry)
    if process.wait():
        warnings.append(f"{service}: Docker logs unavailable; internal timings may be incomplete.")
    steps = parser.finish()
    if parser.ready_at:
        steps.append(interval(cfg.get("ready_patterns", {}).get("startup_group", "service_startup"),
                              f"{service}: start to application ready", started, parser.ready_at,
                              "docker-state-and-stock-log", service=service))
    if not steps:
        warnings.append(f"{service}: no recognized model phase markers; internal split unavailable.")
    return steps
