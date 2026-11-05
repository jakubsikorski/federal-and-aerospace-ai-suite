# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Live, sliding-window HOTA of SceneScape's tracked output against the ground truth.

The per-timestep matching and the HOTA maths are TrackEval's (the library SceneScape's own offline
evaluation uses). The similarity follows that evaluation too: Euclidean distance between 3D points,
sim = max(0, 1 - d / zero_distance) with zero_distance = 2.0 m (0.5 similarity at 1 m).

Each ground-truth frame in the window gets the latest tracker update mapped to a frame at or before
it, held for at most hold_frames. Frames with no fresh update count as "tracker saw nothing", so a
stalled or congested pipeline lowers the score - that is the point of the demo.
"""

import json
import math
from collections import deque

import numpy as np

try:
    from trackeval.metrics import HOTA
except ImportError:  # the unit tests for the plumbing do not need TrackEval
    HOTA = None

ZERO_DISTANCE_M = 2.0


def load_ground_truth(path):
    """gtLoc.json (one JSON line per frame) -> list of (ids, xyz[N,3]) for persons, indexed by frame."""
    frames = []
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            objects = json.loads(line)["objects"]
            people = [p for cat, items in objects.items() if cat.lower() == "person" for p in items]
            ids = np.array([int(p["id"]) for p in people], dtype=int)
            xyz = np.array([p["translation"][:3] for p in people], dtype=float).reshape(-1, 3)
            frames.append((ids, xyz))
    return frames


def similarity(gt_xyz, tr_xyz, zero_distance=ZERO_DISTANCE_M):
    """NxM similarity matrix, 1 at distance 0 and 0 at zero_distance or beyond."""
    if len(gt_xyz) == 0 or len(tr_xyz) == 0:
        return np.zeros((len(gt_xyz), len(tr_xyz)))
    d = np.linalg.norm(gt_xyz[:, None, :] - tr_xyz[None, :, :], axis=2)
    return np.maximum(0.0, 1.0 - d / zero_distance)


def hota_for_frames(gt, tracked, frames):
    """HOTA over an ordered list of GT frame numbers.

    gt:      list indexed by frame -> (ids[N], xyz[N,3])
    tracked: dict frame -> (ids[M], xyz[M,3]) for frames that have a (held) tracker update
    Returns {'HOTA','DetA','AssA'} (means over TrackEval's alpha thresholds) or None if nothing to score.
    """
    if HOTA is None:
        raise RuntimeError("trackeval is not installed")
    gt_ids, tr_ids, sims = [], [], []
    tr_map, gt_map, n_gt_dets, n_tr_dets = {}, {}, 0, 0
    for f in frames:
        g_ids, g_xyz = gt[f]
        t_ids, t_xyz = tracked.get(f, (np.array([], dtype=object), np.zeros((0, 3))))
        # TrackEval wants ids 0..n-1 within the sequence (the tracker ids are UUIDs)
        t_int = np.array([tr_map.setdefault(t, len(tr_map)) for t in t_ids], dtype=int)
        g_int = np.array([gt_map.setdefault(int(g), len(gt_map)) for g in g_ids], dtype=int)
        gt_ids.append(g_int)
        tr_ids.append(t_int)
        sims.append(similarity(g_xyz, t_xyz))
        n_gt_dets += len(g_int)
        n_tr_dets += len(t_int)
    if n_gt_dets == 0:
        return None
    data = {
        "num_gt_ids": len(gt_map),
        "num_tracker_ids": len(tr_map),
        "num_gt_dets": n_gt_dets,
        "num_tracker_dets": n_tr_dets,
        "gt_ids": gt_ids,
        "tracker_ids": tr_ids,
        "similarity_scores": sims,
    }
    res = HOTA().eval_sequence(data)
    return {k: float(np.mean(res[k])) for k in ("HOTA", "DetA", "AssA")}


class FrameClock:
    """Maps a tracker update (wall-clock timestamp) to a ground-truth frame number.

    The camera messages carry the frame number (from the SEI data) and the time they were produced
    on the same host; the tracker update is matched to the latest camera message at least
    lag_s older. The position error against the ground truth is flat for lags of 0-200 ms.
    """

    def __init__(self, lag_s=0.08, keep_s=30.0):
        self.lag_s, self.keep_s = lag_s, keep_s
        self._t = deque()
        self._frame = deque()

    def add_camera(self, t, frame):
        if self._t and t < self._t[-1]:
            return  # keep the series ordered (two cameras interleave within a few ms)
        self._t.append(t)
        self._frame.append(frame)
        while self._t and self._t[0] < t - self.keep_s:
            self._t.popleft()
            self._frame.popleft()

    def frame_at(self, t):
        """Frame number of the latest camera message at or before t - lag, or None."""
        target = t - self.lag_s
        lo, hi = 0, len(self._t)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._t[mid] <= target:
                lo = mid + 1
            else:
                hi = mid
        return self._frame[lo - 1] if lo else None


class SlidingHota:
    """Keeps the recent tracker updates per GT frame and scores the last window_s seconds."""

    def __init__(self, gt, fps=30, window_s=10.0, hold_frames=4, min_frames=60):
        self.gt, self.fps = gt, fps
        self.window_frames = int(window_s * fps)
        self.hold_frames = hold_frames
        self.min_frames = min_frames
        self.n_frames = len(gt)
        self._updates = {}  # frame -> (ids, xyz); the latest update wins
        self._latest = None  # frame number of the newest update
        self._run_start = None  # first frame of the current pass through the video

    def add_update(self, frame, ids, xyz):
        """Record a tracker update mapped to a GT frame. A frame number that jumps backwards means
        the video looped: start a new run so the window never spans the jump."""
        if frame is None or not 0 <= frame < self.n_frames:
            return
        if self._latest is not None and frame < self._latest - self.fps:  # jumped back by > 1 s
            self._updates.clear()
            self._run_start = frame
        if self._run_start is None:
            self._run_start = frame
        self._updates[frame] = (np.array(ids, dtype=object), np.array(xyz, dtype=float).reshape(-1, 3))
        self._latest = frame

    def window(self, end_frame=None):
        """Frames of the current window (never before the start of the current run).

        end_frame lets the window keep moving with the clock while no updates arrive."""
        if self._latest is None:
            return []
        end = self._latest if end_frame is None else min(max(end_frame, self._latest), self.n_frames - 1)
        start = max(self._run_start, end - self.window_frames + 1)
        return list(range(start, end + 1))

    def score(self, end_frame=None):
        frames = self.window(end_frame)
        if len(frames) < self.min_frames:
            return None
        held, last_age = {}, None
        last = None
        for f in frames:
            if f in self._updates:
                last, last_age = self._updates[f], 0
            elif last is not None:
                last_age += 1
            held[f] = last if last is not None and last_age <= self.hold_frames else None
        tracked = {f: v for f, v in held.items() if v is not None}
        result = hota_for_frames(self.gt, tracked, frames)
        if result is not None:
            result["frames"] = len(frames)
            result["covered"] = round(len(tracked) / len(frames), 3)  # share of frames with a fresh update
        return result


class LiveHota:
    """FrameClock + SlidingHota fed from the two MQTT streams; score() is safe to call every second."""

    def __init__(self, gt, window_s=10.0, lag_s=0.08, hold_frames=4, fps=30):
        self.fps = fps
        self.clock = FrameClock(lag_s)
        self.sliding = SlidingHota(gt, fps=fps, window_s=window_s, hold_frames=hold_frames)
        self._last_cam = None  # (timestamp, frame) of the newest camera message

    def on_camera(self, ts, frame):
        self.clock.add_camera(ts, frame)
        if self._last_cam is None or ts >= self._last_cam[0]:
            self._last_cam = (ts, frame)

    def on_scene(self, ts, ids, xyz):
        self.sliding.add_update(self.clock.frame_at(ts), ids, xyz)

    def score(self, now):
        """Score of the last window ending 'now': frames without a fresh update count as missed, so a
        stalled pipeline drives the score down instead of freezing it at the last good value."""
        end = None
        if self._last_cam is not None:
            ts, frame = self._last_cam
            end = frame + max(0, int((now - ts) * self.fps))
        return self.sliding.score(end)
