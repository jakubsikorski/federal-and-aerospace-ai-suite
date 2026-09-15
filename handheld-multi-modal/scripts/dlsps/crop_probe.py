#!/usr/bin/env python3
"""Measurement-only gvapython callback: bounded samples and stage timings."""
import weakref
from types import SimpleNamespace
from collections import Counter, OrderedDict, deque
import json
from pathlib import Path
import time
import uuid

from crop_candidates import atomic_write, bgr_frame, crop_rect, observations


class CropProbe:
    def __init__(self, output_dir="/tmp/dlsps-crop-probe", sample_every_frames=30,
                 max_samples=20, max_tracks=2048, track_ttl_seconds=10,
                 max_frame_pixels=3840*2160, report_seconds=5):
        if (sample_every_frames < 1 or max_samples < 0 or max_tracks < 1
                or track_ttl_seconds <= 0 or max_frame_pixels < 1 or report_seconds <= 0):
            raise ValueError("invalid probe limits")
        self.root = Path(output_dir) / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.every, self.limit, self.capacity = sample_every_frames, max_samples, max_tracks
        self.ttl, self.max_pixels, self.report_seconds = track_ttl_seconds, max_frame_pixels, report_seconds
        self.tracks = OrderedDict()
        self.sample_attempts = {}  # At most max_samples entries; no per-frame growth.
        self.stats = Counter()
        self.timings = {key: deque(maxlen=2048) for key in ("callback_ms", "map_convert_ms", "copy_ms", "jpeg_ms", "write_ms")}
        self.started = self.last_report = time.monotonic()
        shared = SimpleNamespace(**{key: getattr(self, key) for key in
            ("root", "stats", "started", "timings")})
        self.finalizer = weakref.finalize(self, CropProbe.report, shared)

    def report(self):
        elapsed = max(1e-6, time.monotonic() - self.started)
        times = {}
        for key, samples in self.timings.items():
            ordered = sorted(samples)
            if ordered:
                times[key] = {"n": len(ordered), "mean": sum(ordered)/len(ordered),
                              "p95": ordered[min(len(ordered)-1, int(len(ordered)*0.95))], "max": ordered[-1]}
        report = {"probe": dict(self.stats), "elapsed_seconds": elapsed,
                  "callback_fps": self.stats["frames"]/elapsed,
                  "new_track_entries_per_second": self.stats["new_track_entries"]/elapsed,
                  "timings": times, "output_dir": str(self.root)}
        print(json.dumps(report), flush=True)
        atomic_write(self.root / "summary.json", json.dumps(report, indent=2).encode())

    def process_frame(self, frame):
        start = time.monotonic()
        self.stats["frames"] += 1
        try:
            info = frame.video_info()
            self.stats["format"] = info.finfo.name
            self.stats["width"], self.stats["height"] = info.width, info.height
            while self.tracks and next(iter(self.tracks.values())) < start - self.ttl:
                self.tracks.popitem(last=False)
            candidate = None
            for obs in observations(frame):
                self.stats["observations"] += 1
                tid = obs["tracker_id"]
                if tid is None:
                    self.stats["untracked"] += 1
                else:
                    if tid not in self.tracks:
                        self.stats["new_track_entries"] += 1
                        if len(self.tracks) >= self.capacity:
                            self.tracks.popitem(last=False)
                            self.stats["track_evictions"] += 1
                    self.tracks[tid] = start
                    self.tracks.move_to_end(tid)
                if tid is not None and (candidate is None or
                        self.sample_attempts.get(tid, 0) < self.sample_attempts.get(candidate["tracker_id"], 0)):
                    candidate = obs
            if candidate and self.stats["sample_attempts"] < self.limit and (self.stats["frames"] - 1) % self.every == 0:
                self.stats["sample_attempts"] += 1
                tid = candidate["tracker_id"]
                self.sample_attempts[tid] = self.sample_attempts.get(tid, 0) + 1
                rect, flags = crop_rect(candidate["detection_rect"], info.width, info.height)
                before = time.monotonic()
                with bgr_frame(frame, self.max_pixels) as pixels:
                    mapped = time.monotonic()
                    self.timings["map_convert_ms"].append((mapped-before)*1000)
                    x, y, w, h = rect
                    crop = pixels[y:y+h, x:x+w].copy()
                    self.timings["copy_ms"].append((time.monotonic()-mapped)*1000)
                import cv2
                before = time.monotonic()
                ok, encoded = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if not ok:
                    raise OSError("JPEG failed")
                self.timings["jpeg_ms"].append((time.monotonic()-before)*1000)
                metadata = dict(candidate, crop_rect=rect, **flags, observed_at=time.time(),
                                probe_frame_sequence=self.stats["frames"], frame_width=info.width,
                                frame_height=info.height, format=info.finfo.name)
                metadata["frame_messages"] = [json.loads(m) for m in frame.messages()]
                stem = f"sample-{self.stats['sample_attempts']:04d}"
                before = time.monotonic()
                atomic_write(self.root / (stem + ".jpg"), encoded.tobytes())
                atomic_write(self.root / (stem + ".json"), json.dumps(metadata, indent=2).encode())
                self.timings["write_ms"].append((time.monotonic()-before)*1000)
                self.stats["samples_saved"] += 1
        except Exception as exc:
            self.stats["errors"] += 1
            self.stats["last_error"] = str(exc)[:200]
        finally:
            self.timings["callback_ms"].append((time.monotonic()-start)*1000)
            if start - self.last_report >= self.report_seconds:
                try:
                    self.report()
                except OSError as exc:
                    self.stats["report_errors"] += 1
                    self.stats["last_error"] = str(exc)[:200]
                self.last_report = start
        return True
