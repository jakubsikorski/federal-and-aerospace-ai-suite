#!/usr/bin/env python3
"""Fetch the current mode of a port on a Moxa TSN-G5000 switch over Telnet.

Runs `show interfaces ethernet 1/<port>` and prints the link/mode fields.
Read-only: no configuration commands are sent.

Usage: get_port_mode.py [--host IP] [--port N] [--user U] [--password P] [--json]
The password may also be supplied via the MOXA_PASSWORD environment variable.
"""
import argparse
import json
import os
import re
import socket
import sys
import time

IAC, DONT, DO, WONT, WILL = 255, 254, 253, 252, 251
MORE = "--More--"
PROMPT_RE = re.compile(r"\n?\S+# $")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class Switch:
    def __init__(self, host, timeout=10):
        self.host = host
        self.sock = socket.create_connection((host, 23), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buf = ""

    def _recv(self):
        data = self.sock.recv(4096)
        if not data:
            raise ConnectionError("connection closed by switch")
        out = bytearray()
        i = 0
        while i < len(data):
            if data[i] == IAC and i + 2 < len(data):
                cmd, opt = data[i + 1], data[i + 2]
                if cmd in (DO, DONT):  # refuse every option
                    self.sock.sendall(bytes([IAC, WONT, opt]))
                elif cmd in (WILL, 251, 252):
                    self.sock.sendall(bytes([IAC, DONT, opt]))
                i += 3
            else:
                out.append(data[i])
                i += 1
        return out.decode(errors="replace")

    def expect(self, patterns, timeout=10):
        """Read until one of the regexes matches the buffer; return its index."""
        end = time.time() + timeout
        while time.time() < end:
            for n, p in enumerate(patterns):
                if re.search(p, self.buf):
                    return n
            try:
                self.buf += self._recv()
            except socket.timeout:
                pass
        raise TimeoutError(f"timed out waiting for {patterns}; got {self.buf[-200:]!r}")

    def send(self, text):
        self.buf = ""
        self.sock.sendall(text.encode() + b"\r\n")

    def close(self):
        """Log out and wait for the switch to drop the session."""
        self.send("exit")
        try:
            while self._recv():
                pass
        except (ConnectionError, socket.timeout, OSError):
            pass

    def login(self, user, password):
        # The switch sometimes stalls after the banner when the previous session was
        # just closed; reconnect and retry once.
        for attempt in (1, 2):
            try:
                self._login(user, password)
                return
            except (TimeoutError, ConnectionError):
                if attempt == 2:
                    raise
                self.sock.close()
                time.sleep(5)
                self.sock = socket.create_connection((self.host, 23), timeout=10)
                self.sock.settimeout(10)
                self.buf = ""

    def _login(self, user, password):
        self.expect([r"login: $"])
        self.send(user)
        self.expect([r"Password: $"])
        self.send(password)
        if self.expect([r"[\w.()-]+# $", r"(?i)login incorrect|login: $"], timeout=20) != 0:
            raise PermissionError("login failed")

    def run(self, command, timeout=90):
        """Run a command, paging through --More--, and return the clean output."""
        self.send(command)
        out = ""
        while True:
            n = self.expect([re.escape(MORE), r"[\w.()-]+# $"], timeout=timeout)
            if n == 0:
                chunk, _, self.buf = self.buf.partition(MORE)
                out += chunk
                self.sock.sendall(b" ")
            else:
                out += self.buf
                break
        out = ANSI_RE.sub("", out).replace("\r", "")
        lines = out.split("\n")
        # drop echoed command (first line) and trailing prompt
        return "\n".join(l.rstrip() for l in lines[1:-1]).strip()


def parse(output):
    info = {}
    m = re.match(r"(\S+) admin (\w+), oper status is (\w+) \(([^)]+)\)", output)
    if m:
        info.update(interface=m[1], admin=m[2], oper=m[3], link=m[4])
    for key, label in [("media_type", "Media Type"), ("duplex", "Duplex"),
                       ("speed", "Speed"), ("negotiation", "Negotiation"),
                       ("mdi_mdix", r"MDI/MDIX"), ("port_state", "Port State")]:
        m = re.search(rf"^{label}\s*:\s*(.+)$", output, re.M)
        if m:
            info[key] = m[1].strip()
    return info


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default=os.environ.get("MOXA_HOST"), help="switch management address (or set MOXA_HOST)")
    ap.add_argument("--port", type=int, default=1, help="port number on slot 1")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default=os.environ.get("MOXA_PASSWORD", "moxa"))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    sw = Switch(a.host)
    try:
        sw.login(a.user, a.password)
        out = sw.run(f"show interfaces ethernet 1/{a.port}")
        sw.close()
    finally:
        sw.sock.close()
    info = parse(out)
    if not info:
        sys.exit(f"could not parse switch output:\n{out}")
    if a.json:
        print(json.dumps(info, indent=2))
    else:
        for k, v in info.items():
            print(f"{k:12} {v}")


if __name__ == "__main__":
    main()
