#!/usr/bin/env python3
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Replay a recording from record_scene.py through the live HOTA engine and print the score over time.

  python3 replay_hota.py recording.jsonl [--lag 0.08] [--window 10] [--hold 4] [--every 5]
Run with the backend's python path, e.g.: python3 src/scene/tools/replay_hota.py ...
"""
import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "dashboard", "backend"))
from app.hota import FrameClock, SlidingHota, load_ground_truth  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("recording")
ap.add_argument("--gt", default=os.path.join(os.path.dirname(__file__), "..", "data", "gtLoc.json"))
ap.add_argument("--lag", type=float, default=0.08)
ap.add_argument("--window", type=float, default=10.0)
ap.add_argument("--hold", type=int, default=4)
ap.add_argument("--every", type=float, default=5.0)
ap.add_argument("--drop-from", type=float, default=None, help="simulate an outage: ignore scene updates between T and T+--drop-len s (seconds into the recording)")
ap.add_argument("--drop-len", type=float, default=5.0)
a = ap.parse_args()


def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


gt = load_ground_truth(a.gt)
clock, hota = FrameClock(a.lag), SlidingHota(gt, window_s=a.window, hold_frames=a.hold)
events = []
for line in open(a.recording):
    r = json.loads(line)
    events.append((r["t"], r["topic"], r["msg"]))
events.sort(key=lambda e: e[0])
t0 = events[0][0]
next_report = t0 + a.every
print(f"{'t(s)':>6} {'frame':>6} {'HOTA':>6} {'DetA':>6} {'AssA':>6} {'covered':>8}")
for t, topic, m in events:
    if "/camera/" in topic:
        clock.add_camera(ts(m["timestamp"]), m["frame"])
    else:
        if a.drop_from is not None and a.drop_from <= t - t0 < a.drop_from + a.drop_len:
            continue
        frame = clock.frame_at(ts(m["timestamp"]))
        hota.add_update(frame, [o["id"] for o in m["objects"]], [o["translation"] for o in m["objects"]])
    if t >= next_report:
        next_report += a.every
        s = hota.score()
        if s:
            print(f"{t - t0:6.0f} {hota._latest:6d} {s['HOTA']:6.3f} {s['DetA']:6.3f} {s['AssA']:6.3f} {s['covered']:8.2f}")
        else:
            print(f"{t - t0:6.0f} {str(hota._latest):>6}  (not enough frames yet)")
