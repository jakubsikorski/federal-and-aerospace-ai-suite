#!/usr/bin/env python3
"""Get / set the PTP Time Synchronization profile on a Moxa TSN-G5000.

Equivalent of the web UI: System > Time > Time Synchronization.

  ptp_profile.py get
  ptp_profile.py 1588v2     # IEEE 1588 Default-2008: enabled, priority1 64, priority2 128 (current settings)
  ptp_profile.py 802.1as    # IEEE 802.1AS-2011: enabled, priority1 246, priority2 248
Wrappers: ptp_set_1588v2.py, ptp_set_8021as.py.

The profile is global on this switch: the CLI sets it from any interface and the switch applies
it to all ports ("% Note: Profile setting will be applied to all ports."). The script therefore
issues it once, from --via-port (default 8, the port with no cable), and never names port 2.
Add --dry-run to print the CLI commands without sending them.
"""
import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # needed under python -I
from get_port_mode import Switch  # noqa: E402

FORBIDDEN_PORTS = {2}
PROFILES = {
    "1588v2": {"cli": "default", "shown": "1588v2 Default Profile", "p1": 64, "p2": 128,
               "extra": ["mode boundary delay-mechanism e2e", "network-transport ipv4", "domain 0"]},
    "802.1as": {"cli": "dot1as", "shown": "802.1AS", "p1": 246, "p2": 248, "extra": []},
}


def get_state(sw):
    g = sw.run("show ptp")
    status = re.search(r"PTP Status\s*:\s*(\w+)", g)
    profile = re.search(r"Current Profile\s*:\s*(.+)", g)
    if not (status and profile):
        raise RuntimeError(f"cannot parse show ptp:\n{g}")
    st = {"enabled": status[1] == "Enabled", "profile": profile[1].strip()}
    for key in PROFILES.values():
        d = sw.run(f"show ptp profile {key['cli']}")
        p1, p2 = re.search(r"Priority1\s*:\s*(\d+)", d), re.search(r"Priority2\s*:\s*(\d+)", d)
        st[key["cli"]] = (int(p1[1]), int(p2[1])) if p1 and p2 else None
    return st


def show(st):
    print(f"time synchronization: {'Enabled' if st['enabled'] else 'Disabled'}, current profile: {st['profile']}")
    for name, key in PROFILES.items():
        print(f"  {name:8} priority1/priority2 = {st[key['cli']]}")


def plan(profile, via_port):
    p = PROFILES[profile]
    cmds = ["configure", "ptp enable",
            f"ptp profile {p['cli']} priority1 {p['p1']}", f"ptp profile {p['cli']} priority2 {p['p2']}"]
    cmds += [f"ptp profile {p['cli']} {e}" for e in p["extra"]]
    cmds += [f"interface ethernet 1/{via_port}", f"ptp profile {p['cli']}", "exit", "end"]
    return cmds


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=["get", *PROFILES])
    ap.add_argument("--via-port", type=int, default=8, help="port the profile command is issued from (default 8)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--host", default="192.168.127.253")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default=os.environ.get("MOXA_PASSWORD", "moxa"))
    a = ap.parse_args(argv)

    if a.via_port in FORBIDDEN_PORTS or not 1 <= a.via_port <= 8:
        sys.exit(f"cannot use port {a.via_port} as --via-port")

    sw = Switch(a.host)
    try:
        sw.login(a.user, a.password)
        st = get_state(sw)
        show(st)
        if a.action == "get":
            return
        cmds = plan(a.action, a.via_port)
        if a.dry_run:
            print("\n".join(cmds))
            return
        for c in cmds:
            out = sw.run(c)
            if "%" in out and "Note:" not in out:
                sw.run("end")
                raise RuntimeError(f"switch rejected '{c}': {out.strip()}")
        p = PROFILES[a.action]
        deadline = time.time() + 30
        while True:
            new = get_state(sw)
            if new["enabled"] and new["profile"] == p["shown"] and new[p["cli"]] == (p["p1"], p["p2"]):
                break
            if time.time() > deadline:
                show(new)
                sys.exit("VERIFY FAILED: PTP state does not match the request")
            time.sleep(2)
        show(new)
        print("OK")
    finally:
        sw.close()


if __name__ == "__main__":
    main()
