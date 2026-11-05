# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Two gvapython classes that feed SceneScape from a DL Streamer pipeline.

Reduced from SceneScape's single-file adapter (sscape_adapter.py, smart-intersection recipe): detection policy
only, no NTP client (the clock comes from PTP via phc2sys), no calibration image. Our additions: the SEI frame
number is forwarded as "frame" (the live HOTA needs it), and the Basler hardware timestamp is used when present.

  PostDecodeTimestampCapture  after decode: stamps capture time and measured fps
  PostInferenceDataPublish    after gvametaconvert: publishes scenescape/data/camera/<id>, and a JPEG of the
                              annotated frame on scenescape/image/camera/<id> when asked ("getimage")
"""
import base64
import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

import cv2
import gi
import paho.mqtt.client as mqtt

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402
from utils import publisher_utils as utils  # noqa: E402  (shipped in the pipeline-server image)

ROOT_CA = os.environ.get("ROOT_CA", "/run/secrets/certs/scenescape-ca.pem")
BROKER = os.environ.get("MQTT_HOST", "broker.scenescape.intel.com")
DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"
HW_CAPS = Gst.Caps.from_string("timestamp/x-hardware")  # camera PTP time attached by gencamsrc (Basler)


def iso(epoch):
  return f"{datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(DATETIME_FORMAT)[:-3]}Z"


class PostDecodeTimestampCapture:
  def __init__(self):
    self.fps = 5.0
    self.frames = 0
    self.last_fps_ts = None

  def processFrame(self, frame):
    now = time.time()
    self.frames += 1
    if self.last_fps_ts is None:
      self.last_fps_ts = now
    if now - self.last_fps_ts > 1:
      self.fps = self.fps * 0.75 + 0.25 * self.frames / (now - self.last_fps_ts)
      self.last_fps_ts = now
      self.frames = 0

    stamp = iso(now)
    hw = frame._VideoFrame__buffer.get_reference_timestamp_meta(HW_CAPS)
    if hw and hw.timestamp > 0:  # 0 = the camera clock is not set yet
      stamp = iso(hw.timestamp / 1e9)
    frame.add_message(json.dumps({"postdecode_timestamp": stamp, "timestamp_for_next_block": now, "fps": self.fps}))
    return True


class PostInferenceDataPublish:
  def __init__(self, cameraid, detection_labels="person"):
    self.cameraid = cameraid
    self.labels = {label for label in detection_labels.split(",") if label}
    self.publish_image = False
    self.log = logging.getLogger("SSCAPE_ADAPTER")
    self.client = mqtt.Client()
    self.client.on_connect = lambda c, u, f, rc: c.subscribe(f"scenescape/cmd/camera/{cameraid}")
    self.client.on_message = lambda c, u, m: setattr(self, "publish_image", m.payload == b"getimage")
    if ROOT_CA and os.path.exists(ROOT_CA):
      self.client.tls_set(ca_certs=ROOT_CA)
    self.client.connect(BROKER, 1883, 120)
    self.client.loop_start()

  def objects(self, meta):
    out = defaultdict(list)
    detections = meta.get("objects", [])
    if not detections:  # frames without detections carry no resolution either
      return out
    width, height = meta["resolution"]["width"], meta["resolution"]["height"]
    for det in detections:
      box = det["detection"]["bounding_box"]
      xmin, ymin = int(box["x_min"] * width), int(box["y_min"] * height)
      xmax, ymax = int(box["x_max"] * width), int(box["y_max"] * height)
      label = det["detection"]["label"]
      if self.labels and label not in self.labels:
        continue
      cw, ch = (xmax - xmin) / 3, (ymax - ymin) / 4
      out[label].append({
        "category": label, "confidence": det["detection"]["confidence"], "id": len(out[label]) + 1,
        "center_of_mass": {"x": int(xmin + cw), "y": int(ymin + ch), "width": cw, "height": ch},
        "bounding_box_px": {"x": det["x"], "y": det["y"], "width": det["w"], "height": det["h"]}})
    return out

  def send_image(self, frame, data):
    with frame.data() as image:
      for items in data["objects"].values():
        for obj in items:
          box = obj["bounding_box_px"]
          cv2.rectangle(image, (int(box["x"]), int(box["y"])), (int(box["x"] + box["width"]),
                        int(box["y"] + box["height"])), (0, 0, 255), 4)
      cv2.putText(image, f"FPS {data['rate']:.1f}", (0, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
      _, jpeg = cv2.imencode(".jpg", image)
    self.client.publish(f"scenescape/image/camera/{self.cameraid}", json.dumps({
      "timestamp": data["timestamp"], "id": self.cameraid, "image": base64.b64encode(jpeg).decode("utf-8")}))
    self.publish_image = False

  def processFrame(self, frame):
    if not self.client.is_connected():
      return True
    meta = {}
    utils.get_gva_meta_messages(frame, meta)
    now = time.time()
    data = {
      "id": self.cameraid, "timestamp": meta["postdecode_timestamp"], "rate": float(meta["fps"]),
      "frame": meta.get("sei_frame_num"), "debug_timestamp_end": iso(now),
      "debug_processing_time": now - float(meta["timestamp_for_next_block"]), "objects": self.objects(meta)}
    if self.publish_image:
      self.send_image(frame, data)
    self.client.publish(f"scenescape/data/camera/{self.cameraid}", json.dumps(data))
    return True
