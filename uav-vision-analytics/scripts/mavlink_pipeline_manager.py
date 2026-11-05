# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""
Starts and stops the DL Streamer pipelines of the configured cameras when the UAV is armed:
  - ARMED    -> read cameras-pymavlink.yaml and start one pipeline per enabled camera (one after the other)
  - DISARMED -> delete the pipelines that were started

The vehicle's armed state comes from MAVLink (default) or, in the UAV Mission Compute SDK stack (ARM_SOURCE=mqtt), from
the SDK's MQTT telemetry topic uav/<UAV_ID>/telemetry/status ({"armed": true|false}). Cameras whose source is an RTSP
stream (rtsp: in the file) are only started once that stream can be read.

The cameras come from a YAML file (default /home/pipeline-server/configs/cameras-pymavlink.yaml, see configs/cameras-pymavlink.yaml and
camera_config.py). The file is re-read every time the vehicle is armed; an edit made while the vehicle is armed
applies at the next arming. A file with errors never replaces a working configuration.

Usage:
  python3 pipeline_manager.py                 wait for the vehicle, start the cameras on ARM
  python3 pipeline_manager.py --start-now     start the cameras immediately, no vehicle needed (Ctrl+C stops them)
  python3 pipeline_manager.py --status        show what cameras-pymavlink.yaml asks for next to what is running
  python3 pipeline_manager.py --config FILE   use another configuration file
"""

import argparse
import json
import os
import queue
import signal
import subprocess
import sys
import time

# Must be set before pymavlink is imported: it packs outgoing messages as MAVLink 1 until it has
# received a MAVLink 2 packet, which breaks the camera protocol's extension fields.
os.environ.setdefault("MAVLINK20", "1")

import requests  # noqa: E402  pylint: disable=wrong-import-position

import camera_config  # noqa: E402  pylint: disable=wrong-import-position

# ── Constants ─────────────────────────────────────────────────────────────────

CONNECTION_STRING   = os.getenv("MAVLINK_CONNECTION", "udpin:0.0.0.0:14541")
PIPELINE_SERVER_URL = os.getenv("PIPELINE_SERVER_URL", "http://localhost:8081").rstrip("/")
PIPELINE_BASE_URL   = f"{PIPELINE_SERVER_URL}/pipelines/user_defined_pipelines"
PIPELINE_DELETE_URL = f"{PIPELINE_SERVER_URL}/pipelines/{{instance_id}}"
PIPELINE_STATUS_URL = f"{PIPELINE_SERVER_URL}/pipelines/{{instance_id}}/status"
PIPELINE_INFO_URL   = f"{PIPELINE_SERVER_URL}/pipelines/{{instance_id}}"
ALL_STATUS_URL      = f"{PIPELINE_SERVER_URL}/pipelines/status"
# Loading models (GPU/NPU compile, VLM) can take a while after a (re)start, so the
# start request and the wait for RUNNING get generous timeouts.
START_REQUEST_TIMEOUT = 60
START_READY_TIMEOUT   = 180
# DELETE returns before the pipeline has fully stopped, and its RTSP stream path stays registered until then.
# A new pipeline on that path fails with "RTSP Stream ... already exists", so wait for the stop and retry.
STOP_WAIT_TIMEOUT     = 45
STREAM_RELEASE_DELAY = 8      # the RTSP stream paths are released some seconds after the pipelines report stopped
START_ATTEMPTS        = 4
START_RETRY_DELAY     = 6
# Detection-box line thickness (1-10, gvawatermark's own default is 2).
BBOX_THICKNESS = os.getenv("BBOX_THICKNESS", "6").strip()
ARM_SOURCE     = os.getenv("ARM_SOURCE", "mavlink").lower()          # mavlink | mqtt
MQTT_BROKER    = os.getenv("MQTT_BROKER_HOST", "host.docker.internal")
MQTT_PORT      = int(os.getenv("MQTT_BROKER_PORT", "1883"))
RTSP_PROBE_RETRIES = 3
RTSP_PROBE_TIMEOUT = 5
HOST_IP        = os.getenv("HOST_IP", "127.0.0.1")
RTSP_PORT      = os.getenv("RTSP_PORT", "8555")


def log(message: str) -> None:
    print(message, flush=True)


def _install_signal_handlers() -> None:
    """Ctrl+C and `docker stop` (SIGTERM) both lead to one clean shutdown."""
    def _stop(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)


def _shutdown() -> None:
    """Stop the started pipelines exactly once; further signals during the clean-up are ignored."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    log("\nStopping the cameras before exit.")
    stop_cameras()


