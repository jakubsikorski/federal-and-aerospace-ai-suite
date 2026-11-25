# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Markdown and console views of a normalised report."""
from datetime import datetime, timezone
from pathlib import Path

from . import config as configuration
from .report import SKIPPED, facts, leaders, timeline

BAR = 20


def seconds(value):
    if value is None:
        return "unavailable"
    if value < 0.05:
        return "<0.1 s"
    value = round(value, 1)
    if value < 60:
        return f"{value:.1f} s"
    minutes, rest = divmod(value, 60)
    if minutes < 60:
        return f"{int(minutes)}m {rest:04.1f}s"
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours}h {minutes:02d}m {rest:04.1f}s"


def offset(value):
    if value is None:
        return "n/a"
    value = round(value, 1)
    if value < 60:
        return f"+{value:.1f} s"
    minutes, rest = divmod(int(value), 60)
    return f"+{minutes}:{rest:02d}"


def size(value):
    return f"{value / 1e9:.1f} GB" if value >= 1e9 else f"{value / 1e6:.0f} MB"


def cell(text):
    return str(text).replace("|", r"\|").replace("\n", " ").replace("\r", " ")


def bar(start, length, total):
    """A strip showing where in the run a step sat and how much of it the step filled."""
    if not total or start is None or length is None:
        return ""
    left = min(BAR - 1, int(start / total * BAR))
    width = max(1, min(BAR - left, round(length / total * BAR)))
    return "░" * left + "█" * width + "░" * (BAR - left - width)


def percent(value, total):
    return f"{value / total * 100:.0f}%" if total and value is not None else ""


def share(value, total, wrap="({})"):
    text = percent(value, total)
    return wrap.format(text) if text else ""


def title(step):
    return step["label"] + (f" ({size(step['bytes'])})" if step.get("bytes") else "")


def status_text(step):
    status = step["status"]
    if status == "reused":
        return "already running"
    if status == "unavailable" or step["seconds"] is None:
        return "unavailable" if status != "failed" else "failed (no end recorded)"
    return status


def duration(step):
    return "-" if step["status"] in SKIPPED else seconds(step["seconds"])


def section(cfg, step):
    text = configuration.group_title(cfg, step["group"])
    if step.get("service"):
        text += f" / {configuration.service_title(cfg, step['service'])}"
    return text


def headline(report, info):
    total, steps = report["overall_seconds"], report["steps"]
    ok = report["status"] == "ok"
    result = (f"Succeeded in **{seconds(total)}**" if ok else
              f"**FAILED** (exit {report.get('returncode')}) after **{seconds(total)}**")
    lines = [f"**Result:** {result}  "]
    slow = info["slowest"]
    if slow:
        lines.append(f"**Slowest step:** {cell(title(slow))}, {seconds(slow['seconds'])}"
                     + share(slow["seconds"], total, " ({} of the total)") + "  ")
    if info["temperature"] == "cold":
        lines.append("**Run type:** cold, no step was served from cache  ")
    elif info["temperature"] != "unknown":
        lines.append(f"**Run type:** {info['temperature']}, {info['skipped']} of "
                     f"{info['total_steps']} steps cached or already running  ")
    counts = info["counts"]
    parts = [f"{counts[k]} {k}" for k in ("ok", "failed", "cached", "reused", "unavailable") if counts.get(k)]
    if steps:
        lines.append(f"**Steps:** {', '.join(parts)}  ")
    started = datetime.fromisoformat(report["started_at"].replace("Z", "+00:00"))
    lines.append(f"**Started:** {started.astimezone(timezone.utc):%d %b %Y, %H:%M:%S} UTC")
    return lines


def nothing_measured(report):
    """One-line reason there is nothing to show as idle, or '' when idle time is meaningful."""
    steps = report["steps"]
    if not steps:
        return "No steps were timed; wrap work with `timed GROUP LABEL command` to see where time goes."
    if all(s["status"] in SKIPPED for s in steps):
        return "All steps were cached: nothing to measure."
    return ""


def timeline_table(report, cfg):
    total = report["overall_seconds"]
    show_bar = total and total > 0
    rows = ["| Start | Step | Section | Time | Status | Share of run |", "|---:|---|---|---:|---|---|"]
    for depth, step in timeline(report["steps"]):
        name = ("  " * depth + ("↳ " if depth else "") + cell(title(step))
                + (" †" if step["background"] else ""))
        graphic = ""
        if show_bar and step["seconds"] is not None and step["status"] not in SKIPPED:
            graphic = f"`{bar(step['offset'], step['seconds'], total)}` {percent(step['seconds'], total)}"
        rows.append(f"| {offset(step['offset'])} | {name} | {cell(section(cfg, step))} | {duration(step)} "
                    f"| {status_text(step)} | {graphic} |")
    idle = report.get("idle_seconds")
    if idle is not None and not nothing_measured(report):
        rows.append(f"| | *Unmeasured / idle* | | {seconds(idle)} | | {percent(idle, total)} |")
    rows.append(f"| | **Total** | | **{seconds(total)}** | | |")
    return rows


