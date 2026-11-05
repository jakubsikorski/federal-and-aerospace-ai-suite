# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
import numpy as np
import pytest

pytest.importorskip("trackeval")

from app.hota import FrameClock, LiveHota, SlidingHota, hota_for_frames, similarity  # noqa: E402

N = 400


def make_gt():
    """Two people walking in straight lines (ids 0 and 1), one record per frame."""
    gt = []
    for f in range(N):
        xyz = np.array([[1.0 + 0.02 * f, 5.0, 0.0], [8.0 - 0.015 * f, 9.0, 0.0]])
        gt.append((np.array([0, 1]), xyz))
    return gt


def feed_perfect(sliding, gt, frames, ids=("a", "b"), noise=0.0):
    for f in frames:
        sliding.add_update(f, list(ids), gt[f][1] + noise)


def test_similarity_is_one_at_zero_and_zero_at_two_metres():
    sim = similarity(np.array([[0.0, 0.0, 0.0]]), np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]]))
    assert sim[0].tolist() == [1.0, 0.5, 0.0]


def test_clock_maps_to_latest_frame_at_or_before_lag():
    c = FrameClock(lag_s=0.1)
    for i in range(10):
        c.add_camera(100.0 + i * 0.033, i)
    # t - lag = 100.1; frames are at 100.000, .033, .066, .099 -> the latest one at or before is frame 3
    assert c.frame_at(100.0 + 0.2) == 3
    assert c.frame_at(100.0 + 0.1) == 0  # t - lag = 100.0 -> only the first message qualifies
    assert c.frame_at(99.0) is None


def test_perfect_tracking_scores_high_and_missing_frames_score_lower():
    gt = make_gt()
    good = SlidingHota(gt, window_s=5)
    feed_perfect(good, gt, range(0, 200))
    s_good = good.score()
    assert s_good["HOTA"] > 0.9 and s_good["covered"] == 1.0

    gappy = SlidingHota(gt, window_s=5)
    feed_perfect(gappy, gt, [f for f in range(0, 200) if (f // 20) % 2 == 0])  # half of the time no updates
    s_gappy = gappy.score()
    assert s_gappy["covered"] < 0.7 and s_gappy["HOTA"] < s_good["HOTA"] - 0.2


def test_id_switches_lower_association_not_detection():
    gt = make_gt()
    s = SlidingHota(gt, window_s=5)
    for f in range(0, 200):
        ids = ("a", "b") if f < 100 else ("c", "d")  # every track gets a new id half way through
        s.add_update(f, list(ids), gt[f][1])
    r = s.score()
    assert r["DetA"] > 0.9 and r["AssA"] < r["DetA"] - 0.1


def test_window_never_crosses_the_video_loop():
    gt = make_gt()
    s = SlidingHota(gt, window_s=5)
    feed_perfect(s, gt, range(300, 399))
    feed_perfect(s, gt, range(0, 30))   # the video looped
    assert s.window()[0] == 0           # window starts at the new run
    assert s.score() is None            # too few frames (< min_frames) to score yet
    feed_perfect(s, gt, range(30, 90))
    assert s.score() is not None


def test_live_score_falls_when_updates_stop_but_the_clock_runs():
    gt = make_gt()
    live = LiveHota(gt, window_s=5, lag_s=0.0)
    t0 = 1000.0
    for f in range(0, 200):
        live.on_camera(t0 + f / 30, f)
        live.on_scene(t0 + f / 30, ["a", "b"], gt[f][1])
    healthy = live.score(now=t0 + 199 / 30)["HOTA"]
    stalled = live.score(now=t0 + 199 / 30 + 3.0)  # three seconds with no camera and no tracker message
    assert stalled is not None and stalled["HOTA"] < healthy - 0.3 and stalled["covered"] < 0.7


def test_hota_for_frames_handles_empty_tracker():
    gt = make_gt()
    r = hota_for_frames(gt, {}, list(range(0, 50)))
    assert r["HOTA"] == 0.0
