#!/usr/bin/env python3
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Tiny anonymous MQTT broker for local development (no Docker/mosquitto needed).

  pip install amqtt && python3 tools/dev_broker.py [--port 1884]
"""
import argparse
import asyncio

from amqtt.broker import Broker


async def main(port):
    config = {
        "listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
        "sys_interval": 0,
        "auth": {"allow-anonymous": True, "plugins": ["auth_anonymous"]},
        "topic-check": {"enabled": False},
    }
    broker = Broker(config)
    await broker.start()
    print(f"dev broker listening on 127.0.0.1:{port}", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=1884)
    asyncio.run(main(ap.parse_args().port))