def explain(report, cfg):
    lines = ["", "## Details", "",
             "| Section | Steps | Sum of step times | Wall time (overlaps merged) | Cached | Unavailable |",
             "|---|---:|---:|---:|---:|---:|"]
    for group, item in report.get("groups", {}).items():
        all_cached = item["summed_seconds"] is None and not item["unavailable"] and item["cached"] == item["steps"]
        total_text, wall_text = (("cached", "cached") if all_cached else
                                 (seconds(item["summed_seconds"]), seconds(item["active_wall_seconds"])))
        lines.append(f"| {cell(configuration.group_title(cfg, group))} | {item['steps']} "
                     f"| {total_text} | {wall_text} "
                     f"| {item['cached']} | {item['unavailable']} |")
    lines += ["", f"Parallel overlap counted twice in root step times: {seconds(report.get('overlap_seconds'))}.",
              "Cached steps and steps without an end are never counted as zero seconds; "
              "full timestamps and methods are in `report.json`."]
    services = {s["service"] for s in report["steps"] if s.get("service")}
    described = [(configuration.service_title(cfg, name), cfg["services"][name]["description"])
                 for name in sorted(services) if cfg.get("services", {}).get(name, {}).get("description")]
    if described:
        lines += ["", "Services:", *(f"- **{cell(t)}**: {cell(d)}" for t, d in described)]
    return lines


def gantt(report, cfg):
    lines = ["", "## Gantt", "", "```mermaid", "gantt", "  dateFormat x", "  axisFormat %M:%S"]
    last = None
    for _, step in timeline(report["steps"]):
        if step["seconds"] is None or step["offset"] is None or step["status"] in SKIPPED:
            continue
        name = configuration.group_title(cfg, step["group"]).replace(":", " ")
        if name != last:
            lines.append(f"  section {name}")
            last = name
        label = "".join(c for c in title(step) if c not in ":#;").strip() or "step"
        left = int(step["offset"] * 1000)
        lines.append(f"  {label} :{left}, {left + max(1, int(step['seconds'] * 1000))}")
    return lines + ["```"]


def markdown(report, cfg=None, explain_more=False, with_gantt=False):
    cfg, info, total = cfg or {}, facts(report), report["overall_seconds"]
    lines = [f"# Startup timing report: {cell(report['name'])}", "", *headline(report, info), "",
             "## Timeline", "", *timeline_table(report, cfg), ""]
    notes = ["Indented rows are part of the step above them."]
    if report.get("overlap_seconds", 0) >= 0.1:
        notes.append("Some steps ran in parallel, so their times add up to more than the run.")
    if any(s["background"] for s in report["steps"]):
        notes.append("† read from a service log; it runs in the background and is not counted as idle.")
    notes.append("Unavailable means no duration could be measured; it does not mean zero.")
    lines += [" ".join(notes), ""]
    if nothing_measured(report):
        lines += [nothing_measured(report), ""]
    top = leaders(report)
    lines += ["## Slowest 5" if len(top) >= 5 else f"## Slowest {len(top)}", ""] if top else []
    lines += [f"{i}. {cell(title(s))}: {seconds(s['seconds'])}" + share(s["seconds"], total, " ({})")
              for i, s in enumerate(top, 1)]
    if report["warnings"]:
        lines += ["", "## Notes", "", *(f"- {cell(w)}" for w in report["warnings"])]
    if explain_more:
        lines += explain(report, cfg)
    if with_gantt:
        lines += gantt(report, cfg)
    return "\n".join(lines) + "\n"


def previous_run(directory):
    """The run directory just before this one in the same report root, or None."""
    try:
        older = sorted(p for p in directory.parent.iterdir()
                       if p.is_dir() and not p.is_symlink() and p.name < directory.name
                       and (p / "report.json").is_file())
    except OSError:
        return None
    return older[-1] if older else None


def console(report, path):
    """About ten lines for the end of a run."""
    info, total = facts(report), report["overall_seconds"]
    ok = report["status"] == "ok"
    lines = ["", f"Startup timing: {'succeeded' if ok else 'FAILED (exit %s)' % report.get('returncode')}"
             f" in {seconds(total)}"
             + (f" ({info['temperature']}: {info['skipped']}/{info['total_steps']} steps cached)"
                if info["temperature"] not in ("unknown", "cold") else " (cold)" if info["total_steps"] else "")]
    for step in leaders(report, 3):
        lines.append(f"  {seconds(step['seconds']):>10}  {percent(step['seconds'], total):>4}  {title(step)}")
    if report.get("overlap_seconds", 0) >= 0.1:
        lines.append("  (steps ran in parallel, so shares can add up to more than 100%)")
    if nothing_measured(report):
        lines.append(f"  {nothing_measured(report)}")
    elif report.get("idle_seconds") is not None:
        lines.append(f"  {seconds(report['idle_seconds']):>10}  {percent(report['idle_seconds'], total):>4}  unmeasured / idle")
    unavailable = info["counts"].get("unavailable", 0)
    if unavailable:
        lines.append(f"  {unavailable} step(s) unavailable (no end recorded)")
    lines.append(f"Report: {path}")
    previous = previous_run(Path(path).parent)
    if previous:
        script = Path(__file__).resolve().parents[1] / "timing.sh"
        lines.append(f"Compare with the previous run: {script} compare {previous} {Path(path).parent}")
    return "\n".join(lines)
