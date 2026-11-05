#!/usr/bin/env python3
"""Get / set the IEEE 802.1Qbv Time-aware Shaper gate control list on a Moxa TSN-G5000.

Equivalent of the web UI: Layer 2 Switching > Time-aware Shaper > Settings > edit port.

  tas_config.py get --port 1
  tas_config.py set --port 1 --slot 90000:3,1 --slot 10000:5,3,1
  tas_config.py clear --port 1

--slot INTERVAL_US:Q,Q,...  one gate control entry (repeat per slot, in order). Interval is in
                            microseconds (0.001-999999.999); queues are 0-7, the listed queue
                            gates are open, all others closed.
The cycle time is set to the sum of the slot intervals.
Add --dry-run to print the CLI commands without sending them.
Port 2 is refused: our own management session reaches the switch through it.
"""
import argparse
import os
import re
import sys
import time
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # needed under python -I
from get_port_mode import Switch  # noqa: E402

FORBIDDEN_PORTS = {2}
NS_DENOM = 1_000_000_000


def parse_slot(text):
    """'90000:3,1' -> (interval_ns, gate_states)."""
    m = re.fullmatch(r"([\d.]+):(\d(?:,\d)*)", text)
    if not m:
        raise ValueError(f"bad --slot '{text}', expected INTERVAL_US:Q,Q (e.g. 90000:3,1)")
    us = Decimal(m[1])
    if not Decimal("0.001") <= us <= Decimal("999999.999"):
        raise ValueError(f"interval {us} us out of range 0.001-999999.999")
    ns = us * 1000
    if ns != ns.to_integral_value():
        raise ValueError(f"interval {us} us has finer than 0.001 us resolution")
    queues = {int(q) for q in m[2].split(",") if int(q) <= 7}
    if len(queues) != len(m[2].split(",")):
        raise ValueError(f"bad queue list in '{text}' (queues 0-7, no duplicates)")
    return int(ns), sum(1 << q for q in queues)


def queues_of(gates):
    return [q for q in range(7, -1, -1) if gates >> q & 1]


def _port_block(text, port, what):
    block = next((b for b in re.split(r"(?=Port Idx:)", text) if re.match(rf"Port Idx:\s*{port}\s", b)), None)
    if block is None:
        raise RuntimeError(f"port {port} not found in {what}")
    return block


def _entries(block):
    return [(int(t), int(g, 16)) for g, t in
            re.findall(r"Gate States:\s*0x([0-9A-Fa-f]+)\s+Time Interval:\s*(\d+)", block)]


def get_state(sw, port):
    """Parse `show dot1qbv administrative` (what is configured) and `... operative` (what is running)."""
    block = _port_block(sw.run("show dot1qbv administrative"), port, "show dot1qbv administrative")
    num = re.search(r"Numerator:\s*(\d+)", block)
    den = re.search(r"Denominator:\s*(\d+)", block)
    oper = _port_block(sw.run("show dot1qbv operative"), port, "show dot1qbv operative")
    return {
        "port": port,
        "config_change": "Config Change: True" in block,
        # 'Schedule Gate' is the per-port toggle of the web page; this CLI has no keyword to set it
        "gate": "Schedule Gate: True" in block,
        "cycle_ns": int(num[1]) * NS_DENOM // int(den[1]) if num and den else None,
        "entries": _entries(block),  # [(interval_ns, gate_states)] configured
        "oper_entries": _entries(oper),  # what the switch is really running
    }


def show(st):
    print(f"port {st['port']}: config-change={st['config_change']} cycle={st['cycle_ns']} ns "
          f"({st['cycle_ns'] / 1000:g} us)")
    for i, (ns, g) in enumerate(st["entries"]):
        print(f"  slot {i}: interval={ns / 1000:g} us gates=0x{g:02X} queues=q{', q'.join(map(str, queues_of(g)))}")
    if not st["entries"]:
        print("  (no gate control entries)")
    # Extra lines for the dashboard: the web-page toggle and whether the schedule is really running.
    print(f"  gate toggle (web page): {'on' if st['gate'] else 'off'}")
    print(f"  operative: {'active' if st['oper_entries'] else 'not active'} ({len(st['oper_entries'])} entries)")


def plan(port, slots):
    cmds = ["configure", f"interface ethernet 1/{port}", "dot1qbv control-list remove all"]
    for ns, g in slots:
        cmds.append(f"dot1qbv control-list append oper-name sgs gate-states {g} time {ns}")
    if slots:
        cycle = sum(ns for ns, _ in slots)
        cmds += [f"dot1qbv cycle-time numerator {cycle} denominator {NS_DENOM}",
                 "dot1qbv config-change operation true"]
    else:  # clear: back to the factory defaults
        cmds += ["dot1qbv config-change operation false", "dot1qbv cycle-time numerator 1 denominator 1000"]
    cmds += ["exit", "end"]
    return cmds


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=["get", "set", "clear"])
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--slot", action="append", default=[], help="INTERVAL_US:Q,Q (repeatable)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--host", default=os.environ.get("MOXA_HOST"), help="switch management address (or set MOXA_HOST)")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default=os.environ.get("MOXA_PASSWORD", "moxa"))
    a = ap.parse_args()

    if a.port in FORBIDDEN_PORTS:
        sys.exit(f"refusing to touch port {a.port}: management connection uses it")
    if not 1 <= a.port <= 8:
        sys.exit("port must be 1-8")
    if a.action == "set" and not a.slot:
        sys.exit("--slot is required for set")
    if a.action != "set" and a.slot:
        sys.exit("--slot only applies to set")
    try:
        slots = [parse_slot(s) for s in a.slot]
    except ValueError as e:
        sys.exit(str(e))

    sw = Switch(a.host)
    try:
        sw.login(a.user, a.password)
        st = get_state(sw, a.port)
        show(st)
        if a.action == "get":
            return
        cmds = plan(a.port, slots)
        if a.dry_run:
            print("\n".join(cmds))
            return
        for c in cmds:
            out = sw.run(c)
            if "%" in out:
                sw.run("end")
                raise RuntimeError(f"switch rejected '{c}': {out.strip()}")
        # The switch applies changes asynchronously: poll until they show up.
        deadline = time.time() + 30
        while True:
            new = get_state(sw, a.port)
            if slots:
                done = new["entries"] == slots and new["cycle_ns"] == sum(n for n, _ in slots)
            else:
                done = not new["entries"] and not new["config_change"]
            if done:
                break
            if time.time() > deadline:
                show(new)
                sys.exit("VERIFY FAILED: shaper state does not match the request")
            time.sleep(2)
        show(new)
        print("OK")
    finally:
        sw.close()


if __name__ == "__main__":
    main()
