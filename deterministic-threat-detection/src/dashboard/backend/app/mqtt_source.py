# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Subscribes to the inference topics and feeds the latency tracker.

Network latency = local receive time - the send timestamp stamped on machine 2/3 right before
the MQTT publish; processing time = send - capture. Both clocks are PTP-synchronised, so no
offset correction is applied.
"""

import json
import logging
import time

import paho.mqtt.client as mqtt

log = logging.getLogger("mqtt_source")


def source_name(topic):
    """tsn_demo/Cam_x1_0/inference -> Cam_x1_0"""
    parts = topic.split("/")
    return parts[1] if len(parts) >= 3 else topic


def extract(payload):
    """Return (capture_ts, send_ts, seq) from an edge message; missing values are None.

    capture_ts: when the frame entered the pipeline; send_ts: just before the MQTT publish.
    Returns (None, None, None) when neither timestamp is present.
    """
    meta = payload.get("metadata")
    if not isinstance(meta, dict):
        meta = {}

    def num(key):
        v = meta.get(key, payload.get(key))
        return float(v) if isinstance(v, (int, float)) else None

    capture, send = num("ptp_timestamp"), num("send_timestamp")
    seq = meta.get("seq", payload.get("seq"))
    return capture, send, seq if isinstance(seq, int) else None


class MqttSource:
    def __init__(self, tracker, host, port, topic):
        self.tracker = tracker
        self.host, self.port, self.topic = host, port, topic
        self.connected = False
        self.bad_messages = 0
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=10)

    def start(self):
        self.client.connect_async(self.host, self.port, keepalive=3600)  # see edge/latency_stamp.py
        self.client.loop_start()

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            self.connected = True
            client.subscribe(self.topic, qos=0)
            log.info("connected to %s:%s, subscribed to %s", self.host, self.port, self.topic)
        else:
            log.error("MQTT connect failed: %s", reason_code)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = False
        log.warning("MQTT disconnected (%s)", reason_code)

    def _on_message(self, client, userdata, message):
        recv = time.time()
        try:
            payload = json.loads(message.payload)
        except ValueError:
            self.bad_messages += 1
            return
        capture, send, seq = extract(payload)
        if send is not None:
            network = recv - send
            processing = send - capture if capture is not None else None
        elif capture is not None:  # older message format: only the capture time is known
            network, processing = recv - capture, None
        else:
            self.bad_messages += 1
            return
        self.tracker.add(source_name(message.topic), network, seq=seq, recv_time=recv,
                         processing_s=processing)
