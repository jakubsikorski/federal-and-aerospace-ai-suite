# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Qbv (802.1Qbv Time-aware Shaper) control for the Moxa TSN switch.

Two implementations share one interface:
  SimSwitch   - pretends the switch needs a few seconds to apply a change.
  MoxaSwitch  - runs the Moxa Telnet scripts on a *control host* (machine-2) over ssh, so the
                switch session and its replies never use the shaped port (port 1). Writes are
                refused unless allow_write is set; reads are always allowed.
"""

import logging
import re
import subprocess
import threading
import time

log = logging.getLogger("switch")

FORBIDDEN_PORTS = {2}  # machine-2's port is the management path


def parse_tas(text):
    """Parse 'tas_config.py get' output into {'enabled', 'cycle_us', 'slots'}."""
    head = re.search(r"config-change=(\w+)\s+cycle=(\d+) ns", text)
    if not head:
        raise ValueError(f"cannot parse switch output: {text.strip()[:200]!r}")
    slots = [
        {"interval_us": int(m[1]), "queues": [int(q.lstrip("q")) for q in m[2].split(", ")]}
        for m in re.finditer(r"slot \d+: interval=(\d+) us gates=0x[0-9a-f]+ queues=(.*)", text)
    ]
    gate = re.search(r"gate toggle \(web page\): (on|off)", text)
    oper = re.search(r"operative: (active|not active)", text)
    gate_on = None if not gate else gate[1] == "on"
    active = None if not oper else oper[1] == "active"
    configured = head[1] == "True" and bool(slots)
    return {
        "config_change": head[1] == "True",
        "cycle_us": int(head[2]) // 1000,
        "slots": slots,
        "configured": configured,
        "gate": gate_on,  # the per-port toggle of the switch web page (the CLI cannot set it)
        "active": active,  # the schedule the switch is really running
        # 'enabled' means running: a configured schedule that is not operative shapes nothing
        "enabled": active if active is not None else configured,
    }


def parse_schedule(text):
    """'90000:6,1,0 10000:5,1,0' -> ['--slot', '90000:6,1,0', '--slot', '10000:5,1,0']"""
    slots = text.split()
    for s in slots:
        if not re.fullmatch(r"\d+(\.\d+)?:\d(,\d)*", s):
            raise ValueError(f"bad schedule slot '{s}', expected INTERVAL_US:Q,Q")
    args = []
    for s in slots:
        args += ["--slot", s]
    return args


class SimSwitch:
    def __init__(self, port=1, apply_delay_s=3.0):
        self.mode = "sim"
        self.port = port
        self.apply_delay_s = apply_delay_s
        self.enabled = False
        self.busy = False
        self.target = None
        self.error = None
        self._lock = threading.Lock()

    def start(self):
        pass

    def stop(self):
        pass

    def set_enabled(self, enabled):
        with self._lock:
            if self.busy:
                raise RuntimeError("a switch change is still being applied")
            self.busy = True
            self.target = bool(enabled)
        threading.Thread(target=self._apply, args=(bool(enabled),), daemon=True).start()

    def _apply(self, enabled):
        try:
            time.sleep(self.apply_delay_s)
            self.enabled = enabled
            self.error = None
        finally:
            self.target = None
            self.busy = False

    def state(self):
        return {"mode": self.mode, "port": self.port, "enabled": self.enabled, "busy": self.busy,
                "target": self.target, "error": self.error, "read_only": False, "slots": [],
                "cycle_us": None, "known": True}


class MoxaSwitch:
    """Reads the shaper state in the background; applies changes only when allowed."""

    def __init__(self, port, ssh_target, remote_dir, schedule, allow_write=False,
                 poll_interval_s=5.0, run=None, verify_timeout_s=60.0, verify_interval_s=2.0, switch_ip=None):
        if port in FORBIDDEN_PORTS:
            raise ValueError(f"port {port} is the management path and must not be configured")
        self.mode = "moxa"
        self.port = port
        self.ssh_target, self.remote_dir = ssh_target, remote_dir
        self.switch_ip = switch_ip  # management address as the control host reaches it (--host)
        self.schedule_args = parse_schedule(schedule)
        self.allow_write = allow_write
        self.poll_interval_s = poll_interval_s
        # The switch applies a change asynchronously (seconds), so the readback is repeated.
        self.verify_timeout_s, self.verify_interval_s = verify_timeout_s, verify_interval_s
        self._run = run or self._ssh
        self._tas = None
        self.busy = False
        self.target = None  # what the running change is trying to reach (None when idle)
        self.error = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def _ssh(self, args, timeout=60):
        host = ["--host", self.switch_ip] if self.switch_ip else []
        remote = f"cd {self.remote_dir} && python3 -I tas_config.py " + " ".join([*args, *host])
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", self.ssh_target, remote]
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if done.returncode != 0:
            raise RuntimeError((done.stderr or done.stdout).strip()[-300:] or f"exit {done.returncode}")
        return done.stdout

    def refresh(self):
        try:
            self._tas = parse_tas(self._run(["get", "--port", str(self.port)]))
            self.error = None
        except (RuntimeError, ValueError, subprocess.TimeoutExpired, OSError) as exc:
            self.error = f"switch read failed: {exc}"
            log.warning(self.error)

    def start(self):
        self.refresh()
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()

    def _poll(self):
        while not self._stop.wait(self.poll_interval_s):
            if not self.busy:
                self.refresh()

    def stop(self):
        self._stop.set()

    def set_enabled(self, enabled):
        if not self.allow_write:
            raise PermissionError("switch is in read-only mode (SWITCH_ALLOW_WRITE=0)")
        if enabled and self._tas is not None and self._tas.get("gate") is False:
            raise RuntimeError(
                f"the shaper toggle for port {self.port} is off in the switch web page; "
                "turn it on there first (the CLI cannot enable it)")
        with self._lock:
            if self.busy:
                raise RuntimeError("a switch change is still being applied")
            self.busy = True
            self.target = bool(enabled)
            self.error = None
        threading.Thread(target=self._apply, args=(bool(enabled),), daemon=True).start()

    def _apply(self, enabled):
        """Send the change, then read the switch back; 'busy' ends only after the readback, so the
        UI never shows a state the switch does not really have."""
        try:
            if enabled:
                args = ["set", "--port", str(self.port), *self.schedule_args]
            else:
                args = ["clear", "--port", str(self.port)]
            self._run(args, timeout=180)
            deadline = time.time() + self.verify_timeout_s
            while True:
                self._tas = parse_tas(self._run(["get", "--port", str(self.port)]))
                if bool(self._tas["enabled"]) == enabled:
                    break
                if time.time() >= deadline:
                    raise RuntimeError(
                        f"the switch did not reach the requested state within "
                        f"{self.verify_timeout_s:.0f} s (it reports Qbv "
                        f"{'on' if self._tas['enabled'] else 'off'})")
                time.sleep(self.verify_interval_s)
            self.error = None
        except (RuntimeError, ValueError, subprocess.TimeoutExpired, OSError) as exc:
            self.error = f"switch change failed: {exc}"
            log.error(self.error)
            self.refresh()  # show the real state, whatever it is
            if self.error is None:  # refresh succeeded: keep the failure message visible
                self.error = f"switch change failed: {exc}"
        finally:
            self.target = None
            self.busy = False

    def state(self):
        tas = self._tas or {}
        return {"mode": self.mode, "port": self.port, "enabled": bool(tas.get("enabled")),
                "gate": tas.get("gate"), "active": tas.get("active"), "configured": tas.get("configured"),
                "busy": self.busy, "target": self.target, "error": self.error,
                "read_only": not self.allow_write,
                "slots": tas.get("slots", []), "cycle_us": tas.get("cycle_us"),
                "known": self._tas is not None}


def create_switch(cfg):
    if cfg.SWITCH_MODE == "sim":
        return SimSwitch(cfg.SWITCH_PORT)
    if cfg.SWITCH_MODE == "moxa":
        cfg.require("SWITCH_MODE=moxa", SSH_USER=cfg.SSH_USER, M2_LAB_IP=cfg.SWITCH_SSH_TARGET, SWITCH_IP=cfg.SWITCH_IP)
        return MoxaSwitch(cfg.SWITCH_PORT, cfg.SWITCH_SSH_TARGET, cfg.SWITCH_REMOTE_DIR,
                          cfg.SWITCH_SCHEDULE, cfg.SWITCH_ALLOW_WRITE, switch_ip=cfg.SWITCH_IP)
    raise ValueError(f"unknown SWITCH_MODE '{cfg.SWITCH_MODE}'")
