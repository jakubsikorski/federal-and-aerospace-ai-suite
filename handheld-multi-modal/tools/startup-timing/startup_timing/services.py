# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Per-service view of a report: who was busy, when, and on what."""
from . import config as configuration
from .report import SKIPPED, measured, union_seconds

SHARED = "Shared setup"
STRIP = 24
MINIMUM_GAP = 0.05  # seconds of a parent step not covered by its children worth showing


def peak(spans):
    """Most steps running at the same moment."""
    moments = sorted([(a, 1) for a, _ in spans] + [(b, -1) for _, b in spans], key=lambda m: (m[0], m[1]))
    now = best = 0
    for _, change in moments:
        now += change
        best = max(best, now)
    return best


def _uncovered(start, end, spans):
    """Parts of [start, end] that no span covers."""
    gaps, at = [], start
    for a, b in sorted(spans):
        if a > at:
            gaps.append((at, min(a, end)))
        at = max(at, b)
        if at >= end:
            break
    if at < end:
        gaps.append((at, end))
    return [(a, b) for a, b in gaps if b - a >= MINIMUM_GAP]


def leaves(steps):
    """Measured steps that explain where time went. A step that has measured children is replaced
    by the parts of its own time that no child covers; one whose children were never measured
    (cached, unfinished) stays as it is."""
    timed = [s for s in measured(steps) if s["offset"] is not None]
    children = {}
    for step in timed:
        children.setdefault(step["parent_id"], []).append(step)
    result = []
    for step in timed:
        inner = children.get(step["id"])
        if not inner:
            result.append(step)
            continue
        spans = [(c["offset"], c["offset"] + c["seconds"]) for c in inner]
        for index, (a, b) in enumerate(_uncovered(step["offset"], step["offset"] + step["seconds"], spans)):
            result.append({**step, "id": f"{step['id']}/rest{index}", "offset": a, "seconds": b - a,
                           "label": step["label"] + " (own time)", "bytes": None})
    return result


def activities(report, cfg):
    """Time by kind of work (the step group), longest first. Parallel steps are counted once."""
    leaves_ = leaves(report["steps"])
    rows = []
    for group in dict.fromkeys(s["group"] for s in leaves_):
        items = sorted((s for s in leaves_ if s["group"] == group), key=lambda s: s["seconds"], reverse=True)
        spans = [(s["offset"], s["offset"] + s["seconds"]) for s in items]
        wall = union_seconds(spans)
        size = sum(s.get("bytes") or 0 for s in items)
        count = peak(spans)
        rows.append({"group": group, "name": configuration.group_title(cfg, group), "wall": wall,
                     "steps": items, "bytes": size, "peak": count,
                     "batch": len(items) >= 3 and count >= 3,
                     "rate": size / wall if size and wall >= 1 else None})
    return sorted(rows, key=lambda r: -r["wall"])


def batch_members(report, cfg):
    """Ids of the steps that really ran side by side in a parallel batch. A later step of the same
    group that overlaps no other (a single pull, say) is ordinary work of its service."""
    members = set()
    for row in activities(report, cfg):
        if not row["batch"]:
            continue
        for step in row["steps"]:
            a, b = step["offset"], step["offset"] + step["seconds"]
            if any(o is not step and o["offset"] < b and o["offset"] + o["seconds"] > a for o in row["steps"]):
                members.add(step["id"])
    return members


def _short(label, row):
    """Drop a leading 'Chat Q&A: ' style prefix; the row already names the service."""
    head, sep, rest = label.partition(": ")
    names = {row["name"].lower(), (row["service"] or "").lower(), row["name"].split(" / ")[0].lower()}
    return rest if sep and rest and head.lower() in names else label


def analyse(report, cfg):
    """One row per service, busiest first. Steps without a service are grouped as shared."""
    steps = report["steps"]
    batched = batch_members(report, cfg)
    rows = {}
    for step in steps:
        key = step.get("service")
        row = rows.setdefault(key, {"name": configuration.service_title(cfg, key) if key else SHARED,
                                    "service": key, "spans": [], "parts": [], "cached": 0,
                                    "failed": 0, "unavailable": 0, "description": "", "batched": False})
        row["description"] = cfg.get("services", {}).get(key, {}).get("description", "") if key else ""
        row["cached"] += step["status"] in SKIPPED
        row["failed"] += step["status"] == "failed"
        row["unavailable"] += step["seconds"] is None and step["status"] not in SKIPPED
    for step in measured(steps):
        if step["offset"] is None:
            continue
        if step["id"] in batched:  # parallel batches share a resource: counted once under activities
            rows[step.get("service")]["batched"] = True
            continue
        row = rows[step.get("service")]
        row["spans"].append((step["offset"], step["offset"] + step["seconds"]))
    for step in leaves(steps):  # a parent only shows the time its children do not explain
        if step["id"] not in batched:
            rows[step.get("service")]["parts"].append(step)
    for row in rows.values():
        row["busy"] = union_seconds(row["spans"]) if row["spans"] else None
        row["parts"].sort(key=lambda s: s["seconds"], reverse=True)
        row["parts"] = [{**p, "label": _short(p["label"], row)} for p in row["parts"]]
        row["first"] = min((a for a, _ in row["spans"]), default=None)
        row["last"] = max((b for _, b in row["spans"]), default=None)
    # Shared setup goes last: it is the glue, not a service the reader can act on.
    return sorted(rows.values(), key=lambda r: (r["service"] is None, -(r["busy"] or 0)))


def strip(row, total):
    """When in the run the service was busy: filled cells are mostly covered by its steps."""
    if not total or not row["spans"]:
        return ""
    cells = []
    for index in range(STRIP):
        left, right = total * index / STRIP, total * (index + 1) / STRIP
        covered = union_seconds([(max(a, left), min(b, right)) for a, b in row["spans"] if a < right and b > left])
        cells.append("█" if covered >= (right - left) / 2 else "░")
    return "".join(cells)


def parallel(rows, total):
    """True when the services' busy times add up to clearly more than the run."""
    return bool(total) and sum(r["busy"] or 0 for r in rows if r["service"]) > total * 1.05


def summary(report, cfg, limit=5):
    """Small stable JSON for other tools. Keys are documented in the README; do not rename them."""
    from .report import facts
    total, rows = report["overall_seconds"], analyse(report, cfg)

    def part(step):
        return {"label": step["label"], "seconds": round(step["seconds"], 2)}

    services = [{"id": r["service"], "name": r["name"], "busy_seconds": round(r["busy"], 2),
                 "share": round(r["busy"] / total, 4) if total else None,
                 "first_seconds": round(r["first"], 2), "last_seconds": round(r["last"], 2),
                 "cached_steps": r["cached"], "failed_steps": r["failed"],
                 "work": [part(p) for p in r["parts"][:limit]]}
                for r in rows if r["busy"]]
    top = next((s for s in services if s["id"]), None)
    return {"schema": "startup-timing-summary/1", "name": report["name"], "status": report["status"],
            "returncode": report.get("returncode"), "started_at": report["started_at"],
            "total_seconds": round(total, 2), "run_type": facts(report)["temperature"],
            "idle_seconds": round(report["idle_seconds"], 2) if report.get("idle_seconds") is not None else None,
            "bottleneck": top, "services": services,
            "activities": [{"group": a["group"], "name": a["name"], "wall_seconds": round(a["wall"], 2),
                            "share": round(a["wall"] / total, 4) if total else None,
                            "steps": len(a["steps"]), "bytes": a["bytes"] or None,
                            "parallel": a["batch"]} for a in activities(report, cfg)]}
