# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Per-service view of a report: who was busy, when, and on what."""
from . import config as configuration
from .report import SKIPPED, measured, union_seconds

SHARED = "Shared setup"
STRIP = 24


def _short(label, row):
    """Drop a leading 'Chat Q&A: ' style prefix; the row already names the service."""
    head, sep, rest = label.partition(": ")
    names = {row["name"].lower(), (row["service"] or "").lower(), row["name"].split(" / ")[0].lower()}
    return rest if sep and rest and head.lower() in names else label


def analyse(report, cfg):
    """One row per service, busiest first. Steps without a service are grouped as shared."""
    steps = report["steps"]
    parents = {s["parent_id"] for s in steps}
    rows = {}
    for step in steps:
        key = step.get("service")
        row = rows.setdefault(key, {"name": configuration.service_title(cfg, key) if key else SHARED,
                                    "service": key, "spans": [], "parts": [], "cached": 0,
                                    "failed": 0, "unavailable": 0, "description": ""})
        row["description"] = cfg.get("services", {}).get(key, {}).get("description", "") if key else ""
        row["cached"] += step["status"] in SKIPPED
        row["failed"] += step["status"] == "failed"
        row["unavailable"] += step["seconds"] is None and step["status"] not in SKIPPED
    for step in measured(steps):
        if step["offset"] is None:
            continue
        row = rows[step.get("service")]
        row["spans"].append((step["offset"], step["offset"] + step["seconds"]))
        if step["id"] not in parents:  # a parent would only repeat its own parts
            row["parts"].append(step)
    for row in rows.values():
        row["busy"] = union_seconds(row["spans"]) if row["spans"] else None
        row["parts"].sort(key=lambda s: s["seconds"], reverse=True)
        # A step that spans the whole service ("start to ready") says nothing about where time went.
        detail = [p for p in row["parts"] if p["seconds"] < 0.9 * (row["busy"] or 0)]
        row["parts"] = detail or row["parts"]
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


def overlap(first, second):
    """Seconds during which both services had a step running."""
    if not first["spans"] or not second["spans"]:
        return 0.0
    both = union_seconds(first["spans"] + second["spans"])
    return max(0.0, union_seconds(first["spans"]) + union_seconds(second["spans"]) - both)


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
            "bottleneck": top, "services": services}
