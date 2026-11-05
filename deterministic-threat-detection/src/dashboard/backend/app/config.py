# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Runtime settings, read from environment variables."""

import os


def _int(name, default):
    return int(os.environ.get(name, default))


def _float(name, default):
    return float(os.environ.get(name, default))


# --- lab addresses --------------------------------------------------------------------------------
# Every address comes from lab.env (passed in through the environment); this file contains none.
SSH_USER = os.environ.get("SSH_USER", "")
M1_LAB_IP = os.environ.get("M1_LAB_IP", "")
M2_LAB_IP = os.environ.get("M2_LAB_IP", "")
M3_LAB_IP = os.environ.get("M3_LAB_IP", "")
M4_LAB_IP = os.environ.get("M4_LAB_IP", "")
M1_NOISE_IP = os.environ.get("M1_NOISE_IP", "")
M4_NOISE_IP = os.environ.get("M4_NOISE_IP", "")


def _target(host):
    """user@host for ssh, or '' while the lab configuration is incomplete."""
    return f"{SSH_USER}@{host}" if SSH_USER and host else ""


def require(mode, **needed):
    """Fail at startup with a message that names the missing lab.env entries."""
    missing = [name for name, value in needed.items() if not value]
    if missing:
        raise ValueError(f"{mode} needs {', '.join(missing)} - fill them in lab.env (see lab.env.example)")


MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = _int("MQTT_PORT", 1883)
# Topics are tsn_demo/<source>/inference; the middle part names the chart series.
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "tsn_demo/+/inference")

# Rolling window used for the KPI numbers (mean / p95 / max).
STATS_WINDOW_S = _float("STATS_WINDOW_S", 10)
# How much history the server keeps per source for the chart.
HISTORY_S = _float("HISTORY_S", 120)
# A source with no message for this long is shown as stalled.
STALL_AFTER_S = _float("STALL_AFTER_S", 2)
# Messages with an absurd latency (clock not synchronised) are flagged, not plotted.
MAX_PLAUSIBLE_LATENCY_S = _float("MAX_PLAUSIBLE_LATENCY_S", 60)

# "sim" only records the request; "iperf" drives iperf3 from the injector host.
TRAFFIC_MODE = os.environ.get("TRAFFIC_MODE", "sim")
# iperf3 -b counts UDP payload; with default packets 960M is already ~1 Gbit/s on the wire, so
# the top of the slider saturates the injector's NIC (the point: the noise fills the whole link).
TRAFFIC_MAX_MBPS = _int("TRAFFIC_MAX_MBPS", 1000)
# "sim" fakes the switch; "moxa" drives it through the Moxa scripts on a control host.
SWITCH_MODE = os.environ.get("SWITCH_MODE", "sim")
SWITCH_PORT = _int("SWITCH_PORT", 1)
# Machine-2 on the lab network: its port and NIC carry none of the injected load.
SWITCH_SSH_TARGET = os.environ.get("SWITCH_SSH_TARGET") or _target(M2_LAB_IP)
# Management address of the switch, as machine-2 reaches it (passed to the Moxa scripts as --host)
SWITCH_IP = os.environ.get("SWITCH_IP", "")
SWITCH_REMOTE_DIR = os.environ.get("SWITCH_REMOTE_DIR", "~/moxa")
# Reads are always allowed; enabling/disabling the shaper needs SWITCH_ALLOW_WRITE=1.
SWITCH_ALLOW_WRITE = os.environ.get("SWITCH_ALLOW_WRITE", "0") == "1"
# Gate control list used for "Qbv on": INTERVAL_US:QUEUES per slot (same as port 1 today).
SWITCH_SCHEDULE = os.environ.get("SWITCH_SCHEDULE", "90000:6,1,0 10000:5,1,0")

TRAFFIC_SERVER_IP = os.environ.get("TRAFFIC_SERVER_IP") or M1_NOISE_IP
# Load agent on machine-4: HTTP on its *lab* address (never a TSN VLAN, so it stays reachable under load);
# the iperf3 traffic itself leaves machine-4 via VLAN 5.
TRAFFIC_AGENT_URL = os.environ.get("TRAFFIC_AGENT_URL") or (f"http://{M4_LAB_IP}:{_int('LOAD_PORT', 8099)}" if M4_LAB_IP else "")
IPERF3_BIN = os.environ.get("IPERF3_BIN", "iperf3")
# The agent stops the load by itself this long after the last refresh from the dashboard.
TRAFFIC_TTL_S = _int("TRAFFIC_TTL_S", 30)

# --- use case 2: live HOTA from the SceneScape stack on this machine ---
SCENE_ENABLED = os.environ.get("SCENE_ENABLED", "1") == "1"
SCENE_MQTT_HOST = os.environ.get("SCENE_MQTT_HOST", "127.0.0.1")
SCENE_MQTT_PORT = _int("SCENE_MQTT_PORT", 1883)
SCENE_CA = os.environ.get("SCENE_CA", "/certs/scenescape-ca.pem")
GT_PATH = os.environ.get("GT_PATH", "/data/gtLoc.json")
HOTA_WINDOW_S = _float("HOTA_WINDOW_S", 10)
# Camera message -> tracker update delay used to pair them (position error vs the ground truth is flat for 0-200 ms)
HOTA_LAG_S = _float("HOTA_LAG_S", 0.08)

# Restarts a camera's pipeline when its instance ended (e.g. after the relay's RTSP connection reset).
WATCHDOG_ENABLED = os.environ.get("WATCHDOG_ENABLED", "1") == "1"
DLSPS_URL = os.environ.get("DLSPS_URL", "http://127.0.0.1:8082")
WATCHDOG_CAMERAS = {"Cam_x1_0": "hota-cam1", "Cam_x2_0": "hota-cam2"}

# System load: name=url pairs of the machines' metrics-manager streams (set in the local .env)
METRICS_PORT = _int("METRICS_PORT", 19090)


def _default_metrics_sources():
    parts = [f"machine-1=http://127.0.0.1:{METRICS_PORT}/metrics/stream"]  # this machine's own, on loopback
    for name, host in (("machine-2", M2_LAB_IP), ("machine-3", M3_LAB_IP), ("machine-4", M4_LAB_IP)):  # the other machines (lab network)
        if host:
            parts.append(f"{name}=http://{host}:{METRICS_PORT}/metrics/stream")
    return ",".join(parts)


# UC2 camera source: video (files via RTSP from machines 2/3), or basler (cameras on the switch)
SCENE_SOURCE = os.environ.get("SCENE_SOURCE", "video")
METRICS_SOURCES = os.environ.get("METRICS_SOURCES") or _default_metrics_sources()
