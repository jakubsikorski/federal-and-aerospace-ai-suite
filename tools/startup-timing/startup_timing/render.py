# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Markdown and console views of a normalised report."""
from datetime import datetime, timezone
from pathlib import Path

from . import config as configuration
from .report import SKIPPED, facts, timeline
from .services import activities, analyse, parallel, strip

BAR = 20


def seconds(value):
    """Short human duration: 0.4 s, 45 s, 18m 32s, 1h 02m."""
    if value is None:
        return "unavailable"
    if value < 0.05:
        return "<0.1 s"
    if value < 9.95:
        return f"{value:.1f} s"
    whole = int(round(value))
    if whole < 60:
        return f"{whole} s"
    minutes, rest = divmod(whole, 60)
    if minutes < 60:
        return f"{minutes}m {rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


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


def work(part):
    return f"{cell(title(part))} ({seconds(part['seconds'])})"


def activity_detail(act):
    """One short phrase: what the time went on."""
    items = act["steps"]
    if act["bytes"] and len(items) > 1:
        big = sorted(items, key=lambda s: s.get("bytes") or 0, reverse=True)[:3]
        rate = f" at about {size(act['rate'])}/s" if act["rate"] else ""
        text = f"{len(items)} downloads, {size(act['bytes'])}{rate}"
        return text + "; largest: " + ", ".join(cell(title(s)) for s in big)
    if act["batch"]:
        return f"{len(items)} steps in parallel (up to {act['peak']} at once); longest: {work(items[0])}"
    if len(items) == 1:
        return work(items[0])
    return f"{len(items)} steps; longest: {work(items[0])}"


def verdict(report, rows, total, cfg):
    """What took longest, in one to three plain sentences."""
    acts = activities(report, cfg)
    lines = []
    if acts and total:
        top = acts[0]
        lines.append(f"**What took longest:** {cell(top['name'])}, {seconds(top['wall'])}"
                     f"{share(top['wall'], total, ' ({} of the run)')}. {activity_detail(top)}.")
        later = [a for a in acts[1:] if not all(s["background"] for s in a["steps"])]  # not log umbrellas
        if later and later[0]["wall"] >= 0.1 * total:
            nxt = later[0]
            lines.append(f"**Then:** {cell(nxt['name'])}, {seconds(nxt['wall'])}"
                         f"{share(nxt['wall'], total, ' ({} of the run)')}. {activity_detail(nxt)}.")
    named = [r for r in rows if r["service"] and r["busy"]]
    if named and total:
        top = named[0]
        lines.append(f"**Busiest service:** {cell(top['name'])}, {seconds(top['busy'])}"
                     f"{share(top['busy'], total, ' ({} of the run)')}"
                     + (f". Mainly: {work(top['parts'][0])}." if top["parts"] else "."))
    return lines


def headline(report, info, rows, cfg):
    total = report["overall_seconds"]
    ok = report["status"] == "ok"
    started = datetime.fromisoformat(report["started_at"].replace("Z", "+00:00"))
    kind = {"unknown": "", "cold": " · cold start (nothing cached)",
            }.get(info["temperature"], f" · {info['temperature']} ({info['skipped']} of {info['total_steps']} steps cached)")
    result = (f"**Succeeded in {seconds(total)}**" if ok else
              f"**FAILED (exit {report.get('returncode')}) after {seconds(total)}**")
    lines = [f"{result}{kind} · {started.astimezone(timezone.utc):%d %b %Y %H:%M} UTC", ""]
    failed = [s for s in report["steps"] if s["status"] == "failed"]
    if failed:
        lines += ["**Failed step:** " + "; ".join(cell(title(s)) for s in failed[:3]), ""]
    lines += [f"> {line}  " for line in verdict(report, rows, total, cfg)]
    return lines


def services_table(rows, total):
    out = ["| Service | Time | Share | Biggest cost |", "|---|---:|---:|---|"]
    for row in (r for r in rows if r["busy"] is not None or r["failed"]):
        mark = " ⚠ failed" if row["failed"] else ""
        out.append(f"| {cell(row['name'])}{mark} | {'-' if row['busy'] is None else seconds(row['busy'])} "
                   f"| {percent(row['busy'], total) or '-'} "
                   f"| {work(row['parts'][0]) if row['parts'] else '-'} |")
    idle = [r["name"] for r in rows if r["busy"] is None and not r["failed"] and not r["batched"]]
    if idle:
        out += ["", "Cached, already running or not started: " + ", ".join(cell(n) for n in idle) + "."]
    return out


