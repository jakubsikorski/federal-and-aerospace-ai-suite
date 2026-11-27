# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Per-step deltas between two reports."""
from .render import cell, seconds
from .report import SKIPPED, facts, load


def keyed(report):
    seen, result = {}, {}
    for step in report["steps"]:
        base = (step["group"], step["label"], step.get("service") or "")
        seen[base] = seen.get(base, 0) + 1
        result[base + (seen[base],)] = step
    return result


def value(step):
    if step is None:
        return "-"
    return "cached" if step["status"] in SKIPPED else seconds(step["seconds"])


def delta(old, new):
    if old is not None and new is not None:
        if new["status"] in SKIPPED and old["status"] not in SKIPPED:
            return None, "now cached"
        if old["status"] in SKIPPED and new["status"] not in SKIPPED:
            return None, "was cached"
    if old is None or new is None or old["seconds"] is None or new["seconds"] is None \
            or old["status"] in SKIPPED or new["status"] in SKIPPED:
        return None, "n/a"
    change = new["seconds"] - old["seconds"]
    if abs(change) < 0.05:
        return change, "no change"
    pct = f" ({change / old['seconds'] * 100:+.0f}%)" if old["seconds"] >= 0.5 else ""
    return change, f"{'+' if change >= 0 else '-'}{seconds(abs(change))}{pct}"


def compare(path_a, path_b):
    first, second = load(path_a), load(path_b)
    left, right = keyed(first), keyed(second)
    keys = list(left) + [k for k in right if k not in left]
    rows = []
    for key in keys:
        change, text = delta(left.get(key), right.get(key))
        rows.append((-abs(change) if change is not None else 0, key, left.get(key), right.get(key), text))
    rows.sort(key=lambda row: (row[0], *map(str, row[1][:3]), row[1][3]))
    total, text = delta({"seconds": first["overall_seconds"], "status": "ok"},
                        {"seconds": second["overall_seconds"], "status": "ok"})
    lines = [f"Total: {seconds(first['overall_seconds'])} -> {seconds(second['overall_seconds'])}  {text}",
             f"A: {first['name']} ({first['started_at']})", f"B: {second['name']} ({second['started_at']})", "",
             "| Step | A | B | Change (B - A) |", "|---|---:|---:|---:|"]
    temperatures = (facts(first)["temperature"], facts(second)["temperature"])
    if temperatures[0] != temperatures[1] and "unknown" not in temperatures:
        lines.insert(3, f"Note: A is {temperatures[0]} and B is {temperatures[1]}; compare like with like.")
    for _, key, old, new, text in rows:
        note = " (only in B)" if old is None else " (only in A)" if new is None else ""
        lines.append(f"| {cell(key[1])}{note} | {value(old)} | {value(new)} | {text} |")
    return "\n".join(lines)
