# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Markdown and console views of a normalised report."""
from datetime import datetime, timezone
from pathlib import Path

from . import config as configuration
from .report import SKIPPED, facts, timeline
from .services import analyse, parallel, strip

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
    return configuration.service_title(cfg, step["service"]) if step.get("service") \
        else configuration.group_title(cfg, step["group"])


def parts_text(row, count=3):
    return "; ".join(f"{cell(title(p))} {seconds(p['seconds'])}" for p in row["parts"][:count])


def why(rows, total):
    """Plain sentences answering 'what took so long?'."""
    named = [r for r in rows if r["service"] and r["busy"]]
    if not named or not total:
        return []
    top = named[0]
    sentence = (f"**{cell(top['name'])}** was busy longest: {seconds(top['busy'])}"
                f"{share(top['busy'], total, ' ({} of the run)')}")
    if top["parts"]:
        sentence += f", mostly on {parts_text(top, 2)}"
    lines = [sentence + "."]
    if len(named) > 1 and parallel(rows, total):
        second = named[1]
        lines.append(f"**{cell(second['name'])}** ran at the same time for {seconds(second['busy'])}"
                     + (f" ({parts_text(second, 1)})" if second["parts"] else "")
                     + ". Services ran in parallel, so their times add up to more than the run.")
    return lines


def headline(report, info, rows):
    total, steps = report["overall_seconds"], report["steps"]
    ok = report["status"] == "ok"
    result = (f"Succeeded in **{seconds(total)}**" if ok else
              f"**FAILED** (exit {report.get('returncode')}) after **{seconds(total)}**")
    lines = [f"**Result:** {result}"
             + ("" if info["temperature"] == "unknown" else
                f" ({'cold run, nothing cached' if info['temperature'] == 'cold' else info['temperature'] + ': ' + str(info['skipped']) + ' of ' + str(info['total_steps']) + ' steps cached or already running'})"),
             ""]
    lines += [*why(rows, total), ""] if why(rows, total) else []
    started = datetime.fromisoformat(report["started_at"].replace("Z", "+00:00"))
    lines.append(f"Started {started.astimezone(timezone.utc):%d %b %Y, %H:%M:%S} UTC")
    return lines


def services_table(rows, total):
    out = ["| Service | Busy | Of run | When (run start → end) | Main work |", "|---|---:|---:|---|---|"]
    for row in (r for r in rows if r["busy"] is not None or r["failed"]):
        busy = "-" if row["busy"] is None else seconds(row["busy"])
        mark = " ⚠ failed" if row["failed"] else ""
        out.append(f"| {cell(row['name'])}{mark} | {busy} | {percent(row['busy'], total) or '-'} "
                   f"| `{strip(row, total)}` | {parts_text(row) or ('cached' if row['cached'] else '-')} |")
    idle = [r["name"] for r in rows if r["busy"] is None and not r["failed"]]
    if idle:
        out += ["", "Nothing to time (cached, already running or not started): " + ", ".join(cell(n) for n in idle) + "."]
    return out


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
    rows = ["| Start | Step | Service | Time | Status | Position in run |", "|---:|---|---|---:|---|---|"]
    for depth, step in timeline(report["steps"]):
        if step["status"] in SKIPPED:
            continue  # listed in one line below the table
        name = ("&nbsp;&nbsp;" * depth + ("↳ " if depth else "") + cell(title(step))
                + (" †" if step["background"] else ""))
        graphic = ""
        if total and step["seconds"] is not None:
            graphic = f"`{bar(step['offset'], step['seconds'], total)}`"
        rows.append(f"| {offset(step['offset'])} | {name} | {cell(section(cfg, step))} | {duration(step)} "
                    f"| {status_text(step)} | {graphic} |")
    idle = report.get("idle_seconds")
    if idle is not None and not nothing_measured(report):
        rows.append(f"| | *Time with no step running* | | {seconds(idle)} | | |")
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
    rows = analyse(report, cfg)
    lines = [f"# Startup timing report: {cell(report['name'])}", "", *headline(report, info, rows), ""]
    if nothing_measured(report):
        lines += [nothing_measured(report), ""]
    elif rows:
        lines += ["## Time by service", "", *services_table(rows, total), "",
                  "*Busy* is the time the service had a step running; the strip shows when."
                  + (" Services run in parallel, so *Of run* adds up to more than 100%." if parallel(rows, total) else ""),
                  ""]
    skipped = [s for s in report["steps"] if s["status"] in SKIPPED]
    lines += ["<details>", f"<summary>All {len(report['steps']) - len(skipped)} timed steps</summary>", "",
              *timeline_table(report, cfg), ""]
    notes = ["Indented rows are part of the step above them."]
    if any(s["background"] for s in report["steps"]):
        notes.append("† read from a service log; it runs in the background.")
    notes.append("Unavailable means no duration could be measured; it does not mean zero.")
    lines += [" ".join(notes), ""]
    if skipped:
        lines += [f"Cached or already running ({len(skipped)}): " + ", ".join(cell(title(s)) for s in skipped), ""]
    lines += ["</details>"]
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


def console(report, path, cfg=None):
    """About ten lines for the end of a run: which service spent the time, and on what."""
    info, total = facts(report), report["overall_seconds"]
    rows = [r for r in analyse(report, cfg or {}) if r["busy"]]
    ok = report["status"] == "ok"
    lines = ["", f"Startup timing: {'succeeded' if ok else 'FAILED (exit %s)' % report.get('returncode')}"
             f" in {seconds(total)}"
             + (f" ({info['temperature']}: {info['skipped']}/{info['total_steps']} steps cached)"
                if info["temperature"] not in ("unknown", "cold") else " (cold)" if info["total_steps"] else "")]
    width = max([len(r["name"]) for r in rows[:5]] + [1])
    for row in rows[:5]:
        main = f"  {cell(title(row['parts'][0]))} {seconds(row['parts'][0]['seconds'])}" if row["parts"] else ""
        lines.append(f"  {row['name']:<{width}}  {seconds(row['busy']):>9}  {percent(row['busy'], total):>4}{main}")
    if parallel(rows, total):
        lines.append("  (services ran in parallel, so shares can add up to more than 100%)")
    if nothing_measured(report):
        lines.append(f"  {nothing_measured(report)}")
    unavailable = info["counts"].get("unavailable", 0)
    if unavailable:
        lines.append(f"  {unavailable} step(s) unavailable (no end recorded)")
    lines.append(f"Report: {path}")
    previous = previous_run(Path(path).parent)
    if previous:
        script = Path(__file__).resolve().parents[1] / "timing.sh"
        lines.append(f"Compare with the previous run: {script} compare {previous} {Path(path).parent}")
    return "\n".join(lines)
