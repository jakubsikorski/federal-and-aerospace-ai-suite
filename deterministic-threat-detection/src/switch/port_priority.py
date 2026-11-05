#!/usr/bin/env python3
"""Get / set the default priority (untagged frames) of ports on a Moxa TSN-G5000.

Untagged traffic from a device such as a camera takes the priority of the port it enters on, and the
priority picks the egress queue, so this decides which Qbv gate the traffic uses.

  port_priority.py get --ports 5,6
  port_priority.py set --ports 5,6 --value 3
Add --dry-run to print the CLI commands without sending them.
Port 2 is refused: our own management session reaches the switch through it.
"""
import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # needed under python -I
from get_port_mode import Switch  # noqa: E402

FORBIDDEN_PORTS = {2}


def read_priorities(sw):
    """{port: priority} from `show running-config`."""
    text = sw.run("show running-config")
    out = {}
    for m in re.finditer(r"interface ethernet 1/(\d)\n(.*?)\n!", text, re.S):
        p = re.search(r"qos default-priority (\d)", m[2])
        out[int(m[1])] = int(p[1]) if p else 0
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=["get", "set"])
    ap.add_argument("--ports", required=True, help="comma separated port numbers, e.g. 5,6")
    ap.add_argument("--value", type=int, help="priority 0-7 (set)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--host", default=os.environ.get("MOXA_HOST"), help="switch management address (or set MOXA_HOST)")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default=os.environ.get("MOXA_PASSWORD", "moxa"))
    a = ap.parse_args()

    ports = sorted({int(p) for p in a.ports.split(",")})
    if any(p in FORBIDDEN_PORTS for p in ports):
        sys.exit(f"refusing to touch port(s) {sorted(FORBIDDEN_PORTS & set(ports))}: management connection uses it")
    if not all(1 <= p <= 8 for p in ports):
        sys.exit("ports must be 1-8")
    if a.action == "set" and (a.value is None or not 0 <= a.value <= 7):
        sys.exit("--value 0-7 is required for set")

    cmds = ["configure"]
    for p in ports:
        cmds += [f"interface ethernet 1/{p}", f"qos default-priority {a.value}", "exit"]
    cmds.append("end")
    if a.dry_run:
        print("\n".join(cmds))
        return

    sw = Switch(a.host)
    try:
        sw.login(a.user, a.password)
        if a.action == "set":
            for c in cmds:
                out = sw.run(c)
                if "%" in out:
                    sw.run("end")
                    raise RuntimeError(f"switch rejected '{c}': {out.strip()}")
            deadline = time.time() + 20
            while True:
                now = read_priorities(sw)
                if all(now.get(p) == a.value for p in ports) or time.time() > deadline:
                    break
                time.sleep(2)
        now = read_priorities(sw)
        for p in ports:
            print(f"port {p}: default-priority {now.get(p)}")
        if a.action == "set" and any(now.get(p) != a.value for p in ports):
            sys.exit("VERIFY FAILED: priority does not match the request")
    finally:
        sw.close()


if __name__ == "__main__":
    main()
