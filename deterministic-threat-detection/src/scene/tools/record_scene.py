#!/usr/bin/env python3
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Record the camera detections and the tracked scene output from the SceneScape broker.

Writes JSON lines {"t": <local receive time>, "topic": ..., "msg": {...}} for later replay in tests
and for calibrating the HOTA frame alignment.

  python3 record_scene.py out.jsonl [seconds] [--host 127.0.0.1] [--port 1883] [--ca <scenescape-ca.pem>]
"""
import argparse
import json
import os
import ssl
import time

import paho.mqtt.client as mqtt

ap = argparse.ArgumentParser()
ap.add_argument("out")
ap.add_argument("seconds", nargs="?", type=float, default=90)
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=1883)
ap.add_argument("--ca", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "secrets", "certs", "scenescape-ca.pem"))
a = ap.parse_args()

f = open(a.out, "w")
n = {"camera": 0, "scene": 0}


def on_connect(c, u, flags, rc, props=None):
    c.subscribe([("scenescape/data/camera/+", 0), ("scenescape/regulated/scene/+", 0)])


def on_message(c, u, m):
    t = time.time()
    try:
        msg = json.loads(m.payload)
    except ValueError:
        return
    kind = "camera" if "/camera/" in m.topic else "scene"
    n[kind] += 1
    f.write(json.dumps({"t": t, "topic": m.topic, "msg": msg}) + "\n")


c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
c.tls_set(ca_certs=a.ca, cert_reqs=ssl.CERT_REQUIRED)
c.tls_insecure_set(True)  # certificate is for broker.scenescape.intel.com
c.on_connect, c.on_message = on_connect, on_message
c.connect(a.host, a.port)
c.loop_start()
time.sleep(a.seconds)
c.loop_stop()
f.close()
print(f"recorded {n['camera']} camera + {n['scene']} scene messages in {a.seconds:.0f} s -> {a.out}")
