#!/usr/bin/env python3
"""Bounded contextual crop callback for gvapython. See README.md for placement."""
import argparse
from collections import Counter, OrderedDict
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
from functools import wraps
import json
import math
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid
import weakref
from types import SimpleNamespace


@dataclass
class Settings:
    stream_id: str = "cam1"
    output_dir: str = "/tmp/dlsps-crops"
    padding: float = 0.25
    aspect_ratio: float = 4 / 3
    confirm_seconds: float = 0.25
    min_observations: int = 3
    min_confidence: float = 0.4
    min_object_pixels: int = 16
    rate: float = 2.0
    burst: int = 3
    interval_seconds: float = 2.0
    improvement: float = 0.15
    candidates_per_track: int = 3
    track_ttl_seconds: float = 10.0
    max_tracks: int = 2048
    queue_jobs: int = 8
    queue_bytes: int = 32 * 1024 * 1024
    max_frame_pixels: int = 3840 * 2160
    max_dimension: int = 640
    jpeg_quality: int = 85
    store_bytes: int = 256 * 1024 * 1024
    # A rolling window, not a cliff: `save` evicts the oldest candidates to make
    # room rather than refusing new ones. At ~3 candidates per track this is
    # roughly the last 500 tracked objects per camera, which is far more history
    # than a handheld sortie needs, and it bounds two cameras at ~512 MiB.
    store_candidates: int = 1500
    free_disk_bytes: int = 64 * 1024 * 1024
    # Evidence for objects seen this sortie, not a day of archive. The old 24h
    # default meant nothing was ever expired during a demo, so every pipeline
    # restart layered another generation of crops on top of the last: 3985 files
    # and 105 MiB on one camera, measured 2026-09-14, heading for the cap.
    retention_seconds: float = 3600.0
    report_seconds: float = 5.0
    # A store whose status file has not been touched for this long belongs to a
    # dead producer and may be taken over. Must stay well above report_seconds.
    takeover_seconds: float = 30.0
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_topic: str = "detections/snips"

    @classmethod
    def load(cls, path=None):
        values = json.loads(Path(path).read_text()) if path else {}
        settings = cls(**values)  # Unknown keys fail at startup, never silently ignored.
        for name, value in asdict(settings).items():
            default = getattr(cls(), name)
            if isinstance(default, str):
                if not isinstance(value, str):
                    raise ValueError(f"{name} must be a string")
            elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
            elif value < 0 or (isinstance(default, int) and not isinstance(value, int)):
                raise ValueError(f"invalid {name}")
        zero_ok = {"padding", "confirm_seconds", "min_confidence", "improvement", "free_disk_bytes"}
        for name, value in asdict(settings).items():
            if isinstance(value, (int, float)) and name not in zero_ok and value <= 0:
                raise ValueError(f"{name} must be positive")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", settings.stream_id):
            raise ValueError("stream_id must be 1..64 safe identifier characters")
        if settings.min_confidence > 1 or not 1 <= settings.jpeg_quality <= 100:
            raise ValueError("invalid confidence or JPEG quality")
        if settings.mqtt_port > 65535 or any(c in settings.mqtt_topic for c in "+#\0"):
            raise ValueError("invalid MQTT port/topic")
        if not settings.mqtt_topic or not settings.output_dir:
            raise ValueError("output_dir and mqtt_topic cannot be empty")
        return settings


def crop_rect(box, width, height, padding=0.25, aspect_ratio=4 / 3):
    """Return integer xywh containing the visible ROI, plus boundary flags."""
    x, y, w, h = box
    if not all(math.isfinite(v) for v in (*box, padding, aspect_ratio)):
        raise ValueError("nonfinite geometry")
    if w <= 0 or h <= 0 or width <= 0 or height <= 0 or padding < 0 or aspect_ratio <= 0:
        raise ValueError("invalid geometry")
    left, top = max(0, x), max(0, y)
    right, bottom = min(width, x + w), min(height, y + h)
    if right <= left or bottom <= top:
        raise ValueError("ROI outside frame")
    cw, ch = (right - left) * (1 + 2 * padding), (bottom - top) * (1 + 2 * padding)
    cw, ch = max(cw, ch * aspect_ratio), max(ch, cw / aspect_ratio)
    cw, ch = min(width, math.ceil(cw)), min(height, math.ceil(ch))
    cx = max(0, min(width - cw, math.floor((left + right - cw) / 2)))
    cy = max(0, min(height - ch, math.floor((top + bottom - ch) / 2)))
    return (cx, cy, cw, ch), {
        "object_touches_frame": x <= 0 or y <= 0 or x + w >= width or y + h >= height,
        "aspect_limited": abs(cw / ch - aspect_ratio) > 0.02,
    }


