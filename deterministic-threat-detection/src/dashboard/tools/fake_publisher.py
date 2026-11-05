#!/usr/bin/env python3
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Publishes fake DL Streamer inference messages to test the dashboard without hardware.

The latency model follows the dashboard's own controls (read from /api/status): it grows with
the injected load while Qbv is off and stays flat once Qbv is on.

  python3 tools/fake_publisher.py [--broker 127.0.0.1] [--port 1884] [--api http://127.0.0.1:8080]
"""
import argparse
import json
import random
import time
import urllib.request

import paho.mqtt.client as mqtt

SOURCES = ["Cam_x1_0", "Cam_x2_0"]
BASE_S = 0.0006  # a quiet network
PROCESSING_S = {"Cam_x1_0": 0.027, "Cam_x2_0": 0.047}  # decode + inference, differs per machine
FPS = 30


def controls(api):
    try:
        with urllib.request.urlopen(f"{api}/api/status", timeout=1) as r:
            body = json.load(r)
        return body["traffic"]["rate_mbps"], body["qbv"]["enabled"]
    except OSError:
        return 0, False


def latency(rate_mbps, qbv):
    if qbv:
        return BASE_S + random.uniform(0, 0.0005)
    load = min(rate_mbps / 1000.0, 1.0)
    queueing = (load ** 3) * 0.6 * random.uniform(0.3, 1.0)
    return BASE_S + queueing + random.uniform(0, 0.0005)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--broker", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1884)
    ap.add_argument("--api", default="http://127.0.0.1:8080")
    a = ap.parse_args()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.connect(a.broker, a.port)
    client.loop_start()
    seq = {name: 0 for name in SOURCES}
    rate, qbv, next_poll = 0, False, 0.0
    next_t = time.perf_counter()
    while True:
        now = time.time()
        if now >= next_poll:
            rate, qbv = controls(a.api)
            next_poll = now + 0.5
        for name in SOURCES:
            lat = latency(rate, qbv)
            proc = PROCESSING_S[name]
            now_s = time.time()
            msg = {"objects": [], "metadata": {"ptp_timestamp": now_s - lat - proc,
                                               "send_timestamp": now_s - lat, "seq": seq[name]}}
            seq[name] += 1
            client.publish(f"tsn_demo/{name}/inference", json.dumps(msg), qos=0)
        next_t += 1.0 / FPS
        time.sleep(max(0.0, next_t - time.perf_counter()))


if __name__ == "__main__":
    main()
