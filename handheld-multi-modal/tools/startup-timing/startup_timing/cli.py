# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Command line: run, begin, end, render, compare, pull."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

from . import config as configuration
from . import journal, render, services
from .report import build, load


def _same_group(child):
    try:
        return os.getpgid(child.pid) == os.getpgid(0)
    except OSError:
        return False


def load_config(path=None):
    """The optional TOML, or {} (with a warning) when it is unreadable: it must never stop a startup."""
    try:
        return configuration.load(path)
    except ValueError as error:
        print(f"startup timing: {error}; continuing without configuration", file=sys.stderr)
        return {}


def execute(command):
    """Run the command, forwarding SIGINT/SIGTERM to it and waiting for it before reporting."""
    child = subprocess.Popen(command)

    def forward(number, _frame):
        # Ctrl-C reaches the whole foreground process group, so the child already has it.
        if number == signal.SIGINT and _same_group(child):
            return
        try:
            child.send_signal(number)
        except ProcessLookupError:  # the child exited just before the signal arrived
            pass

    previous = {n: signal.signal(n, forward) for n in (signal.SIGINT, signal.SIGTERM)}
    try:
        code = child.wait()
        return 128 - code if code < 0 else code  # killed by signal N: shell convention 128+N
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def collect_logs(cfg, start, finish, enabled=True):
    """Optional service-log plugin; imported only when the config asks for it."""
    if not (enabled and configuration.uses_log_plugin(cfg)):
        return [], []
    try:
        from . import logmarkers
        return logmarkers.observe_services(cfg, start, finish)
    except Exception as error:  # pylint: disable=broad-exception-caught  # best effort, never fatal
        return [], [f"Service log collection failed ({type(error).__name__}: {error}); stock service timings are missing."]