# ── Configuration (reloaded from the file, never replaced by a broken one) ─────

class ActiveConfig:
    """The last good plan, refreshed from the file when it changes or on demand."""

    def __init__(self, path: str):
        self.path = path
        self.plans: list = []
        self._mtime = None

    def _stamp(self):
        try:
            return os.stat(self.path).st_mtime_ns
        except OSError:
            return None

    def refresh(self, force: bool = False, quiet: bool = False) -> bool:
        """Re-read the file if it changed (or force). True if a (new) good plan is active."""
        stamp = self._stamp()
        if not force and stamp == self._mtime:
            return False
        self._mtime = stamp
        try:
            config = camera_config.load_config(self.path)
            unavailable = []
            plans = camera_config.build_plan(config, thickness=BBOX_THICKNESS, unavailable=unavailable,
                                            pipelines=camera_config.load_pipeline_names())
        except camera_config.ConfigError as exc:
            log(f"[config] {self.path} has errors, "
                + ("keeping the previous configuration:" if self.plans else "nothing to start:"))
            for line in str(exc).splitlines():
                log(f"[config]   {line}")
            return False
        for line in unavailable:
            log(f"[config] WARNING {line}; left out until the next arming")
        changed = [p.request for p in plans] != [p.request for p in self.plans]
        self.plans = plans
        if changed or not quiet:
            log(f"[config] {self.path}: {len(plans)} camera(s) enabled")
            for line in camera_config._table(plans).splitlines():  # pylint: disable=protected-access
                log(f"[config]   {line}")
        return changed


# ── Pipeline lifecycle ────────────────────────────────────────────────────────

running_instances: dict[str, str] = {}      # camera id -> pipeline instance id


def _wait_until_running(name: str, instance_id: str) -> bool:
    """Block until the instance reports RUNNING; False on ERROR/ABORTED or timeout."""
    url = PIPELINE_STATUS_URL.format(instance_id=instance_id)
    deadline = time.time() + START_READY_TIMEOUT
    state = None
    while time.time() < deadline:
        try:
            state = requests.get(url, timeout=5).json().get("state")
        except (requests.RequestException, ValueError):
            state = None
        if state == "RUNNING":
            log(f"[pipeline] '{name}' is RUNNING")
            return True
        if state in ("ERROR", "ABORTED"):
            log(f"[pipeline] '{name}' failed to start (state {state})")
            return False
        time.sleep(1)
    log(f"[pipeline] '{name}' not RUNNING after {START_READY_TIMEOUT}s (last state {state})")
    return False


