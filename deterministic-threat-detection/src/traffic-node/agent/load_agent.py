# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Load agent for machine-4: runs the iperf3 UDP client that floods VLAN 5 on the dashboard's request.

The API listens on the LAB address only (LOAD_BIND), never on a TSN VLAN: while the injected load fills the
TSN link, the control path must still work. The load itself leaves from CLIENT_IP (machine-4's VLAN 5 address).

  POST /rate  {"mbps": 300, "ttl_s": 30}   start / change / stop (0) the load; the load also stops by itself
                                           ttl_s after the last POST, so a crashed dashboard cannot leave it on
  GET  /state                              {"rate_mbps", "running", "returncode", "ttl_left_s"}
"""
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIND = os.environ.get("LOAD_BIND", "127.0.0.1")
PORT = int(os.environ.get("LOAD_PORT", "8099"))
SERVER_IP = os.environ["SERVER_IP"]  # machine-1, VLAN 5 (where "iperf3 -s" runs)
CLIENT_IP = os.environ["CLIENT_IP"]  # machine-4, VLAN 5
MAX_MBPS = int(os.environ.get("MAX_MBPS", "1000"))
MAX_TTL_S = 300
RUN_S = 3600  # upper bound of a single iperf3 run, also re-armed by every POST of the same rate


class Load:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.rate = 0
        self.deadline = 0.0
        self.returncode = None

    def _stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc, self.rate = None, 0

    def set(self, mbps, ttl_s):
        mbps = max(0, min(int(mbps), MAX_MBPS))
        with self.lock:
            if mbps == 0:
                self._stop()
                return
            self.deadline = time.time() + max(1, min(int(ttl_s), MAX_TTL_S))
            if mbps == self.rate and self.proc and self.proc.poll() is None:
                return  # a refresh: only the deadline moves
            self._stop()
            self.proc = subprocess.Popen(
                ["iperf3", "-c", SERVER_IP, "-B", CLIENT_IP, "-u", "-b", f"{mbps}M", "-t", str(RUN_S), "-f", "m"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.rate, self.returncode = mbps, None

    def state(self):
        with self.lock:
            running = bool(self.proc and self.proc.poll() is None)
            if self.proc and not running:
                self.returncode = self.proc.returncode
            return {"rate_mbps": self.rate if running else 0, "running": running, "returncode": self.returncode,
                    "ttl_left_s": max(0, round(self.deadline - time.time(), 1)) if running else 0}

    def watchdog(self):
        while True:
            time.sleep(1)
            with self.lock:
                if self.proc and time.time() > self.deadline:
                    self._stop()


load = Load()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(200 if self.path == "/state" else 404, load.state() if self.path == "/state" else {"error": "not found"})

    def do_POST(self):
        if self.path != "/rate":
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            load.set(body["mbps"], body.get("ttl_s", 30))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            return self._send(400, {"error": f"{type(exc).__name__}: {exc}"})
        self._send(200, load.state())

    def log_message(self, fmt, *args):  # the dashboard polls; keep the container log readable
        pass


if __name__ == "__main__":
    threading.Thread(target=load.watchdog, daemon=True).start()
    print(f"load agent on {BIND}:{PORT}: iperf3 {CLIENT_IP} -> {SERVER_IP}, max {MAX_MBPS} Mbit/s", flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