def write(directory, report, cfg, explain=False, gantt=False, keep_json=False):
    if not keep_json:
        (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    (directory / "report.md").write_text(render.markdown(report, cfg, explain, gantt))
    (directory / "summary.json").write_text(json.dumps(services.summary(report, cfg), indent=2) + "\n")
    return directory / "report.md"


def link_latest(root, directory):
    link, temporary = root / "latest", root / f".latest-{uuid.uuid4().hex[:6]}"
    try:
        temporary.symlink_to(directory.name)
        os.replace(temporary, link)
    except OSError:
        temporary.unlink(missing_ok=True)  # a symlink is a convenience, never a failure


def finalise(directory, root, name, start, clock, code, cfg, logs, explain, gantt):
    finish, elapsed = journal.utc(), time.monotonic() - clock
    steps = journal.read_steps(directory / "events.jsonl", code != 0)
    extra, warnings = collect_logs(cfg, start, finish, logs)
    report = build(name, start, finish, elapsed, code, steps + extra, warnings, clock)
    path = write(directory, report, cfg, explain, gantt)
    link_latest(root, directory)
    print(render.console(report, path, cfg), flush=True)


def run(command, name=None, config_path=None, output=None, explain=False, gantt=False, logs=True):
    cfg = load_config(config_path)
    found = configuration.find(config_path) if cfg else None  # an unusable file is not handed to child commands
    root = configuration.report_root(cfg, output).resolve()  # scripts may change directory
    try:
        directory = root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-") + uuid.uuid4().hex[:8])
        directory.mkdir(parents=True, mode=0o700)
        (directory / "events.jsonl").touch(mode=0o600)
    except OSError as error:  # a report we cannot write must not stop the application from starting
        print(f"startup timing: cannot create the report directory ({error}); running without timing",
              file=sys.stderr)
        for variable in ("STARTUP_TIMING_DIR", "STARTUP_TIMING_PARENT", "STARTUP_TIMING_CONFIG"):
            os.environ.pop(variable, None)
        return execute(command)
    os.environ["STARTUP_TIMING_DIR"] = str(directory)
    os.environ.pop("STARTUP_TIMING_PARENT", None)
    if found:
        os.environ["STARTUP_TIMING_CONFIG"] = found
    start, clock, code = journal.utc(), time.monotonic(), 1
    try:
        code = execute(command)
    finally:
        try:
            finalise(directory, root, name or Path(command[0]).name, start, clock, code, cfg, logs, explain, gantt)
        except Exception as error:  # pylint: disable=broad-exception-caught
            # The report is a convenience: never replace the command's own exit status.
            print(f"startup timing: could not write the report ({type(error).__name__}: {error})",
                  file=sys.stderr)
    return code


def strip_separator(command):
    return command[1:] if command[:1] == ["--"] else command


def parser():
    top = argparse.ArgumentParser(prog="timing", description="Time the steps of a startup command.")
    sub = top.add_subparsers(dest="action", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="optional TOML (default: $STARTUP_TIMING_CONFIG or ./startup-timing.toml)")
    view = argparse.ArgumentParser(add_help=False)
    view.add_argument("--explain", action="store_true", help="add per-section totals and notes")
    view.add_argument("--gantt", action="store_true", help="add a Mermaid gantt chart")
    runner = sub.add_parser("run", parents=[common, view], help="run a command and write a report")
    runner.add_argument("--name", help="report title (default: the command name)")
    runner.add_argument("--output", help="report root directory (default: ./startup-timings)")
    runner.add_argument("--no-logs", action="store_true", help="skip the optional service-log plugin")
    runner.add_argument("command", nargs=argparse.REMAINDER)
    opening = sub.add_parser("begin", help="start a step; prints its token")
    opening.add_argument("--service")
    opening.add_argument("--parent")
    opening.add_argument("group")
    opening.add_argument("label")
    closing = sub.add_parser("end", help="finish a step")
    closing.add_argument("token")
    closing.add_argument("--code", type=int, default=0)
    closing.add_argument("--status", choices=("ok", "failed", "cached", "reused"))
    pulling = sub.add_parser("pull", parents=[common], help="plugin: pull the images a command lists")
    pulling.add_argument("command", nargs=argparse.REMAINDER)
    rendering = sub.add_parser("render", parents=[common, view], help="re-render report.md from report.json")
    rendering.add_argument("reports", type=Path, nargs="+")
    summing = sub.add_parser("summary", parents=[common], help="print the short, paste-ready summary of a run")
    summing.add_argument("report", type=Path, nargs="?",
                         help="report.json or run directory (default: latest run under the report root)")
    summing.add_argument("--json", action="store_true", help="print summary.json instead of Markdown")
    comparing = sub.add_parser("compare", help="per-step deltas between two reports")
    comparing.add_argument("first", type=Path)
    comparing.add_argument("second", type=Path)
    return top


def main(argv=None):
    top = parser()
    args = top.parse_args(argv)
    if args.action == "begin":
        try:
            print(journal.begin(args.group, args.label, args.service, args.parent))
        except OSError as error:  # timing is observational: never fail the application
            print(f"startup timing: cannot record step: {error}", file=sys.stderr)
        return 0
    if args.action == "end":
        try:
            journal.end(args.token, args.code, args.status)
        except OSError as error:
            print(f"startup timing: cannot record step end: {error}", file=sys.stderr)
        return 0
    if args.action == "compare":
        from .compare import compare
        print(compare(args.first, args.second))
        return 0
    if args.action == "summary":
        cfg = load_config(args.config)
        data = load(args.report or configuration.report_root(cfg) / "latest")
        print(json.dumps(services.summary(data, cfg), indent=2) if args.json else "\n".join(render.brief(data, cfg)))
        return 0
    if args.action == "render":
        cfg = load_config(args.config)
        for path in args.reports:
            path = path / "report.json" if path.is_dir() else path
            output = write(path.parent, load(path), cfg, args.explain, args.gantt, keep_json=True)
            print(f"Startup timing report available: {output}")
        return 0
    command = strip_separator(args.command)
    if not command:
        top.error("a command is required after --")
    if args.action == "pull":
        from .images import pull_images
        return pull_images(load_config(args.config), command)
    return run(command, args.name, args.config, args.output, args.explain, args.gantt, not args.no_logs)


def entry():
    try:
        return main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"startup timing failed: {error}", file=sys.stderr)
        return 1