def _start_one(plan) -> bool:
    """Start one camera's pipeline; True once it is RUNNING. Retries, because a stream path may still be held."""
    for attempt in range(1, START_ATTEMPTS + 1):
        try:
            resp = requests.post(f"{PIPELINE_BASE_URL}/{plan.pipeline}", json=plan.request,
                                 headers={"Content-Type": "application/json"}, timeout=START_REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            log(f"[pipeline] Failed to start camera '{plan.camera_id}' ({plan.pipeline}): {exc}")
            return False
        log(f"[pipeline] Start camera '{plan.camera_id}' with '{plan.pipeline}' → {resp.status_code}: {resp.text.strip()}")
        if resp.status_code != 200:
            log("[pipeline]   (the pipeline server's own log has the reason; a bad request is not retried)")
            return False
        instance_id = resp.text.strip().strip('"')
        running_instances[plan.camera_id] = instance_id
        # One pipeline at a time: starting several VLM pipelines at once made their GPU model
        # loads collide and crashed the pipeline server.
        if _wait_until_running(plan.camera_id, instance_id):
            return True
        _delete(instance_id)
        running_instances.pop(plan.camera_id, None)
        if attempt < START_ATTEMPTS:
            delay = START_RETRY_DELAY * attempt
            log(f"[pipeline] Retrying camera '{plan.camera_id}' in {delay}s (attempt {attempt + 1} of {START_ATTEMPTS})")
            time.sleep(delay)
    log(f"[pipeline] Giving up on camera '{plan.camera_id}' after {START_ATTEMPTS} attempts")
    return False


def rtsp_available(url: str) -> bool:
    """True if ffprobe can read a video stream from the RTSP url (retried a few times)."""
    cmd = ["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "v:0", "-show_entries", "stream=codec_type",
           "-of", "default=noprint_wrappers=1", "-timeout", str(RTSP_PROBE_TIMEOUT * 1_000_000), url]
    for attempt in range(1, RTSP_PROBE_RETRIES + 1):
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=RTSP_PROBE_TIMEOUT + 2, check=False)
            if result.returncode == 0 and b"codec_type" in result.stdout:
                return True
        except (subprocess.TimeoutExpired, OSError) as exc:
            log(f"[rtsp-check] Error probing {url}: {exc}")
        if attempt < RTSP_PROBE_RETRIES:
            time.sleep(2)
    return False


def start_cameras(plans: list) -> None:
    """Start the pipeline of every planned camera, one after the other."""
    running_instances.clear()
    npu_device = os.getenv("NPU_DEVICE", "/dev/null")
    for plan in plans:
        if plan.pipeline.startswith("uav_object_detection_npu") and npu_device == "/dev/null":
            log(f"[pipeline] Skipping camera '{plan.camera_id}': NPU_DEVICE not available.")
            continue
        if plan.source_url and not rtsp_available(plan.source_url):
            log(f"[pipeline] Skipping camera '{plan.camera_id}': its RTSP source {plan.source_url} is not available.")
            continue
        _start_one(plan)


def _delete(instance_id: str) -> None:
    try:
        requests.delete(PIPELINE_DELETE_URL.format(instance_id=instance_id), timeout=10)
    except requests.RequestException:
        pass


def _wait_until_stopped(instance_id: str) -> bool:
    deadline = time.time() + STOP_WAIT_TIMEOUT
    while time.time() < deadline:
        try:
            resp = requests.get(PIPELINE_STATUS_URL.format(instance_id=instance_id), timeout=5)
            state = resp.json().get("state") if resp.status_code == 200 else "GONE"
        except (requests.RequestException, ValueError):
            state = None
        if state in ("ABORTED", "COMPLETED", "ERROR", "GONE"):
            return True
        time.sleep(1)
    return False


def stop_cameras() -> None:
    """DELETE all currently tracked pipeline instances."""
    if not running_instances:
        log("[pipeline] No running instances; nothing to stop.")
        return
    for camera_id, instance_id in list(running_instances.items()):
        try:
            resp = requests.delete(PIPELINE_DELETE_URL.format(instance_id=instance_id), timeout=10)
            log(f"[pipeline] Stop camera '{camera_id}' ({instance_id}) → {resp.status_code}")
        except requests.RequestException as exc:
            log(f"[pipeline] Failed to stop camera '{camera_id}' ({instance_id}): {exc}")
            continue
        if not _wait_until_stopped(instance_id):
            log(f"[pipeline] Camera '{camera_id}' did not report stopped within {STOP_WAIT_TIMEOUT}s")
    running_instances.clear()
    time.sleep(STREAM_RELEASE_DELAY)