@contextmanager
def bgr_frame(frame, max_pixels):
    """Map only admitted frames. YUV conversion allocates a full BGR frame."""
    import cv2
    info = frame.video_info()
    if info.width * info.height > max_pixels:
        raise ValueError("frame exceeds max_frame_pixels")
    fmt = info.finfo.name
    with frame.data() as data:
        if fmt == "BGR":
            pixels = data
        elif fmt in ("BGRA", "BGRx"):
            pixels = cv2.cvtColor(data, cv2.COLOR_BGRA2BGR)
        elif fmt in ("NV12", "I420"):
            code = cv2.COLOR_YUV2BGR_NV12 if fmt == "NV12" else cv2.COLOR_YUV2BGR_I420
            pixels = cv2.cvtColor(data.reshape(info.height * 3 // 2, info.width), code)
        else:
            raise ValueError(f"unsupported frame format: {fmt}; negotiate BGR system memory")
        if pixels.shape != (info.height, info.width, 3):
            raise ValueError("mapped frame shape does not match caps")
        yield pixels



def frame_planes(data, info, layout):
    """Bounded zero-copy plane views over a mapped buffer, including row padding."""
    import numpy as np
    width, height, fmt = info.width, info.height, info.finfo.name
    if width <= 0 or height <= 0:
        raise ValueError("invalid frame dimensions")
    if fmt in ("NV12", "I420"):
        if width % 2 or height % 2:
            raise ValueError("YUV420 requires even dimensions")
        shapes = [(height, width), (height//2, width)] if fmt == "NV12" else [
            (height, width), (height//2, width//2), (height//2, width//2)]
    elif fmt in ("BGR", "BGRA", "BGRx"):
        shapes = [(height, width * (3 if fmt == "BGR" else 4))]
    else:
        raise ValueError(f"unsupported frame format: {fmt}")
    size = memoryview(data).nbytes
    planes = []
    if int(layout.n_planes) != len(shapes):
        raise ValueError("plane count does not match format")
    for index, (rows, columns) in enumerate(shapes):
        offset, stride = int(layout.offset[index]), int(layout.stride[index])
        if offset < 0 or stride < columns or offset + (rows-1)*stride + columns > size:
            raise ValueError("plane exceeds mapped buffer or has unsupported stride")
        plane = np.ndarray((rows, columns), dtype=np.uint8, buffer=data,
                           offset=offset, strides=(stride, 1))
        plane.flags.writeable = False
        planes.append(plane)
    return planes


@contextmanager
def mapped_planes(frame, info):
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    from gstgva.util import gst_buffer_data
    # Pinned gstgva exposes no public raw-buffer accessor. Isolate this access;
    # unlike frame.data(), it does not repack entire padded frames.
    with gst_buffer_data(frame._VideoFrame__buffer, Gst.MapFlags.READ) as data:
        layout = frame.video_meta()
        if layout is None:
            layout = SimpleNamespace(n_planes=info.finfo.n_planes, offset=info.offset, stride=info.stride)
        yield frame_planes(data, info, layout)


def bgr_crop(planes, info, rect):
    """Own an exact BGR crop; convert only a chroma-aligned region for YUV420."""
    import cv2
    import numpy as np
    x, y, w, h = rect
    width, height, fmt = info.width, info.height, info.finfo.name
    if w <= 0 or h <= 0 or x < 0 or y < 0 or x+w > width or y+h > height:
        raise ValueError("crop outside frame")
    if fmt in ("BGR", "BGRA", "BGRx"):
        channels = 3 if fmt == "BGR" else 4
        crop = planes[0][y:y+h, x*channels:(x+w)*channels].reshape(h, w, channels)
        return crop.copy() if channels == 3 else cv2.cvtColor(crop, cv2.COLOR_BGRA2BGR)
    if fmt not in ("NV12", "I420") or width % 2 or height % 2:
        raise ValueError(f"unsupported crop format or dimensions: {fmt}")
    # Include complete 2x2 chroma blocks, then trim back to the original rect.
    left, top = x & ~1, y & ~1
    right, bottom = (x+w+1) & ~1, (y+h+1) & ~1
    cw, ch = right-left, bottom-top
    packed = np.empty((ch * 3 // 2, cw), dtype=np.uint8)
    packed[:ch] = planes[0][top:bottom, left:right]
    if fmt == "NV12":
        packed[ch:] = planes[1][top//2:bottom//2, left:right]
        code = cv2.COLOR_YUV2BGR_NV12
    else:
        chroma = packed.reshape(-1)[cw*ch:].reshape(2, ch//2, cw//2)
        for index, plane in enumerate(planes[1:]):
            chroma[index] = plane[top//2:bottom//2, left//2:right//2]
        code = cv2.COLOR_YUV2BGR_I420
    converted = cv2.cvtColor(packed, code)
    # Exact allocation: queued bytes exclude alignment padding and borrowed views.
    return converted[y-top:y-top+h, x-left:x-left+w].copy()


def observations(frame):
    for roi in frame.regions():
        try:
            box = tuple(roi.rect())
            confidence = float(roi.confidence())
            if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                continue
            yield {"tracker_id": roi.object_id(), "region_id": roi.region_id(),
                   "label": roi.label(), "confidence": confidence, "detection_rect": box}
        except (ValueError, RuntimeError, TypeError):
            continue


def json_observations(objects):
    """Use gvametaconvert's pixel rectangle, never its normalized detection box."""
    for obj in objects:
        try:
            box = tuple(obj[key] for key in ("x", "y", "w", "h"))
            detection = obj["detection"]
            confidence = float(detection["confidence"])
            if (not all(isinstance(v, int) and not isinstance(v, bool) for v in box)
                    or not math.isfinite(confidence) or not 0 <= confidence <= 1):
                continue
            yield {"tracker_id": obj.get("id"), "region_id": obj["region_id"],
                   "label": obj.get("roi_type", detection.get("label", "")),
                   "confidence": confidence, "detection_rect": box}
        except (KeyError, ValueError, TypeError, AttributeError):
            continue


def atomic_write(path, data):
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            import os
            os.fsync(handle.fileno())
        temporary.replace(path)
        # Persist the rename as well as the file contents.
        with directory_fd(path.parent) as fd:
            import os
            os.fsync(fd)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def directory_fd(path):
    import os
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield fd
    finally:
        os.close(fd)


class OwnershipLost(RuntimeError):
    """This producer's lock was replaced by a new owner."""


def owned_operation(operation):
    @wraps(operation)
    def guarded(self, *args, **kwargs):
        with self.owned():
            return operation(self, *args, **kwargs)
    return guarded


class Store:
    """Single owner per directory; JSON sidecars are the durable event outbox."""
    def __init__(self, settings):
        self.settings = settings
        self.root = Path(settings.output_dir)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.status = self.root / "status.json"
        self.evicted = 0
        # Creation-ordered candidate stems, so rotation never scans the
        # directory. maintain() rebuilds it from disk whenever it walks.
        self.order = OrderedDict()  # stem -> expiry, in creation order
        self.outbox = OrderedDict()  # pending stems, recovered at startup
        self.lock = None
        self._mutex = threading.RLock()
        self._operation_fd = None
        try:
            with self.owned():
                self.lock = self._acquire()
                generation = self.root / "generation"
                if not generation.exists():
                    atomic_write(generation, uuid.uuid4().hex.encode())
                self.generation = generation.read_text().strip()
                if not re.fullmatch(r"[a-f0-9]{32}", self.generation):
                    raise ValueError("invalid store generation")
                # Immediately, so a pipeline that dies before its first report tick still
                # leaves a timestamp for the next producer to judge it by.
                self.heartbeat()
                self.maintain()
        except BaseException:
            self.close()
            raise

    @contextmanager
    def owned(self):
        # Serialize takeover and file/outbox operations across producer objects
        # and processes. Nested save -> maintain calls reuse the same flock.
        with self._mutex:
            if self._operation_fd is not None:
                yield
                return
            with directory_fd(self.root) as fd:
                fcntl.flock(fd, fcntl.LOCK_EX)
                if self.lock is not None:
                    try:
                        valid = not self.lock.closed and os.path.samestat(
                            os.fstat(self.lock.fileno()), (self.root / ".lock").stat())
                    except FileNotFoundError:
                        valid = False
                    if not valid:
                        raise OwnershipLost(f"{self.root} producer was displaced")
                self._operation_fd = fd
                try:
                    yield
                finally:
                    self._operation_fd = None

    def _acquire(self):
        """Exclusive owner, with takeover of a store whose producer is gone.

        Normal callback destruction releases the lock. If DLSPS retains the
        callback after a pipeline stops, finalization cannot run; takeover lets
        a replacement start without restarting the whole service.

        Liveness is the status file's mtime, not the lock: a live producer
        refreshes it every report_seconds, a leaked one stops the moment frames
        stop. Unlinking before reopening is what actually breaks the tie - the
        old worker detects the replaced inode on its next pass and exits.
        Store operations share a directory flock with takeover, so an old
        producer cannot write or expire files after the new owner takes over.

        ponytail: mtime is the liveness signal. A producer stalled longer than
        takeover_seconds while still alive may be displaced. Move to a pid/boot-id
        claim only if stalls that long become normal.
        """
        path = self.root / ".lock"
        for attempt in (1, 2):
            handle = path.open("a")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return handle
            except OSError:
                handle.close()
            if attempt == 2:
                break
            try:
                idle = time.time() - self.status.stat().st_mtime
            except FileNotFoundError:
                idle = None
            if idle is None or idle < self.settings.takeover_seconds:
                raise RuntimeError(
                    f"{self.root} is owned by a live producer"
                    + (f" (last seen {idle:.0f}s ago)" if idle is not None else ""))
            path.unlink(missing_ok=True)
        raise RuntimeError(f"could not take over {self.root}")

    @owned_operation
    def heartbeat(self, stats=None, observed_at=None):
        """Liveness plus the counters that never reach MQTT (D17)."""
        stamp = time.time() if observed_at is None else observed_at
        atomic_write(self.status, json.dumps({
            "stream_id": self.settings.stream_id,
            "generation": self.generation,
            "updated_at": stamp,
            "stats": dict(stats or {}),
        }, allow_nan=False).encode())
        # Queued reports must not make a stalled frame producer look alive.
        os.utime(self.status, (stamp, stamp))

    @owned_operation
    def maintain(self):
        now = time.time()
        events = {}
        for path in self.root.iterdir():
            if re.fullmatch(r"[a-f0-9]{32}\.(jpg|json|pending)\.tmp", path.name):
                path.unlink()
            elif re.fullmatch(r"[a-f0-9]{32}\.jpg", path.name):
                stem = path.with_suffix("")
                if not stem.with_suffix(".json").exists() and not stem.with_suffix(".pending").exists():
                    path.unlink(missing_ok=True)  # Save-before-outbox crash: never announced.
            elif re.fullmatch(r"[a-f0-9]{32}\.(json|pending)", path.name):
                event = json.loads(path.read_text())
                if event["expires_at"] <= now:
                    path.with_suffix(".jpg").unlink(missing_ok=True)
                    path.unlink()
                else:
                    events[path.stem] = event["expires_at"]
        files = list(self.root.iterdir())
        self.bytes = sum(p.stat().st_size for p in files if p.is_file())
        self.count = sum(p.suffix == ".jpg" for p in files)
        # Rebuild the eviction order here, where the directory is already being
        # walked, so rotation itself never has to scan. Oldest first.
        live = sorted(
            (p.stat().st_mtime, p.stem) for p in files
            if p.suffix in (".json", ".pending") and re.fullmatch(r"[a-f0-9]{32}", p.stem))
        self.order = OrderedDict((stem, events[stem]) for _, stem in live)
        self.outbox = OrderedDict((p.stem, None) for p in files
                                  if p.suffix == ".pending" and p.stem in events)

    def _remove(self, stem):
        had_image = False
        for suffix in (".jpg", ".json", ".pending"):
            path = self.root / (stem + suffix)
            try:
                size = path.stat().st_size
                path.unlink()
            except FileNotFoundError:
                continue
            self.bytes = max(0, self.bytes - size)
            if suffix == ".jpg":
                had_image = True
                self.count = max(0, self.count - 1)
        self.order.pop(stem, None)
        self.outbox.pop(stem, None)
        return had_image

    @owned_operation
    def expire(self):
        now = time.time()
        # Bounded by store_candidates; no disk reads for unexpired entries.
        for stem, expiry in list(self.order.items()):
            if expiry <= now:
                self._remove(stem)

    def _evict(self, needed):
        """Rotate the store: drop the oldest candidates to make room for a new one.

        Without this the quota is a cliff, not a budget - `save` refuses every
        new candidate once the store is full and the store then stays full until
        `retention_seconds` expires the whole backlog, so the pipeline keeps the
        *oldest* evidence and discards everything happening now. Rotation makes
        the store a rolling window instead: the newest candidate always gets in.

        Eviction order is the in-memory `order` index, oldest first, so this
        uses the oldest entry rather than a directory scan. `maintain` rebuilds it from
        disk on the walk it already does, which is also what recovers the order
        after a restart or a takeover, when memory starts empty. Scanning per
        save instead would re-list, re-stat and re-sort the whole directory on
        every write, in the same thread that does the JPEG encode.

        Entries whose files are already gone - expired by `maintain`, or never
        written - are skipped without counting as an eviction.

        ponytail: creation order, not a value function. Quality already decides
        *whether* a candidate is written (improvement threshold, N per track); it
        does not need to decide eviction order too. Note this means an
        undelivered `.pending` backlog is evicted like anything else if the
        broker stays down long enough to fill the store; keeping stale
        undeliverable crops while dropping live evidence would be the worse
        trade.
        """
        c = self.settings
        if not self.order:  # Fresh producer or post-takeover: recover from disk.
            self.maintain()
        evicted = 0
        while self.order and (self.count >= c.store_candidates
                              or self.bytes + needed > c.store_bytes):
            stem = next(iter(self.order))
            evicted += self._remove(stem)
        return evicted

    @owned_operation
    def save(self, event, encoded):
        payload = json.dumps(event, allow_nan=False).encode()
        size = len(encoded) + len(payload)
        c = self.settings
        if size > c.store_bytes:
            raise ValueError("individual crop exceeds store byte budget")
        if self.count >= c.store_candidates or self.bytes + size > c.store_bytes:
            self.evicted += self._evict(size)
        # Free space is the one limit rotation cannot fix: the shortfall is not
        # ours to reclaim, so this stays a refusal.
        if shutil.disk_usage(self.root).free < c.free_disk_bytes + size:
            raise OSError("crop store free-space reserve reached")
        if self.count >= c.store_candidates or self.bytes + size > c.store_bytes:
            raise OSError("crop store quota reached and nothing could be evicted")
        image = self.root / event["storage_key"]
        sidecar = image.with_suffix(".pending" if c.mqtt_host else ".json")
        atomic_write(image, encoded)
        try:
            atomic_write(sidecar, payload)
        except Exception:
            image.unlink(missing_ok=True)
            raise
        self.bytes += size
        self.count += 1
        # Newest at the right; _evict pops from the left.
        self.order[image.stem] = event["expires_at"]
        if c.mqtt_host:
            self.outbox[image.stem] = None

    @owned_operation
    def publish_one(self, client):
        delivery = getattr(self, "delivery", None)
        if delivery is not None:
            path, result = delivery
            if result.is_published():
                if path.exists():
                    path.replace(path.with_suffix(".json"))
                self.outbox.pop(path.stem, None)
                self.delivery = None
                return True
            return False  # Paho owns retries; do not enqueue duplicates each tick.
        if not self.outbox or client is None or not client.is_connected():
            return False
        stem = next(iter(self.outbox))
        path = self.root / (stem + ".pending")
        if self.order[stem] <= time.time():
            self._remove(stem)
            return False
        try:
            payload = path.read_bytes()
        except FileNotFoundError:
            self.outbox.pop(stem, None)
            return False
        result = client.publish(self.settings.mqtt_topic, payload, qos=1, retain=False)
        if result.rc == 0:
            self.delivery = (path, result)
        return False

    def close(self):
        with self._mutex:
            if self.lock is not None:
                self.lock.close()


class CropCandidates:
    def __init__(self, config=None):
        import cv2  # Missing runtime dependency must fail visibly at startup.
        self.c = Settings.load(config)
        if self.c.mqtt_host:
            import paho.mqtt.client
        self.store = Store(self.c)
        self.session = uuid.uuid4().hex
        self.frame_sequence = 0
        self.tracks = OrderedDict()
        self.jobs = OrderedDict()
        self.pending = set()
        self.control = {"bytes": 0, "stopping": False, "store_blocked": False, "report": None, "worker_failed": False}  # Includes in-flight crop bytes.
        self.condition = threading.Condition()
        self.stats = Counter()
        self.tokens = float(self.c.burst)
        self.last_token = time.monotonic()
        self.last_report = self.last_token
        # Worker owns shared data, never the callback: gvapython destruction can stop it.
        shared = SimpleNamespace(**{key: getattr(self, key) for key in
            ("c", "store", "session", "condition", "jobs", "pending", "control", "stats")})
        self.worker = threading.Thread(target=CropCandidates._work, args=(shared,),
                                       name="crop-writer", daemon=True)
        self.worker.start()
        self.finalizer = weakref.finalize(self, CropCandidates._stop, self.condition,
                                         self.control, self.worker)

    def _identity(self, frame):
        """Run AFTER gvametaconvert; preserve stock JSON and append correlation."""
        context = {"schema_version": 1, "stream_id": self.c.stream_id,
                   "tracking_session_id": self.session, "generation": self.store.generation,
                   "frame_sequence": self.frame_sequence, "observed_at": time.time()}
        found = False
        objects = []
        for message in list(frame.messages()):
            data = json.loads(message)
            if isinstance(data, dict) and ("objects" in data or "resolution" in data):
                objects.extend(data.get("objects") or [])
                data["crop_context"] = context
                if "frame_id" in data:
                    context["source_frame_id"] = data["frame_id"]
                frame.remove_message(message)
                frame.add_message(json.dumps(data, allow_nan=False))
                found = True
        if not found:
            raise ValueError("place callback after gvametaconvert: missing observation JSON")
        return context, objects

    def process_frame(self, frame):
        if self.control["stopping"]:
            return True
        started = time.monotonic()
        self.frame_sequence += 1
        try:
            if self.control["worker_failed"]:
                # Keep camera attribution after writer failure, but never after takeover.
                with self.store.owned():
                    pass
            context, objects = self._identity(frame)
            if self.control["worker_failed"]:
                return True
            info = frame.video_info()
            eligible = []
            with self.condition:
                self.tokens = min(self.c.burst, self.tokens + (started - self.last_token) * self.c.rate)
                self.last_token = started
                while self.tracks and next(iter(self.tracks.values()))["last"] < started - self.c.track_ttl_seconds:
                    self.tracks.popitem(last=False)
                seen_ids = set()
                for obs in json_observations(objects):
                    tid = obs["tracker_id"]
                    self.stats["observations"] += 1
                    if tid is None or not isinstance(tid, int) or isinstance(tid, bool) or tid < 0:
                        self.stats["untracked"] += 1
                        continue
                    if tid in seen_ids:
                        self.stats["duplicate_track_roi"] += 1
                        continue
                    seen_ids.add(tid)
                    state = self.tracks.get(tid)
                    if state is None:
                        if len(self.tracks) >= self.c.max_tracks:
                            self.stats["track_capacity"] += 1
                            continue
                        state = {"first": started, "last": started, "seen": 0,
                                 "saved": 0, "score": 0.0, "attempt": -math.inf}
                        self.tracks[tid] = state
                        self.stats["new_tracks"] += 1
                    state["last"] = started
                    state["seen"] += 1
                    self.tracks.move_to_end(tid)
                    if (tid in self.pending or state["saved"] >= self.c.candidates_per_track
                            or started - state["first"] < self.c.confirm_seconds
                            or state["seen"] < self.c.min_observations
                            or started - state["attempt"] < self.c.interval_seconds
                            or obs["confidence"] < self.c.min_confidence
                            or min(obs["detection_rect"][2:]) < self.c.min_object_pixels):
                        continue
                    if self.control["store_blocked"]:
                        self.stats["store_limited"] += 1
                        continue
                    if self.tokens < 1:
                        self.stats["rate_limited"] += 1
                        continue
                    if len(self.pending) >= self.c.queue_jobs or self.control["bytes"] >= self.c.queue_bytes:
                        self.stats["queue_limited"] += 1
                        continue
                    eligible.append((state["saved"] > 0, state["attempt"], tid, state, obs))
            # First snapshots first, then least recently attempted. No pixel work for rejected jobs.
            eligible.sort(key=lambda item: item[:3])
            admitted = []
            with self.condition:
                for _, _, tid, state, obs in eligible:
                    if self.control["store_blocked"]:
                        self.stats["store_limited"] += 1
                        continue
                    if self.tokens < 1:
                        self.stats["rate_limited"] += 1
                        continue
                    if len(self.pending) >= self.c.queue_jobs:
                        self.stats["queue_limited"] += 1
                        continue
                    try:
                        rect, flags = crop_rect(obs["detection_rect"], info.width, info.height,
                                                self.c.padding, self.c.aspect_ratio)
                    except ValueError:
                        self.stats["invalid_box"] += 1
                        continue
                    size = rect[2] * rect[3] * 3
                    if self.control["bytes"] + size > self.c.queue_bytes:
                        self.stats["queue_limited"] += 1
                        continue
                    self.tokens -= 1
                    self.control["bytes"] += size
                    self.pending.add(tid)
                    state["attempt"] = started
                    admitted.append((tid, state, obs, rect, flags, size))
            if admitted:
                submitted = set()
                try:
                    if info.width * info.height > self.c.max_frame_pixels:
                        raise ValueError("frame exceeds max_frame_pixels")
                    with mapped_planes(frame, info) as planes:
                        for tid, state, obs, rect, flags, size in admitted:
                            copy = bgr_crop(planes, info, rect)
                            event = dict(context, **obs, crop_rect=rect, **flags,
                                         frame_width=info.width, frame_height=info.height)
                            with self.condition:
                                if self.control["stopping"] or self.control["worker_failed"]:
                                    raise RuntimeError("crop worker stopped during frame mapping")
                                self.jobs[tid] = (state, event, copy, size)
                                self.condition.notify()
                            submitted.add(tid)
                except Exception:
                    with self.condition:
                        for tid, _, _, _, _, size in admitted:
                            if tid not in submitted:
                                self.pending.remove(tid)
                                self.control["bytes"] -= size
                    raise
        except OwnershipLost:
            self.control["stopping"] = True
        except Exception as exc:
            self.stats["callback_errors"] += 1
            self.stats["last_error"] = str(exc)[:200]
        finally:
            self.stats["frames"] += 1
            self.stats["callback_us"] += int((time.monotonic() - started) * 1e6)
            if not self.control["stopping"] and not self.control["worker_failed"] and started - self.last_report >= self.c.report_seconds:
                report = {"crop_stats": dict(self.stats), "pending_bytes": self.control["bytes"],
                          "pending_jobs": len(self.pending), "tracks": len(self.tracks),
                          # Store occupancy, so a full-and-rotating store is
                          # visibly different from one that stopped writing.
                          "stored_candidates": self.store.count,
                          "stored_bytes": self.store.bytes,
                          "evicted": self.store.evicted,
                          "session": self.session}
                with self.condition:
                    self.control["report"] = (report, time.time())
                    self.condition.notify()
                self.last_report = started
        return True  # Crop failure never intentionally drops the video buffer.

    def _work(self):
        import cv2
        client = None
        try:
            if self.c.mqtt_host:
                import paho.mqtt.client as mqtt
                client = mqtt.Client(client_id="crops-" + self.session)
                client.max_queued_messages_set(1)
                client.max_inflight_messages_set(1)
                client.connect_async(self.c.mqtt_host, self.c.mqtt_port)
                client.loop_start()
            maintained = reconciled = time.monotonic()
            while True:
                with self.store.owned():
                    pass  # Retire displaced workers even when no frames arrive.
                with self.condition:
                    if not self.jobs and not self.control["stopping"]:
                        self.condition.wait(timeout=0.1)
                    if self.control["stopping"] and not self.jobs:
                        break
                try:
                    with self.condition:
                        report = self.control["report"]
                        self.control["report"] = None
                    if report and time.time() - report[1] < self.c.takeover_seconds:
                        self.store.heartbeat(report[0], observed_at=report[1])
                        print(json.dumps(report[0]), flush=True)
                    if time.monotonic() - reconciled >= 60:
                        self.store.maintain()
                        reconciled = time.monotonic()
                    if time.monotonic() - maintained >= 5:
                        self.store.expire()
                        # Only a real inability to write blocks admission now.
                        # Count and byte quotas are rotated through by `save`, so
                        # treating a full store as blocked would freeze the store
                        # at its cap forever and discard every current candidate.
                        self.control["store_blocked"] = (
                            shutil.disk_usage(self.store.root).free < self.c.free_disk_bytes)
                        maintained = time.monotonic()
                    if self.store.publish_one(client):
                        self.stats["published"] += 1
                except OwnershipLost:
                    raise
                except Exception as exc:
                    self.stats["delivery_errors"] += 1
                    self.stats["last_error"] = str(exc)[:200]
                with self.condition:
                    item = self.jobs.popitem(last=False) if self.jobs else None
                if item:
                    tid, (state, event, pixels, size) = item
                    try:
                        dx, dy, dw, dh = map(int, event["detection_rect"])
                        cx, cy, _, _ = event["crop_rect"]
                        roi = pixels[max(0, dy-cy):min(pixels.shape[0], dy+dh-cy),
                                     max(0, dx-cx):min(pixels.shape[1], dx+dw-cx)]
                        sharpness = float(cv2.Laplacian(cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())
                        # ponytail: heuristic quality, calibrate weights on deployment footage.
                        score = math.sqrt(roi.shape[0] * roi.shape[1]) * event["confidence"] * (1 + math.log1p(sharpness))
                        if event["object_touches_frame"]:
                            score *= 0.75
                        if state["saved"] and score <= state["score"] * (1 + self.c.improvement):
                            self.stats["not_improved"] += 1
                            continue
                        scale = min(1, self.c.max_dimension / max(pixels.shape[:2]))
                        if scale < 1:
                            pixels = cv2.resize(pixels, (max(1, round(pixels.shape[1]*scale)),
                                                       max(1, round(pixels.shape[0]*scale))), interpolation=cv2.INTER_AREA)
                        ok, encoded = cv2.imencode(".jpg", pixels, [cv2.IMWRITE_JPEG_QUALITY, self.c.jpeg_quality])
                        if not ok:
                            raise OSError("JPEG encoding failed")
                        cid = uuid.uuid4().hex
                        event.update(candidate_id=cid, storage_key=cid + ".jpg", event="crop_ready",
                                     candidate_number=state["saved"] + 1, quality_score=score,
                                     sharpness=sharpness, image_width=pixels.shape[1], image_height=pixels.shape[0],
                                     expires_at=time.time() + self.c.retention_seconds,
                                     confidence_freshness="tracker_metadata_unverified")
                        try:
                            self.store.save(event, encoded.tobytes())
                        except OSError:
                            self.control["store_blocked"] = True
                            raise
                        with self.condition:
                            state["saved"] += 1
                            state["score"] = score
                        self.stats["saved"] += 1
                    except OwnershipLost:
                        raise
                    except Exception as exc:
                        self.stats["writer_errors"] += 1
                        self.stats["last_error"] = str(exc)[:200]
                    finally:
                        with self.condition:
                            self.pending.remove(tid)
                            self.control["bytes"] -= size
        except OwnershipLost:
            self.stats["ownership_lost"] += 1
            self.control["stopping"] = True
        except Exception as exc:
            self.control["worker_failed"] = True
            self.stats["worker_errors"] += 1
            self.stats["last_error"] = str(exc)[:200]
            print(json.dumps({"crop_worker_error": str(exc)[:200], "session": self.session}), flush=True)
            with self.condition:
                for tid, (_, _, _, size) in self.jobs.items():
                    self.pending.discard(tid)
                    self.control["bytes"] -= size
                self.jobs.clear()
                self.control["report"] = None
            # Stay available for shutdown/takeover; do not refresh the heartbeat
            # or attempt more images while observations continue independently.
            while not self.control["stopping"]:
                try:
                    with self.store.owned():
                        pass
                except (OwnershipLost, OSError):
                    self.control["stopping"] = True
                    break
                with self.condition:
                    self.condition.wait(timeout=0.1)
        finally:
            with self.condition:
                self.control["stopping"] = True
                for tid, (_, _, _, size) in self.jobs.items():
                    self.pending.remove(tid)
                    self.control["bytes"] -= size
                self.jobs.clear()
            try:
                if client:
                    client.disconnect()
                    client.loop_stop()
            finally:
                self.store.close()

    @staticmethod
    def _stop(condition, control, worker):
        with condition:
            control["stopping"] = True
            condition.notify_all()
        worker.join(timeout=5)

    def close(self):
        self.finalizer()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print-defaults", action="store_true")
    parser.add_argument("--clear-store", metavar="DIRECTORY")
    parser.add_argument("--confirm-clear", action="store_true")
    parser.add_argument("--self-check", action="store_true",
                        help="offline checks for store ownership; no cv2 or broker needed")
    args = parser.parse_args()
    if args.self_check:
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            base = Settings(output_dir=tmp, takeover_seconds=30.0)
            live = Store(base)
            try:
                Store(base)
            except RuntimeError as exc:
                assert "live producer" in str(exc), exc
            else:
                raise AssertionError("a live producer's store must not be taken over")

            # Age the heartbeat past the threshold: the holder is now a zombie.
            # `live` deliberately stays open - that is exactly the leak this
            # takeover exists for.
            old = time.time() - base.takeover_seconds - 1
            os.utime(live.status, (old, old))
            taken = Store(base)
            assert taken.generation == live.generation, "takeover must not rotate generation"
            assert time.time() - taken.status.stat().st_mtime < 5, "new owner must heartbeat"

            try:
                Store(base)
            except RuntimeError as exc:
                assert "live producer" in str(exc), exc
            else:
                raise AssertionError("the new owner must itself be protected")
            taken.close()
            live.close()

        # Rotation: a full store must keep accepting new candidates by dropping
        # the oldest, must never refuse the newest one, and must do it from the
        # in-memory index rather than rescanning the directory.
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(output_dir=tmp, store_candidates=100,
                                store_bytes=1024 * 1024)
            store = Store(settings)
            scans = [0]
            walk = store.maintain

            def counted():
                scans[0] += 1
                return walk()

            store.maintain = counted
            try:
                keys = []
                for n in range(300):
                    cid = uuid.uuid4().hex
                    keys.append(cid)
                    store.save({"storage_key": cid + ".jpg", "n": n,
                                "expires_at": time.time() + 3600}, b"x" * 128)
                assert store.count <= 100, f"store grew past its cap: {store.count}"
                assert store.evicted >= 200, f"nothing rotated: {store.evicted}"
                survivors = {p.stem for p in store.root.glob("*.jpg")}
                assert len(survivors) == store.count, (survivors, store.count)
                assert keys[-1] in survivors, "the newest candidate must always be kept"
                assert not (survivors & set(keys[:100])), "the oldest must go first"
                for cid in survivors:
                    assert (store.root / (cid + ".json")).exists(), "orphaned image left behind"
                # The whole point of the index: 200 evictions, no rescans.
                assert scans[0] == 0, f"rotation rescanned the directory {scans[0]}x"
            finally:
                store.maintain = walk
                store.close()

            # Fall back to disk when memory is empty, which is what a restart or
            # a takeover looks like: same directory, brand new Store.
            reopened = Store(settings)
            try:
                assert len(reopened.order) == reopened.count == 100, (
                    len(reopened.order), reopened.count)
                cid = uuid.uuid4().hex
                reopened.save({"storage_key": cid + ".jpg",
                               "expires_at": time.time() + 3600}, b"x" * 128)
                assert reopened.count == 100, "a reopened full store must still rotate"
                assert reopened.evicted == 1, reopened.evicted
                assert (reopened.root / (cid + ".jpg")).exists(), "newest must survive"
            finally:
                reopened.close()
        print("all checks passed")
    elif args.print_defaults:
        print(json.dumps(asdict(Settings()), indent=2))
    elif args.clear_store and args.confirm_clear:
        store = Store(Settings(output_dir=args.clear_store))  # Refuses a running producer's lock.
        try:
            for path in store.root.iterdir():
                if re.fullmatch(r"[a-f0-9]{32}\.(jpg|json|pending)(\.tmp)?", path.name):
                    path.unlink()
            atomic_write(store.root / "generation", uuid.uuid4().hex.encode())
        finally:
            store.close()
    else:
        parser.error("use --print-defaults or --clear-store DIRECTORY --confirm-clear")
