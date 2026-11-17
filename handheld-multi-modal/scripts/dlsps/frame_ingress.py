#!/usr/bin/env python3
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Frame arrival stamp for gvapython, placed right after decode.

It never maps pixels. CropCandidates consumes the stamp, so the published
observation carries per-frame pipeline latency and the decoded frame count,
and the Backend can report time to alert and frames dropped by leaky queues.
"""
import json
import time

KEY = "handheld_ingress"


class FrameIngress:
    def __init__(self):
        self.sequence = 0

    def process_frame(self, frame):
        self.sequence += 1
        frame.add_message(json.dumps({KEY: {
            "wall": time.time(), "mono_ns": time.monotonic_ns(), "sequence": self.sequence}}))
        return True
