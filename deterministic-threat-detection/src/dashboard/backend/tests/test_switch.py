# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
import time

import pytest

from app.controllers.switch import MoxaSwitch, SimSwitch, parse_schedule, parse_tas

CURRENT = """port 1: config-change=True cycle=100000000 ns (100000 us)
  slot 0: interval=90000 us gates=0x43 queues=q6, q1, q0
  slot 1: interval=10000 us gates=0x23 queues=q5, q1, q0
  gate toggle (web page): on
  operative: active (2 entries)
"""
EMPTY = ("port 4: config-change=False cycle=1000000 ns (1000 us)\n  (no gate control entries)\n"
         "  gate toggle (web page): on\n  operative: not active (0 entries)\n")
CONFIGURED_NOT_RUNNING = CURRENT.replace("gate toggle (web page): on", "gate toggle (web page): off").replace(
    "operative: active (2 entries)", "operative: not active (0 entries)")


def test_parse_tas_enabled_and_empty():
    tas = parse_tas(CURRENT)
    assert tas["enabled"] and tas["cycle_us"] == 100000
    assert tas["slots"] == [{"interval_us": 90000, "queues": [6, 1, 0]},
                            {"interval_us": 10000, "queues": [5, 1, 0]}]
    assert not parse_tas(EMPTY)["enabled"]
    with pytest.raises(ValueError):
        parse_tas("garbage")


def test_parse_schedule():
    assert parse_schedule("90000:6,1,0 10000:5,1,0") == ["--slot", "90000:6,1,0", "--slot", "10000:5,1,0"]
    with pytest.raises(ValueError):
        parse_schedule("90000:")


def test_port_2_is_refused():
    with pytest.raises(ValueError):
        MoxaSwitch(2, "host", "~/moxa", "100:1")


def test_moxa_is_read_only_by_default_and_never_writes():
    calls = []
    sw = MoxaSwitch(1, "host", "~/moxa", "90000:6,1,0", run=lambda a, timeout=60: calls.append(a) or CURRENT)
    sw.refresh()
    assert sw.state()["enabled"] and sw.state()["read_only"]
    with pytest.raises(PermissionError):
        sw.set_enabled(False)
    assert calls == [["get", "--port", "1"]]  # no set/clear was issued


def test_moxa_write_issues_set_or_clear_then_rereads():
    state = {"out": EMPTY}
    calls = []

    def run(args, timeout=60):
        calls.append(args)
        if args[0] == "set":
            state["out"] = CURRENT
        if args[0] == "clear":
            state["out"] = EMPTY
        return state["out"]

    sw = MoxaSwitch(1, "host", "~/moxa", "90000:6,1,0 10000:5,1,0", allow_write=True, run=run)
    sw.refresh()
    assert not sw.state()["enabled"]
    sw.set_enabled(True)
    for _ in range(50):
        if not sw.busy:
            break
        time.sleep(0.02)
    assert sw.state()["enabled"]
    assert ["set", "--port", "1", "--slot", "90000:6,1,0", "--slot", "10000:5,1,0"] in calls
    sw.set_enabled(False)
    for _ in range(50):
        if not sw.busy:
            break
        time.sleep(0.02)
    assert not sw.state()["enabled"] and ["clear", "--port", "1"] in calls


def test_moxa_read_failure_is_reported_not_raised():
    def run(args, timeout=60):
        raise RuntimeError("ssh: connect timed out")

    sw = MoxaSwitch(1, "host", "~/moxa", "100:1", run=run)
    sw.refresh()
    assert "read failed" in sw.state()["error"] and sw.state()["known"] is False


def test_sim_switch_state_shape():
    assert SimSwitch().state()["read_only"] is False


def _wait_idle(sw):
    for _ in range(200):
        if not sw.busy:
            return
        time.sleep(0.01)
    raise AssertionError("switch stayed busy")


