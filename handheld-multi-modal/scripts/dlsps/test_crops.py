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

import cv2
import numpy as np
from crop_candidates import CropCandidates, OwnershipLost, Settings, Store, crop_rect
from crop_probe import CropProbe


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
        self.text = [json.dumps({"objects": [{"id": i} for i in ids], "frame_id": 42})]
    def video_info(self):
        return SimpleNamespace(width=120, height=100, finfo=SimpleNamespace(name="BGR"))
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


def make(root, **overrides):
    config = root / (uuid.uuid4().hex + ".json")
    config.write_text(json.dumps(dict(output_dir=str(root / uuid.uuid4().hex),
        confirm_seconds=0, min_observations=1, report_seconds=1000, **overrides)))
    return CropCandidates(str(config))


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
                        for operation in (old.store.heartbeat, old.store.maintain,
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
            @contextmanager
            def data(self):
                entered.set()
                assert release.wait(5)
                with super().data() as pixels:
                    yield pixels
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


def run():
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
              "gvadetect model=/home/pipeline-server/yolo_models/yolo11s/INT8/yolo11s.xml "
              "device=NPU pre-process-backend=va ! gvatrack tracking-type=short-term-imageless ! "
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
    parser.add_argument("--output", default="/tmp/dlsps-crop-evidence")
    args = parser.parse_args()
    if args.live:
        live(args.output)
    else:
        run()
