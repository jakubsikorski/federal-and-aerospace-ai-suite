#!/usr/bin/env python3
"""Run inside the pinned DLSPS image: python3 test_crops.py."""
from contextlib import contextmanager
import gc
import json
import os
from pathlib import Path
import random
import tempfile
import threading
import time
from types import SimpleNamespace
import uuid
import weakref
import atexit

from unittest.mock import patch

import cv2
import numpy as np
from crop_candidates import (CropCandidates, OwnershipLost, Settings, Store, bgr_crop,
    bgr_from_payload, crop_payload, crop_payload_size, crop_quality, crop_rect,
    frame_planes, json_observations, mapped_planes, observations)
from crop_probe import CropProbe

SCRATCH = Path(os.environ.get("CROP_TEST_TMPDIR", Path.cwd() / ".crop-test-tmp"))
SCRATCH.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(SCRATCH)
if "CROP_TEST_TMPDIR" not in os.environ:
    atexit.register(lambda: SCRATCH.rmdir() if SCRATCH.exists() and not any(SCRATCH.iterdir()) else None)


class ROI:
    def __init__(self, tid):
        self.tid = tid
    def rect(self):
        return (30, 30, 30, 40)
    def object_id(self):
        return self.tid
    def region_id(self):
        return 77
    def label(self):
        return "person"
    def confidence(self):
        return 0.9


class Frame:
    def __init__(self, ids, broken=False):
        self.ids, self.broken = ids, broken
        self.maps = 0
        self.text = [json.dumps({"objects": [{"id": i, "region_id": 77,
            "x": 30, "y": 30, "w": 30, "h": 40, "roi_type": "person",
            "detection": {"label": "person", "confidence": 0.9},
            "custom_classifier": {"label": "example", "confidence": 0.8}}
            for i in ids], "frame_id": 42})]
    def video_info(self):
        return SimpleNamespace(width=120, height=100, finfo=SimpleNamespace(name="BGR", n_planes=1),
                               n_planes=1, offset=[0], stride=[360])
    def video_meta(self):
        return None
    @property
    def _VideoFrame__buffer(self):
        from gi.repository import Gst
        Gst.init(None)
        self.maps += 1
        if self.broken:
            raise RuntimeError("injected map failure")
        pixels = np.zeros((100, 120, 3), dtype=np.uint8)
        pixels[30:70, 30:60] = (20, 180, 240)
        return Gst.Buffer.new_wrapped(pixels.tobytes())
    def regions(self):
        return (ROI(i) for i in self.ids)
    def messages(self):
        return list(self.text)
    def remove_message(self, text):
        self.text.remove(text)
    def add_message(self, text):
        self.text.append(text)
    @contextmanager
    def data(self):
        self.maps += 1
        if self.broken:
            raise RuntimeError("injected map failure")
        pixels = np.zeros((100, 120, 3), dtype=np.uint8)
        pixels[30:70, 30:60] = (20, 180, 240)
        yield pixels
        pixels[:] = 0  # Async encoder must own a copy.


def make(root, predicted_confidence=None, **overrides):
    config = root / (uuid.uuid4().hex + ".json")
    config.write_text(json.dumps(dict(output_dir=str(root / uuid.uuid4().hex),
        confirm_seconds=0, min_observations=1, report_seconds=1000, **overrides)))
    return CropCandidates(str(config), predicted_confidence=predicted_confidence)


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for crop worker")