def test_busy_lasts_until_the_switch_is_read_back():
    state = {"out": EMPTY}
    seen = []

    def run(args, timeout=60):
        if args[0] == "set":
            time.sleep(0.15)
            state["out"] = CURRENT
        return state["out"]

    sw = MoxaSwitch(1, "host", "~/moxa", "90000:6,1,0 10000:5,1,0", allow_write=True, run=run)
    sw.refresh()
    sw.set_enabled(True)
    while sw.busy:
        s = sw.state()
        seen.append((s["busy"], s["target"], s["enabled"]))
        time.sleep(0.01)
    final = sw.state()
    assert seen and all(b and t is True for b, t, _ in seen)  # busy always carries the target
    assert final["enabled"] is True and final["target"] is None and final["busy"] is False
    # while it was busy it never claimed to be enabled before the readback
    assert all(not enabled for _, _, enabled in seen[:-1]) or seen[-1][2] is False


def test_change_that_does_not_stick_is_reported_with_the_real_state():
    sw = MoxaSwitch(1, "host", "~/moxa", "90000:6,1,0", allow_write=True,
                    run=lambda a, timeout=60: EMPTY,  # the switch ignores every write
                    verify_timeout_s=0.1, verify_interval_s=0.02)
    sw.refresh()
    sw.set_enabled(True)
    _wait_idle(sw)
    state = sw.state()
    assert state["enabled"] is False and "did not reach the requested state" in state["error"]


def test_slow_switch_is_waited_for_not_reported_as_failed():
    polls = {"n": 0}

    def run(args, timeout=60):
        if args[0] == "get":
            polls["n"] += 1
            return CURRENT if polls["n"] >= 5 else EMPTY  # applies the change a few reads late
        return ""

    sw = MoxaSwitch(1, "host", "~/moxa", "90000:6,1,0", allow_write=True, run=run,
                    verify_timeout_s=5, verify_interval_s=0.01)
    sw.set_enabled(True)
    _wait_idle(sw)
    state = sw.state()
    assert state["enabled"] is True and state["error"] is None and polls["n"] >= 5


def test_failed_write_keeps_error_and_real_state():
    def run(args, timeout=60):
        if args[0] == "set":
            raise RuntimeError("ssh: connection timed out")
        return CURRENT

    sw = MoxaSwitch(1, "host", "~/moxa", "90000:6,1,0", allow_write=True, run=run)
    sw.refresh()
    sw.set_enabled(True)
    _wait_idle(sw)
    state = sw.state()
    assert "connection timed out" in state["error"] and state["enabled"] is True


def test_second_request_while_busy_is_rejected():
    state = {"out": CURRENT}

    def run(args, timeout=60):
        if args[0] == "clear":
            time.sleep(0.2)
            state["out"] = EMPTY
        return state["out"]

    sw = MoxaSwitch(1, "host", "~/moxa", "90000:6,1,0", allow_write=True, run=run,
                    verify_timeout_s=2, verify_interval_s=0.01)
    sw.refresh()
    sw.set_enabled(False)
    with pytest.raises(RuntimeError):
        sw.set_enabled(True)
    _wait_idle(sw)


def test_configured_but_not_operative_is_not_reported_as_enabled():
    tas = parse_tas(CONFIGURED_NOT_RUNNING)
    assert tas["configured"] is True and tas["active"] is False and tas["gate"] is False
    assert tas["enabled"] is False  # nothing is being shaped


def test_old_script_output_without_the_new_lines_still_parses():
    old = CURRENT.split("  gate toggle")[0]
    tas = parse_tas(old)
    assert tas["gate"] is None and tas["active"] is None and tas["enabled"] is True


def test_enabling_is_refused_while_the_web_toggle_is_off():
    sw = MoxaSwitch(1, "host", "~/moxa", "90000:6,1,0", allow_write=True,
                    run=lambda a, timeout=60: CONFIGURED_NOT_RUNNING)
    sw.refresh()
    with pytest.raises(RuntimeError, match="off in the switch web page"):
        sw.set_enabled(True)
    assert sw.busy is False
    sw.set_enabled(False)  # turning it off is always allowed
    _wait_idle(sw)


def test_the_switch_address_is_handed_to_the_scripts(monkeypatch):
    seen = []

    class Done:
        returncode, stdout, stderr = 0, CURRENT, ""

    monkeypatch.setattr("app.controllers.switch.subprocess.run", lambda cmd, **kw: seen.append(cmd) or Done())
    sw = MoxaSwitch(1, "user@control-host", "~/moxa", "90000:6,1,0", switch_ip="switch-mgmt")
    sw.refresh()
    assert seen[0][-1].endswith("tas_config.py get --port 1 --host switch-mgmt")
    assert sw.state()["enabled"] is True