def activity_table(acts, total, limit=8):
    out = ["| Activity | Time | Share | What |", "|---|---:|---:|---|"]
    for act in acts[:limit]:
        out.append(f"| {cell(act['name'])} | {seconds(act['wall'])} | {percent(act['wall'], total)} "
                   f"| {activity_detail(act)} |")
    if len(acts) > limit:
        out.append(f"| {len(acts) - limit} smaller activities | {seconds(sum(a['wall'] for a in acts[limit:]))} | | |")
    return out


def chart(rows, total):
    """When each service was busy, one text bar per service."""
    rows = [r for r in rows if r["busy"]]
    if not rows or not total:
        return []
    width = min(26, max(len(r["name"]) for r in rows))
    lines = ["```text"]
    for row in rows:
        lines.append(f"{row['name'][:width]:<{width}}  {strip(row, total)}  {seconds(row['busy'])}")
    ruler = "0".ljust(24 - len(seconds(total))) + seconds(total)
    lines += [f"{'':<{width}}  {ruler}", "```"]
    return lines


def details(rows, limit=5):
    """What each service spent its time on, as short lists."""
    out = []
    for row in rows:
        if not row["busy"]:
            continue
        out += ["", f"**{cell(row['name'])}** · {seconds(row['busy'])}", ""]
        shown = row["parts"][:limit]
        out += [f"- {seconds(p['seconds'])} · {cell(title(p))}" for p in shown]
        rest = row["parts"][limit:]
        if rest:
            out.append(f"- {seconds(sum(p['seconds'] for p in rest))} · {len(rest)} smaller step{'s' if len(rest) > 1 else ''}")
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


def brief(report, cfg=None):
    """The part worth pasting into a cycle report: result, what took longest, one table."""
    cfg, info, total = cfg or {}, facts(report), report["overall_seconds"]
    rows = analyse(report, cfg)
    lines = [f"# Startup timing: {cell(report['name'])}", "", *headline(report, info, rows, cfg)]
    while lines[-1] == "":
        lines.pop()
    lines.append("")
    if nothing_measured(report):
        return lines + [nothing_measured(report), ""]
    acts = activities(report, cfg)
    lines += ["## Time by activity", "", *activity_table(acts, total), "",
              "Time during which at least one step of that kind was running."
              + (" Parallel steps are counted once." if any(x["peak"] > 1 for x in acts) else ""), "",
              "## Time by service", "", *services_table(rows, total), ""]
    note = []
    batches = [a["name"] for a in acts if a["batch"]]
    if batches:
        note.append("Not counted per service: parallel " + ", ".join(n.lower() for n in batches)
                    + " (see Time by activity).")
    if parallel(rows, total):
        note.append("Services run in parallel, so shares add up to more than 100%.")
    idle = report.get("idle_seconds")
    if idle and total and idle / total >= 0.05:
        note.append(f"{seconds(idle)} ({percent(idle, total)}) had no timed step running: untimed work or waits.")
    return lines + ([" ".join(note), ""] if note else [])


def markdown(report, cfg=None, explain_more=False, with_gantt=False):
    cfg, total = cfg or {}, report["overall_seconds"]
    rows = analyse(report, cfg)
    lines = brief(report, cfg)
    if not nothing_measured(report):
        lines += ["## When each service was busy", "", *chart(rows, total), "",
                  "## What each service spent its time on", *details(rows), ""]
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
    for act in activities(report, cfg or {})[:3]:
        lines.append(f"  {act['name']}: {seconds(act['wall'])} ({percent(act['wall'], total)})"
                     + (f", {size(act['bytes'])}" if act["bytes"] else ""))
    lines.append("  By service:")
    width = max([len(r["name"]) for r in rows[:3]] + [1])
    for row in rows[:3]:
        main = f"  {cell(title(row['parts'][0]))} {seconds(row['parts'][0]['seconds'])}" if row["parts"] else ""
        lines.append(f"  {row['name']:<{width}}  {seconds(row['busy']):>9}  {percent(row['busy'], total):>4}{main}")
    if parallel(rows, total):
        lines.append("  (shares can add up to more than 100%: steps ran in parallel)")
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