def takeover_checks():
    for mqtt_host in ("", "mqtt"):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            config.write_text(json.dumps(dict(output_dir=str(root / "store"),
                stream_id="crop_validation", mqtt_host=mqtt_host, takeover_seconds=1)))
            gc.collect()
            baseline = len(os.listdir('/proc/self/fd'))
            retained = []
            try:
                for _ in range(20):
                    if retained:
                        os.utime(retained[-1].store.status, (1, 1))
                    current = CropCandidates(str(config))
                    retained.append(current)
                    for old in retained[:-1]:
                        old.worker.join(timeout=2)
                        assert not old.worker.is_alive() and old.store.lock.closed
                        assert old.control['bytes'] == 0 and not old.pending
                        before = current.store.status.read_bytes()
                        old.last_report = 0
                        old.process_frame(Frame([1]))
                        assert current.store.status.read_bytes() == before
                        for operation in (old.store.heartbeat, old.store.maintain, old.store.expire,
                                          lambda: old.store.save({}, b''),
                                          lambda: old.store.publish_one(None)):
                            try:
                                operation()
                                raise AssertionError('displaced store accepted an operation')
                            except OwnershipLost:
                                pass
                current.close()
                gc.collect()
                assert len(os.listdir('/proc/self/fd')) == baseline
            finally:
                for producer in retained:
                    producer.close()
            print(f'PASS: 20 retained takeovers, mqtt={bool(mqtt_host)}, descriptors={baseline}')

    # Displace a worker while it owns copied pixels inside the JPEG encoder.
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        old = make(root, takeover_seconds=1)
        entered, release = threading.Event(), threading.Event()
        encode = cv2.imencode
        def slow_encode(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return encode(*args, **kwargs)
        cv2.imencode = slow_encode
        replacement = None
        try:
            old.process_frame(Frame([1]))
            assert entered.wait(2)
            os.utime(old.store.status, (1, 1))
            replacement = Store(old.c)
            status = replacement.status.read_bytes()
            release.set()
            old.worker.join(timeout=2)
            assert not old.worker.is_alive() and old.store.lock.closed
            assert not list(replacement.root.glob('*.jpg'))
            assert replacement.status.read_bytes() == status
            assert old.control['bytes'] == 0 and not old.pending
        finally:
            release.set()
            cv2.imencode = encode
            old.close()
            if replacement:
                replacement.close()
    print('PASS: takeover during JPEG encoding rejects the retired write')

    with tempfile.TemporaryDirectory() as temporary:
        old = make(Path(temporary), takeover_seconds=1)
        entered, release = threading.Event(), threading.Event()
        class SlowFrame(Frame):
            @property
            def _VideoFrame__buffer(self):
                entered.set()
                assert release.wait(5)
                return super()._VideoFrame__buffer
        callback = threading.Thread(target=old.process_frame, args=(SlowFrame([1]),))
        replacement = None
        try:
            callback.start()
            assert entered.wait(2)
            os.utime(old.store.status, (1, 1))
            replacement = Store(old.c)
            old.worker.join(timeout=2)
            assert not old.worker.is_alive()
            release.set()
            callback.join(timeout=2)
            assert not callback.is_alive()
            assert not old.jobs and not old.pending and old.control['bytes'] == 0
            assert not list(replacement.root.glob('*.jpg'))
        finally:
            release.set()
            callback.join(timeout=2)
            old.close()
            if replacement:
                replacement.close()
    print('PASS: takeover during frame mapping releases reserved pixels')



def limited_conversion_checks():
    rng = np.random.default_rng(12)
    width, height = 120, 100
    rectangles = [(0,0,1,1), (119,99,1,1), (1,3,37,29), (0,0,120,100),
                  (3,5,117,95), (30,30,30,40)]
    rectangles += [(x,y,int(rng.integers(1,width-x+1)),int(rng.integers(1,height-y+1)))
                   for x,y in zip(rng.integers(0,width,30), rng.integers(0,height,30))]
    for fmt, channels in (("BGR",3), ("BGRA",4), ("BGRx",4), ("NV12",1), ("I420",1)):
        shape = (height,width,channels) if channels > 1 else (height*3//2,width,1)
        data = rng.integers(0,256,shape,dtype=np.uint8)
        info = SimpleNamespace(width=width,height=height,finfo=SimpleNamespace(name=fmt))
        code = {"BGRA":cv2.COLOR_BGRA2BGR,"BGRx":cv2.COLOR_BGRA2BGR,
                "NV12":cv2.COLOR_YUV2BGR_NV12,"I420":cv2.COLOR_YUV2BGR_I420}.get(fmt)
        reference = data if code is None else cv2.cvtColor(data if channels > 1 else data.reshape(height*3//2,width),code)
        if fmt == "NV12":
            layout = SimpleNamespace(n_planes=2,offset=[0,width*height],stride=[width,width])
        elif fmt == "I420":
            layout = SimpleNamespace(n_planes=3,offset=[0,width*height,width*height*5//4],stride=[width,width//2,width//2])
        else:
            layout = SimpleNamespace(n_planes=1,offset=[0],stride=[width*channels])
        planes = frame_planes(data,info,layout)
        for rect in rectangles:
            x,y,w,h = rect
            with patch("cv2.cvtColor", side_effect=AssertionError("callback converted colour")):
                payload = crop_payload(planes, info, rect)
            assert payload["data"].flags.owndata and payload["bytes"] == payload["data"].nbytes
            assert payload["bytes"] == crop_payload_size(info, rect)
            assert not np.shares_memory(payload["data"], data)
            assert np.array_equal(bgr_from_payload(payload), reference[y:y+h,x:x+w]), (fmt, rect)
            crop = bgr_crop(planes,info,rect)
            assert np.array_equal(crop,reference[y:y+h,x:x+w]), (fmt,rect)
            assert crop.flags.owndata and crop.nbytes == w*h*3
            assert not np.shares_memory(crop,data)
        if channels == 1:
            convert = cv2.cvtColor
            with patch("cv2.cvtColor", wraps=convert) as tracked:
                crop = bgr_crop(planes,info,(1,3,37,29))
                assert tracked.call_args.args[0].shape == (30*3//2,38)
        offsets, strides, cursor = [], [], 7
        for plane in planes:
            offsets.append(cursor); strides.append(plane.shape[1]+8)
            cursor += (plane.shape[0]+2)*strides[-1]
        raw = np.full(cursor,199,dtype=np.uint8)
        for plane,offset,stride in zip(planes,offsets,strides):
            target = np.ndarray(plane.shape,dtype=np.uint8,buffer=raw,offset=offset,strides=(stride,1))
            target[:] = plane
        padded_layout = SimpleNamespace(n_planes=len(planes),offset=offsets,stride=strides)
        padded = frame_planes(raw,info,padded_layout)
        assert all(np.shares_memory(plane,raw) for plane in padded)
        for rect in rectangles:
            x,y,w,h=rect
            assert np.array_equal(bgr_crop(padded,info,rect),reference[y:y+h,x:x+w]),(fmt,rect)
        # Exercise the real pinned GStreamer mapping API with padded metadata.
        import gi
        gi.require_version("Gst", "1.0")
        gi.require_version("GstVideo", "1.0")
        from gi.repository import Gst, GstVideo
        from gstgva import VideoFrame
        Gst.init(None)
        caps=Gst.Caps.from_string(f"video/x-raw,format={fmt},width={width},height={height},framerate=30/1")
        real_info=GstVideo.VideoInfo.new_from_caps(caps)
        buffer=Gst.Buffer.new_wrapped(raw.tobytes())
        GstVideo.buffer_add_video_meta_full(buffer,GstVideo.VideoFrameFlags.NONE,
            real_info.finfo.format,width,height,len(planes),
            offsets+[0]*(4-len(offsets)),strides+[0]*(4-len(strides)))
        real_frame=VideoFrame(buffer,video_info=real_info)
        with patch.object(VideoFrame,"data",side_effect=AssertionError("full-frame repack")):
            with mapped_planes(real_frame,real_info) as mapped:
                for rect in rectangles:
                    x,y,w,h=rect
                    assert np.array_equal(bgr_crop(mapped,real_info,rect),reference[y:y+h,x:x+w]),(fmt,rect)
        for bad in (SimpleNamespace(n_planes=len(planes),offset=offsets,stride=[1]*len(planes)),
                    SimpleNamespace(n_planes=len(planes),offset=[-1]*len(planes),stride=strides),
                    SimpleNamespace(n_planes=len(planes),offset=[raw.nbytes]*len(planes),stride=strides)):
            try:
                frame_planes(raw,info,bad)
                raise AssertionError("unsafe plane layout accepted")
            except ValueError:
                pass
        data[:] = 0
        assert crop.any(), "crop retained mapped frame memory"
    print("PASS: region-only conversion matches full-frame BGR/BGRA/BGRx/NV12/I420, odd edges, owned exact-size pixels")


def backoff_and_quality_checks():
    sharp = np.zeros((80, 80, 3), dtype=np.uint8)
    sharp[::2, :] = 255
    blurred = cv2.GaussianBlur(sharp, (21, 21), 0)
    large = np.tile(sharp, (4, 4, 1))
    small = sharp[:30, :30]
    edge_score, _ = crop_quality(sharp, 0.9, True, 256)
    sharp_score, sharpness = crop_quality(sharp, 0.9, False, 256)
    blur_score, blur = crop_quality(blurred, 0.9, False, 256)
    large_score, _ = crop_quality(large, 0.9, False, 64)
    small_score, _ = crop_quality(small, 0.9, False, 64)
    assert sharpness > blur and sharp_score > blur_score * 1.15
    assert large_score > small_score
    assert sharp_score > edge_score

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        producer = make(root, rate=100, burst=10, interval_seconds=0.02,
                        max_interval_seconds=0.08, backoff_multiplier=2,
                        improvement=10, candidates_per_track=4)
        try:
            producer.process_frame(Frame([1]))
            wait_for(lambda: producer.stats["saved"] == 1)
            time.sleep(0.03)
            producer.process_frame(Frame([1]))
            wait_for(lambda: producer.stats["not_improved"] == 1)
            assert 0.039 <= producer.tracks[1]["interval"] <= 0.041
            time.sleep(0.045)
            producer.process_frame(Frame([1]))
            wait_for(lambda: producer.stats["not_improved"] == 2)
            assert 0.079 <= producer.tracks[1]["interval"] <= 0.081

            before = producer.stats["saved"]
            producer.process_frame(Frame([1, 2]))
            wait_for(lambda: producer.stats["saved"] == before + 1)
            assert producer.tracks[2]["saved"] == 1
            assert producer.tracks[1]["saved"] == 1, "backed-off old track should not jump ahead"

            moved = Frame([1])
            moved.text = [json.dumps({"objects": [{"id": 1, "region_id": 77,
                "x": 60, "y": 30, "w": 30, "h": 40, "roi_type": "person",
                "detection": {"label": "person", "confidence": 0.9}}]})]
            time.sleep(0.03)
            producer.process_frame(moved)
            assert producer.tracks[1]["interval"] == producer.c.interval_seconds
            wait_for(lambda: producer.stats["saved"] >= before + 2 or producer.stats["not_improved"] >= 3)
        finally:
            producer.close()
    print("PASS: bounded sharpness ranking, crop backoff growth/reset, and new-track fairness")


def optimization_checks():
    frame = Frame([None, 1, 2])
    objects = json.loads(frame.text[0])["objects"]
    assert list(json_observations(objects)) == list(observations(frame))
    invalid = [None, {}, dict(objects[1], x=1.5), dict(objects[1],
               detection={"confidence": float("nan")})]
    assert list(json_observations(invalid)) == []
    with tempfile.TemporaryDirectory() as temporary:
        producer = make(Path(temporary), rate=0.01, burst=1)
        try:
            with patch.object(Frame, "regions", side_effect=AssertionError("native ROI traversal")):
                producer.process_frame(frame)
                assert not producer.stats["callback_errors"], producer.stats
                assert json.loads(frame.text[0])["objects"] == objects
                producer.tokens = 0
                with patch("crop_candidates.crop_rect", side_effect=AssertionError("budget exhausted")):
                    producer.process_frame(Frame([1, 2, 3]))
                assert not producer.stats["callback_errors"], producer.stats
                assert producer.tracks[2]["seen"] == 2 and 3 in producer.tracks
        finally:
            producer.close()

    # Even a blocked heartbeat must not block process_frame. Its timestamp is
    # the frame's progress time, not the delayed worker's write time.
    with tempfile.TemporaryDirectory() as temporary:
        producer = make(Path(temporary))
        entered, release = threading.Event(), threading.Event()
        heartbeat = producer.store.heartbeat
        caller = []
        def blocked_heartbeat(*args, **kwargs):
            caller.append(threading.current_thread())
            entered.set()
            assert release.wait(3)
            return heartbeat(*args, **kwargs)
        producer.store.heartbeat = blocked_heartbeat
        producer.last_report = 0
        callback = threading.Thread(target=producer.process_frame, args=(Frame([]),))
        try:
            callback.start()
            assert entered.wait(2)
            callback.join(timeout=1)
            assert not callback.is_alive(), "heartbeat blocks video callback"
            assert caller == [producer.worker]
        finally:
            release.set()
            callback.join(timeout=3)
            producer.close()

    with tempfile.TemporaryDirectory() as temporary:
        settings = Settings(output_dir=temporary, mqtt_host="unused")
        store = Store(settings)
        live, expired = uuid.uuid4().hex, uuid.uuid4().hex
        store.save({"storage_key": live+".jpg", "expires_at": time.time()+100}, b"image")
        store.save({"storage_key": expired+".jpg", "expires_at": 0}, b"expired")
        store.close()
        store = Store(settings)
        class Result:
            rc = 0
            def is_published(self):
                return True
        client = SimpleNamespace(is_connected=lambda: True, publish=lambda *a, **k: Result())
        try:
            assert list(store.outbox) == [live] and store.count == 1
            # Add an expired entry after recovery, so expire must use the index.
            store.save({"storage_key": expired+".jpg", "expires_at": 0}, b"expired")
            with patch.object(Path, "iterdir", side_effect=AssertionError("directory scan")), \
                 patch.object(Path, "glob", side_effect=AssertionError("outbox scan")):
                store.expire()
                assert store.count == 1 and expired not in store.outbox
                store.publish_one(client)
                assert store.publish_one(client)
                assert not store.outbox and not store.publish_one(client)
            assert (store.root / (live+".json")).exists()
            stamp = time.time() - 20
            store.heartbeat({"frames": 1}, observed_at=stamp)
            assert abs(store.status.stat().st_mtime - stamp) < 0.001
            assert json.loads(store.status.read_text())["updated_at"] == stamp
        finally:
            store.close()
    print("PASS: JSON/native identity equivalence, metadata preservation, early budget gate, indexed outbox/expiry, asynchronous frame-timed heartbeat")



def failure_safety_checks():
    with tempfile.TemporaryDirectory() as root:
        store=Store(Settings(output_dir=root,store_bytes=1000,free_disk_bytes=0))
        try:
            for _ in range(2):
                store.save({"storage_key":uuid.uuid4().hex+".jpg","expires_at":time.time()+100},b"x"*100)
            before={p.name:p.read_bytes() for p in store.root.iterdir() if p.suffix in (".jpg",".json")}
            try:
                store.save({"storage_key":uuid.uuid4().hex+".jpg","expires_at":time.time()+100},b"x"*2000)
                raise AssertionError("oversized candidate accepted")
            except ValueError:
                pass
            assert store.count==2 and store.evicted==0
            assert before=={p.name:p.read_bytes() for p in store.root.iterdir() if p.suffix in (".jpg",".json")}
        finally:
            store.close()
    with tempfile.TemporaryDirectory() as root:
        # Fail writer startup without involving a real broker or production store.
        import paho.mqtt.client as mqtt
        with patch.object(mqtt.Client,"connect_async",side_effect=RuntimeError("injected worker failure")):
            producer=make(Path(root),mqtt_host="unused",takeover_seconds=1)
            replacement=None
            try:
                for _ in range(100):
                    if producer.control["worker_failed"]: break
                    time.sleep(.01)
                assert producer.control["worker_failed"] and not producer.control["stopping"]
                frame=Frame([1]); producer.process_frame(frame)
                assert "crop_context" in json.loads(frame.text[0]) and frame.maps==0
                os.utime(producer.store.status,(1,1))
                replacement=Store(producer.c)
                producer.worker.join(timeout=2)
                assert not producer.worker.is_alive() and producer.store.lock.closed
                retired=Frame([1]); producer.process_frame(retired)
                assert "crop_context" not in json.loads(retired.text[0])
            finally:
                producer.close()
                if replacement: replacement.close()
    print("PASS: oversized admission preserves evidence; failed writer preserves observations and retires on takeover")


def tracker_prediction_checks():
    # A short-term tracker's 1.0 marks a predicted box; only then is it skipped.
    # Without that tracker (zero-term) a genuine near-one score is cropped.
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for sentinel, confidence, expected in ((1.0, 1.0, 0), (1.0, 0.9, 1), (1.0, 0.99995, 1),
                                               (None, 1.0, 1), (None, 0.99995, 1)):
            producer = make(root, predicted_confidence=sentinel)
            frame = Frame([5])
            frame.text = [text.replace('"confidence": 0.9}', f'"confidence": {confidence}}}')
                          for text in frame.text]
            producer.process_frame(frame)
            producer.close()
            assert producer.stats["saved"] == expected, (sentinel, confidence, producer.stats)
        for bad in (0, 1.5, True, "1"):
            try:
                make(root, predicted_confidence=bad)
            except ValueError:
                continue
            raise AssertionError(f"invalid predicted_confidence accepted: {bad!r}")


def run():
    tracker_prediction_checks()
    failure_safety_checks()
    limited_conversion_checks()
    backoff_and_quality_checks()
    optimization_checks()
    takeover_checks()
    rng = random.Random(8)
    for _ in range(2000):
        width, height = rng.randint(1, 1000), rng.randint(1, 1000)
        x, y = rng.randint(-50, width-1), rng.randint(-50, height-1)
        w, h = rng.randint(1, width+100), rng.randint(1, height+100)
        if x+w <= 0 or y+h <= 0:
            continue
        (cx, cy, cw, ch), _ = crop_rect((x,y,w,h), width, height)
        assert 0 <= cx < cx+cw <= width and 0 <= cy < cy+ch <= height
        assert cx <= max(0,x) and cy <= max(0,y)
        assert cx+cw >= min(width,x+w) and cy+ch >= min(height,y+h)
    for box in [(0,0,0,1), (200,200,10,10), (float("nan"),0,1,1)]:
        try:
            crop_rect(box,100,100)
            assert False, box
        except ValueError:
            pass
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        probe = CropProbe(output_dir=str(root / "probe"), sample_every_frames=1,
                          max_samples=6, report_seconds=1000)
        for _ in range(8):
            probe.process_frame(Frame([None, 1, 2, 3]))
        sampled = [json.loads(path.read_text())["tracker_id"]
                   for path in sorted(probe.root.glob("sample-*.json"))]
        assert sampled == [1, 2, 3, 1, 2, 3], sampled
        assert probe.stats["samples_saved"] == 6 and len(probe.sample_attempts) == 3
        probe.finalizer()
        producer = make(root, rate=0.01, burst=3, max_tracks=16, store_candidates=2)
        frame = Frame([0, 0] + list(range(1, 100)))
        assert producer.process_frame(frame)
        context = json.loads(frame.messages()[0])["crop_context"]
        assert context["source_frame_id"] == 42
        for n in range(10):
            assert producer.process_frame(Frame(list(range(100+n*100,200+n*100))))
        producer.close()
        assert not producer.worker.is_alive()
        assert len(producer.tracks) <= 16
        assert producer.control["bytes"] == 0 and not producer.pending
        assert producer.stats["saved"] == 3 and producer.store.count == 2, producer.stats
        assert producer.stats["writer_errors"] == 0 and producer.store.evicted == 1, producer.stats
        assert producer.stats["track_capacity"] > 0
        assert producer.stats["duplicate_track_roi"] == 1
        for image in producer.store.root.glob("*.jpg"):
            path = image.with_suffix('.json')
            event = json.loads(path.read_text())
            pixels = cv2.imread(str(path.with_suffix(".jpg")))
            assert pixels is not None and pixels.max() > 100
            assert event["tracking_session_id"] == context["tracking_session_id"]
            assert event["tracker_id"] != event["region_id"]
            assert event["crop_rect"][2] > event["detection_rect"][2]
        producer = make(root)
        empty = Frame([])
        empty.text = [json.dumps({"resolution":{"width":120,"height":100}})]
        assert producer.process_frame(empty)
        producer.close()
        assert not producer.stats["callback_errors"] and empty.maps == 0
        producer = make(root, queue_bytes=1)
        frame = Frame([1])
        producer.process_frame(frame)
        producer.close()
        assert frame.maps == 0 and producer.stats["queue_limited"] == 1
        producer = make(root)
        producer.process_frame(Frame([1], broken=True))
        producer.close()
        assert producer.stats["callback_errors"] == 1
        assert producer.control["bytes"] == 0 and not producer.pending
        producer = make(root)
        worker = producer.worker
        ref = weakref.ref(producer)
        del producer
        gc.collect()
        assert ref() is None and not worker.is_alive(), "callback lifecycle leaks worker"
        settings = Settings(output_dir=str(root / "outbox"), mqtt_host="unused")
        store = Store(settings)
        cid = uuid.uuid4().hex
        event = {"storage_key": cid+".jpg", "expires_at": time.time()+100}
        store.save(event, b"test")
        store.close()
        store = Store(settings)
        assert store.count == 1 and (store.root / (cid+".pending")).exists()
        class Result:
            rc = 0
            ack = False
            def is_published(self):
                return self.ack
        class Client:
            calls = 0
            result = Result()
            def is_connected(self):
                return True
            def publish(self, *args, **kwargs):
                self.calls += 1
                return self.result
        client = Client()
        store.publish_one(client)
        store.publish_one(client)
        assert client.calls == 1
        client.result.ack = True
        assert store.publish_one(client)
        assert (store.root / (cid+".json")).exists()
        event["expires_at"] = 0
        (store.root / (cid+".json")).write_text(json.dumps(event))
        store.maintain()
        assert store.count == 0
        store.close()
    print("PASS: probe track diversity, contextual geometry, churn/rate/storage limits, correlation, owned pixels, map failures, lifecycle, durable outbox and expiry")


def live(output):
    """Real pinned-image NPU/VA probe + producer + MQTT subscriber, isolated from REST pipelines."""
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    from gstgva import VideoFrame
    import paho.mqtt.client as mqtt
    Gst.init(None)
    root = Path(output) / uuid.uuid4().hex
    root.mkdir(parents=True)
    events, messages = [], []
    connected = __import__("threading").Event()
    client = mqtt.Client(client_id="crop-check-"+uuid.uuid4().hex)
    client.on_connect = lambda c,u,f,rc: c.subscribe("detections/snips", qos=1)
    client.on_subscribe = lambda c,u,mid,qos: connected.set()
    client.on_message = lambda c,u,msg: events.append(json.loads(msg.payload))
    client.connect("mqtt",1883)
    client.loop_start()
    assert connected.wait(5), "MQTT subscription not ready"
    scripts = Path(__file__).resolve().parent
    common = ("filesrc location=/home/pipeline-server/resources/videos/classroom.avi ! parsebin ! "
              "vah264dec ! video/x-raw(memory:VAMemory) ! "
              "gvadetect model=/home/pipeline-server/yolo_models/yolo26s/INT8/yolo26s.xml "
              "device=NPU pre-process-backend=va ! gvatrack tracking-type=zero-term-imageless config=tracking_per_class=false ! "
              "gvametaconvert add-empty-results=true ! ")
    def execute(callback, seconds):
        pipeline = Gst.parse_launch(common + callback + " ! appsink name=check emit-signals=true sync=true")
        sink = pipeline.get_by_name("check")
        def receive(sink):
            sample = sink.emit("pull-sample")
            frame = VideoFrame(sample.get_buffer(), caps=sample.get_caps())
            for text in frame.messages():
                value = json.loads(text)
                if "crop_context" in value:
                    messages.append(value)
            return Gst.FlowReturn.OK
        sink.connect("new-sample",receive)
        try:
            pipeline.set_state(Gst.State.PLAYING)
            error = pipeline.get_bus().timed_pop_filtered(int(seconds*Gst.SECOND), Gst.MessageType.ERROR)
            if error:
                raise AssertionError(error.parse_error())
        finally:
            pipeline.set_state(Gst.State.NULL)
    try:
        kwarg = json.dumps({"output_dir":str(root / "probe"), "max_samples":3,
                            "sample_every_frames":15,"report_seconds":1})
        execute(f"gvapython module={scripts}/crop_probe.py class=CropProbe kwarg={json.dumps(kwarg)}", 5)
        config = root / "config.json"
        config.write_text(json.dumps({"output_dir":str(root / "crops"),"mqtt_host":"mqtt",
                                     "stream_id":"crop_validation","report_seconds":1}))
        kwarg = json.dumps({"config":str(config)})
        execute(f"gvapython module={scripts}/crop_candidates.py class=CropCandidates kwarg={json.dumps(kwarg)}", 7)
        deadline = time.monotonic()+3
        while time.monotonic() < deadline and not events:
            time.sleep(0.05)
    finally:
        client.disconnect()
        client.loop_stop()
    probe_files = list((root / "probe").glob("*/*.jpg"))
    assert len(probe_files) == 3, "probe did not save its three real samples"
    probe_ids = [json.loads(path.with_suffix(".json").read_text())["tracker_id"]
                 for path in sorted(probe_files)]
    assert len(set(probe_ids)) == 3, f"probe repeated tracks despite multi-object fixture: {probe_ids}"
    for path in probe_files:
        assert cv2.imread(str(path)) is not None, f"invalid probe JPEG: {path}"
    assert messages, "no observation correlation metadata"
    session = messages[0]["crop_context"]["tracking_session_id"]
    received = [e for e in events if e.get("tracking_session_id") == session]
    assert received, "no matching crop events received over MQTT"
    crop_ids = sorted({e["tracker_id"] for e in received})
    assert len(crop_ids) >= 3, f"producer lacks track coverage: {crop_ids}"
    frames = {m["crop_context"]["frame_sequence"]:m for m in messages}
    for event in received:
        observation = frames[event["frame_sequence"]]
        assert observation["crop_context"]["generation"] == event["generation"]
        assert any(o.get("id") == event["tracker_id"] for o in observation["objects"])
        pixels = cv2.imread(str(root / "crops" / event["storage_key"]))
        assert pixels is not None and pixels.shape[:2] == (event["image_height"],event["image_width"])
        assert event["crop_rect"][2] >= event["detection_rect"][2]
    (root / "observations.json").write_text(json.dumps(messages,indent=2))
    (root / "received-events.json").write_text(json.dumps(received,indent=2))
    cpu_model = next((line.split(":",1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
                      if line.startswith("model name")), "unavailable")
    result = {"cpu_model":cpu_model, "device":"NPU",
              "probe_images":len(probe_files), "probe_track_ids":probe_ids,
              "producer_track_ids":crop_ids, "correlated_mqtt_events":len(received),
              "observation_frames":len(messages), "output_dir":str(root)}
    (root / "result.json").write_text(json.dumps(result,indent=2))
    print("PASS real NPU/VA crop path:",json.dumps(result))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", default="scripts/dlsps/.crop-live-evidence")
    args = parser.parse_args()
    if args.live:
        live(args.output)
    else:
        run()