def print_stream_urls(plans: list) -> None:
    log("RTSP streams available at:")
    for plan in plans:
        if plan.camera_id in running_instances:
            log(f"  rtsp://{HOST_IP}:{RTSP_PORT}/{plan.rtsp_path}   ({plan.camera_id})")
    for plan in plans:
        if plan.mqtt_topic and plan.camera_id in running_instances:
            log(f"  MQTT captions: {plan.mqtt_topic}   ({plan.camera_id})")


# ── Status: what the file asks for next to what runs ───────────────────────────

def show_status(path: str) -> int:
    try:
        unavailable = []
        plans = camera_config.build_plan(camera_config.load_config(path), thickness=BBOX_THICKNESS, unavailable=unavailable,
                                            pipelines=camera_config.load_pipeline_names())
    except camera_config.ConfigError as exc:
        log(f"{path} has errors:\n" + "\n".join("  " + line for line in str(exc).splitlines()))
        return 1
    try:
        instances = requests.get(ALL_STATUS_URL, timeout=10).json()
    except (requests.RequestException, ValueError) as exc:
        log(f"cannot reach the pipeline server at {PIPELINE_SERVER_URL}: {exc}")
        return 1
    by_path = {}
    for inst in instances:
        if inst.get("state") not in ("RUNNING", "QUEUED"):
            continue
        try:
            info = requests.get(PIPELINE_INFO_URL.format(instance_id=inst["id"]), timeout=10).json()
            frame = ((info.get("request") or {}).get("destination") or {}).get("frame") or {}
            if isinstance(frame, dict) and frame.get("path"):
                # request.pipeline.name is always the generic group "user_defined_pipelines"; the pipeline that
                # was started is request.pipeline.version
                pipeline = (info.get("request") or {}).get("pipeline") or {}
                by_path[frame["path"]] = (inst, pipeline.get("version") or pipeline.get("name"),
                                          (info.get("request") or {}).get("parameters") or {}, frame)
        except (requests.RequestException, ValueError):
            continue
    rows = [("camera", "asked for (file)", "running", "state", "fps", "matches")]
    for plan in plans:
        found = by_path.get(plan.rtsp_path)
        if not found:
            rows.append((plan.camera_id, plan.pipeline, "-", "not running", "-", "-"))
            continue
        inst, name, params, frame = found
        wanted_frame = plan.request["destination"]["frame"]
        same = (name == plan.pipeline
                and all(params.get(k) == v for k, v in plan.request["parameters"].items())
                and (frame.get("overlay", True) is not False) == (wanted_frame.get("overlay", True) is not False))
        rows.append((plan.camera_id, plan.pipeline, name or "?", inst.get("state", "?"),
                     f"{inst.get('avg_fps') or 0:.1f}", "yes" if same else "NO: restart to apply the file"))
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    for row in rows:
        log("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    for line in unavailable:
        log(f"WARNING {line}")
    return 0


# ── Vehicle ───────────────────────────────────────────────────────────────────

def _is_vehicle_heartbeat(msg, mavutil) -> bool:
    """True for a heartbeat sent by the vehicle's autopilot.

    Ground-control stations (QGroundControl: system 255, MAV_TYPE_GCS) also send
    heartbeats, and QGroundControl sets the "armed" bit in base_mode. Reacting to
    those would flip the armed state every heartbeat and start/stop the pipelines in a loop
    once a remote QGroundControl connects.
    """
    return (msg.type != mavutil.mavlink.MAV_TYPE_GCS
            and msg.autopilot != mavutil.mavlink.MAV_AUTOPILOT_INVALID)


def monitor_vehicle(active: ActiveConfig) -> None:
    from pymavlink import mavutil  # pylint: disable=import-outside-toplevel

    log(f"Connecting to {CONNECTION_STRING}...")
    master = mavutil.mavlink_connection(CONNECTION_STRING)
    log("Waiting for vehicle heartbeat...")
    msg = None
    while msg is None or not _is_vehicle_heartbeat(msg, mavutil):
        msg = master.recv_match(type="HEARTBEAT", blocking=True)
    vehicle_sysid = msg.get_srcSystem()
    log(f"Heartbeat received from System {vehicle_sysid} Component {msg.get_srcComponent()}")

    last_armed_state = None
    log("Monitoring ARMED/DISARMED status. Press Ctrl+C to stop.\n")
    try:
        while True:
            msg = master.recv_match(type="HEARTBEAT", blocking=True)
            if not msg or msg.get_srcSystem() != vehicle_sysid or not _is_vehicle_heartbeat(msg, mavutil):
                continue
            is_armed = bool(msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
            if is_armed != last_armed_state:
                if is_armed:
                    log("Vehicle Status: ARMED → starting cameras")
                    active.refresh(force=True, quiet=True)
                    start_cameras(active.plans)
                    print_stream_urls(active.plans)
                else:
                    log("Vehicle Status: DISARMED → stopping cameras")
                    stop_cameras()
                last_armed_state = is_armed
            elif active.refresh() and is_armed:
                log("[config] changed while armed: it applies the next time the vehicle is armed")
    except KeyboardInterrupt:
        _shutdown()


def armed_from_payload(payload) -> "bool | None":
    """The armed flag of an SDK status message ({"armed": true}), or None if it is not usable."""
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or "armed" not in data:
        return None
    return bool(data["armed"])


def monitor_mqtt(active: ActiveConfig) -> None:
    """Arm trigger of the UAV Mission Compute SDK stack: its status topic on the SDK's MQTT broker."""
    import paho.mqtt.client as mqtt  # pylint: disable=import-outside-toplevel

    topic = os.getenv("MQTT_ARM_TOPIC") or f"uav/{camera_config.uav_id()}/telemetry/status"
    states: "queue.Queue[bool]" = queue.Queue()
    client = mqtt.Client()
    client.on_connect = lambda c, userdata, flags, rc: (log(f"Connected to {MQTT_BROKER}:{MQTT_PORT} (result {rc}), watching {topic}"), c.subscribe(topic))
    client.on_message = lambda c, userdata, msg: (lambda armed: armed is None or states.put(armed))(armed_from_payload(msg.payload))
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()     # the pipelines start in this thread, so the network loop must not depend on it
    last_armed_state = None
    log("Monitoring ARMED/DISARMED status. Press Ctrl+C to stop.\n")
    try:
        while True:
            try:
                is_armed = states.get(timeout=1)
            except queue.Empty:
                if active.refresh() and last_armed_state:
                    log("[config] changed while armed: it applies the next time the vehicle is armed")
                continue
            if is_armed == last_armed_state:
                continue
            if is_armed:
                log("Vehicle Status: ARMED → starting cameras")
                active.refresh(force=True, quiet=True)
                start_cameras(active.plans)
                print_stream_urls(active.plans)
            else:
                log("Vehicle Status: DISARMED → stopping cameras")
                stop_cameras()
            last_armed_state = is_armed
    except KeyboardInterrupt:
        client.loop_stop()
        _shutdown()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Camera pipeline manager: starts the configured cameras when the vehicle is armed")
    parser.add_argument("--config", default=camera_config.DEFAULT_FILE, help="camera configuration file (default: %(default)s)")
    parser.add_argument("--start-now", action="store_true", help="start the cameras immediately, without waiting for the vehicle")
    parser.add_argument("--status", action="store_true", help="show the configured cameras next to what is running, then exit")
    args = parser.parse_args()

    if args.status:
        return show_status(args.config)

    _install_signal_handlers()
    active = ActiveConfig(args.config)
    active.refresh(force=True)
    if not active.plans:
        log(f"[config] No usable camera configuration in {args.config}; fix the file (make check-cameras) and start again.")
        return 2

    if args.start_now:
        start_cameras(active.plans)
        print_stream_urls(active.plans)
        log("Running without a vehicle. Press Ctrl+C to stop the cameras.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            _shutdown()
        return 0

    (monitor_mqtt if ARM_SOURCE == "mqtt" else monitor_vehicle)(active)
    return 0


if __name__ == "__main__":
    sys.exit(main())
