"""The runtime contracts (design.md section 3; docs/notes.md review 7).

An external review (2026-11-03) found ways around the gate that no single test had looked for, because each was a
state change between two calls: a definition changed while the human decided, a weaker memory written over a
stronger one, a raw send of a command remembered as dangerous, a port reopened behind the broker's back, two
identical boards taken for one. Each test here is one of those, and fails on the code before the fix.
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from benchscout.broker import Broker
from benchscout.io import FixtureIo
from benchscout.policy import Effect

FIXTURE = Path(__file__).parent.parent / "fixtures" / "bench_example.json"
REAL = Path(__file__).parent.parent / "fixtures" / "real"
VCP = "/dev/cu.usbmodem21403"  # ST-Link VCP: safe to open, a unit with its own serial
CH340 = "/dev/cu.usbserial-10"  # bridge, no serial: opening may reset the board behind it


def bench(tmp_path, change=None, select=(), wait=0):
    data = json.loads(FIXTURE.read_text())
    if change:
        change(data)
    f = tmp_path / "bench.json"
    f.write_text(json.dumps(data))
    br = Broker(FixtureIo(f), tmp_path / "ws" / ".benchscout", approval_wait=wait)
    br.scan()
    if select:
        br.select([br.bench.ports[br.resolve_port(p)].device_id for p in select])
    return br


def remember(br, port, command, effect, **extra):
    return br.remember(port, "command", f"{command} ({effect})", data={"command": command, "effect": effect, **extra})


# ---- what an approval runs -------------------------------------------------------------------------------------

def test_an_approval_runs_only_what_the_human_saw(tmp_path):
    br = bench(tmp_path, select=[VCP])
    port = br.resolve_port(VCP)
    assert remember(br, port, "status", "disruptive")["status"] == "remembered"
    asked = br.request("serial.command", port, {"name": "status"})
    assert asked["status"] == "approval_required"
    card = br.approvals.get(asked["approval_id"])
    assert '"data": "status"' in card.args and '"eol": "\\n"' in card.args, "the card shows what will be sent"
    remember(br, port, "status", "disruptive", eol="\r\n")  # the definition changes while the human decides
    br.decide(asked["approval_id"], True)
    again = br.request("serial.command", port, {"name": "status"}, approval_id=asked["approval_id"])
    assert again["status"] == "approval_mismatch", "the OK was for other bytes"
    # a line ending is a line ending: it cannot carry a second command past the human
    assert remember(br, port, "ping", "safe", eol="\nerase\n")["status"] == "rejected"
    br.end_session()


# ---- loosening is against what is in force ---------------------------------------------------------------------

OPS = st.lists(st.one_of(st.tuples(st.just("remember"), st.sampled_from(["safe", "disruptive", "dangerous"])),
                         st.tuples(st.just("decide"), st.booleans())), min_size=1, max_size=8)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(ops=OPS)
def test_a_remembered_effect_is_never_weakened_without_the_human(tmp_path, monkeypatch, ops):
    """Whatever the agent writes, in whatever order: the command is never judged weaker than the strongest effect in
    force, unless the human confirmed the weaker one; and a raw send of it is never weaker than that either."""
    monkeypatch.setenv("BENCHSCOUT_MEMORY", str(tmp_path / f"m{time.monotonic_ns()}"))
    br = bench(tmp_path, select=[VCP])
    port = br.resolve_port(VCP)
    key = br._key(br.bench.devices[br.bench.ports[port].device_id])
    in_force = None  # the model: the entry in force, by the rules (stronger or equal applies) or the human's OK
    for op, arg in ops:
        if op == "remember":
            r = remember(br, port, "erase", arg)
            weaker = Effect[arg.upper()] < max(Effect.DISRUPTIVE, Effect[in_force.upper()] if in_force else Effect.DISRUPTIVE)
            assert r["status"] == ("pending_human" if weaker else "remembered"), (arg, in_force)
            if not weaker:
                in_force = arg
        else:
            waiting = [e for e in br.memory.entries(key) if e.status == "pending"]
            if waiting:
                br.decide_memory(key, waiting[-1].id, arg)
                if arg:
                    in_force = waiting[-1].data["effect"]
        expected = Effect[in_force.upper()] if in_force else Effect.DISRUPTIVE
        assert br.impact("serial.command", port, {"name": "erase"}).effect == expected
        assert br.impact("serial.send", port, {"data": "erase"}).effect == max(expected, Effect.DISRUPTIVE)
    br.end_session()


def test_a_dangerous_command_waits_for_the_human_to_be_weakened(tmp_path):
    br = bench(tmp_path, select=[VCP])
    port = br.resolve_port(VCP)
    remember(br, port, "erase", "dangerous")
    assert remember(br, port, "erase", "disruptive")["status"] == "pending_human"
    assert br.impact("serial.command", port, {"name": "erase"}).effect == Effect.DANGEROUS, "the stronger stays in force"
    br.end_session()


# ---- a raw send is not a way around --------------------------------------------------------------------------

def test_a_raw_send_is_judged_by_what_it_sends_and_allowed_only_for_the_words_seen(tmp_path):
    br = bench(tmp_path, select=[VCP])
    port = br.resolve_port(VCP)
    remember(br, port, "erase", "dangerous")
    for data in ("erase", "erase all", "status\nerase"):
        assert br.impact("serial.send", port, {"data": data}).effect == Effect.DANGEROUS, data
    assert br.impact("serial.send", port, {"data": "status", "eol": "\nerase\n"}).effect == Effect.DANGEROUS, "eol is sent too"
    asked = br.request("serial.send", port, {"data": "status"})
    br.decide(asked["approval_id"], True, "session")
    assert br.request("serial.send", port, {"data": "status", "seconds": 0.1})["status"] == "done"
    assert br.request("serial.send", port, {"data": "reboot", "seconds": 0.1})["status"] == "approval_required", \
        "the session OK was for `status`, not for whatever comes next"
    br.set_allow_all(True)
    assert br.request("serial.send", port, {"data": "reboot", "seconds": 0.1})["status"] == "done"
    assert br.request("serial.send", port, {"data": "erase", "seconds": 0.1})["status"] == "approval_required", \
        "allow all covers what only changes state; a command remembered as dangerous still asks"
    br.end_session()


# ---- opening is a step of every call --------------------------------------------------------------------------

def test_every_call_that_opens_a_port_that_may_reset_asks(tmp_path):
    br = bench(tmp_path)
    port = br.resolve_port("/dev/cu.usbmodem1101")  # the chip's own USB: opening may reset it
    br.bench.ports[port].commands["status"] = {"effect": "safe", "eol": "\n", "text": "", "pending": False}
    for action, args in (("serial.listen", {}), ("serial.watch", {}), ("serial.send", {"data": "status"}),
                         ("serial.command", {"name": "status"})):
        assert br.impact(action, port, args).effect >= Effect.DISRUPTIVE, action
    br.end_session()


def test_a_listen_at_another_baud_reopens_and_is_judged_as_an_open(tmp_path):
    br = bench(tmp_path, lambda d: d.update(streams={CH340: [{"after": 0.05, "text": "boot\n"}]}), select=[CH340])
    port = br.resolve_port(CH340)
    br.decide(br.watch_offers[port], True)
    for _ in range(100):
        if br._held_open(port, {}):
            break
        time.sleep(0.02)
    assert br.impact("serial.listen", port, {"baud": 115200}).effect == Effect.SAFE, "held open: nothing is opened"
    assert br.impact("serial.listen", port, {"baud": 9600}).effect == Effect.DISRUPTIVE, "another baud closes and reopens it"
    br.end_session()


# ---- recon applies memory first ----------------------------------------------------------------------------------

def test_recon_does_not_open_a_port_memory_says_resets_when_opened(tmp_path):
    from benchscout.memory import Entry

    br = bench(tmp_path)
    port = br.resolve_port(VCP)  # safe to open by the shipped data, so recon listens to it
    dev = br.bench.devices[br.bench.ports[port].device_id]
    br.memory.add(br._key(dev), Entry(kind="safety", source="benchscout", target=port, status="active",
                                      text="opened as safe and it vanished", data={"listen_safe": False}), br._header(dev))
    br.io.calls.clear()
    br.scan()
    assert ("serial_listen", VCP) not in br.io.calls, "the stricter memory counts before any probe"
    br.end_session()


# ---- the broker decides every reopen -------------------------------------------------------------------------

def test_a_watch_that_reset_the_device_by_opening_does_not_open_it_again_on_its_own(tmp_path, monkeypatch):
    events = []
    br = bench(tmp_path, lambda d: d.update(streams={VCP: [{"after": 0.2, "vanish": 0.3}, {"after": 0.8, "text": "boot\n"}]}))
    br.on_event = events.append
    port = br.resolve_port(VCP)
    br.select([br.bench.ports[port].device_id])
    end = time.monotonic() + 4
    while time.monotonic() < end and not any(e.get("event") == "approval_requested" for e in events):
        time.sleep(0.05)
    assert not any(e.get("kind") == "returned" for e in events), "it vanished right after opening: not reopened unasked"
    gone = [e for e in events if e.get("kind") == "gone"]
    assert gone and "asks" in gone[0]["what"]
    offer = next(e["approval"] for e in events if e.get("event") == "approval_requested")
    assert offer["action"] == "serial.watch" and offer["by"] == "benchscout", "benchscout asks the human, as the first time"
    br.end_session()


# ---- identical units are not mixed up ---------------------------------------------------------------------------

def _two_ch340(locations: bool):
    def change(d):
        d["usb"] = [u for u in d["usb"] if u["vid"] != "0x1a86"] + [
            {"name": "USB Serial", "vendor": "QinHeng", "vid": "0x1a86", "pid": "0x7523", "serial": None, "location": "0x01300000"},
            {"name": "USB Serial", "vendor": "QinHeng", "vid": "0x1a86", "pid": "0x7523", "serial": None, "location": "0x01400000"}]
        d["serial"] = [s for s in d["serial"] if s["vid"] != "0x1a86"] + [
            {"device": "/dev/cu.usbserial-130", "vid": "0x1a86", "pid": "0x7523", "serial_number": None,
             "location": "1-3" if locations else None, "description": "USB Serial"},
            {"device": "/dev/cu.usbserial-140", "vid": "0x1a86", "pid": "0x7523", "serial_number": None,
             "location": "1-4" if locations else None, "description": "USB Serial"}]
    return change


@pytest.mark.parametrize("locations", [True, False])
def test_two_identical_boards_without_serials_are_never_taken_for_one(tmp_path, locations):
    br = bench(tmp_path, _two_ch340(locations))
    owners = {p.path: p.device_id for p in br.bench.ports.values() if p.path in ("/dev/cu.usbserial-130", "/dev/cu.usbserial-140")}
    assert len(set(owners.values())) == 2, "each port has its own device"
    if locations:  # told apart by where each is plugged in (pyserial "1-3" is the USB device at 0x01300000)
        assert owners["/dev/cu.usbserial-130"] == "usb:1a86:7523:0x01300000"
    br.select([owners["/dev/cu.usbserial-130"]])
    assert br.request("serial.listen", "/dev/cu.usbserial-140")["status"] == "not_selected", "choosing one is not the other"
    br.end_session()


def test_on_macos_a_port_and_its_usb_device_are_found_at_the_same_place():
    """Real bench, 2026-11-03: pyserial's "1-1" is system_profiler's locationID 0x01100000."""
    from benchscout.recon import place

    rec = json.loads((REAL / "macos_locations_2026-11-03.json").read_text(encoding="utf-8"))
    for s in rec["serial"]:
        usb = next(u for u in rec["usb"] if u["serial"] == s["serial_number"])
        assert place(usb["location"]) == place(s["location"]), s["device"]
    assert place("3-3:1.0") == "3-3", "Linux: pyserial adds the interface after a colon"


# ---- names only from what devices printed -------------------------------------------------------------------------

def test_a_part_is_named_only_by_a_checked_quote_of_device_output(tmp_path):
    br = bench(tmp_path, select=[VCP])
    port = br.resolve_port(VCP)
    assert br.remember(port, "identity", "STM32F4", evidence="trust me")["status"] == "rejected"
    br.remember(port, "observation", "it said: MAGIC-CHIP-9000 rev B")  # the agent's words, not the device's
    target = br.bench.targets_via(port)[0].id
    assert br.identify(target, "MAGIC-CHIP-9000", "MAGIC-CHIP-9000 rev B")["status"] != "identified"
    br.end_session()


# ---- memory, patterns, knowledge kept current ----------------------------------------------------------------------

def test_always_on_this_instrument_keeps_every_command_the_human_allowed(tmp_path):
    from benchscout.memory import Memory

    m = Memory(tmp_path)
    from benchscout.memory import Entry

    for cmd in ("VOLT", "CURR"):
        m.add("k", Entry(kind="command", text="allowed always on this instrument, in the benchscout inbox", source="human",
                         target="visa:x", data={"command": cmd, "effect": "safe"}))
    assert {e.data["command"] for e in m.entries("k")} == {"VOLT", "CURR"}


def test_a_learned_trouble_line_is_looked_for_at_once_and_a_bad_pattern_is_refused(tmp_path):
    events = []
    br = bench(tmp_path, lambda d: d.update(streams={VCP: [{"after": 0.6, "text": "BROWNOUT detected\n"}]}), select=[VCP])
    br.on_event = events.append
    port = br.resolve_port(VCP)
    assert br.remember(port, "interpretation", "x", data={"pattern": "BROWN(OUT"})["status"] == "rejected"
    br.remember(port, "interpretation", "power dipped", data={"pattern": "BROWNOUT", "alert": "brownout"})
    end = time.monotonic() + 3
    while time.monotonic() < end and not any(e.get("kind") == "alert" for e in events):
        time.sleep(0.05)
    assert any(e.get("kind") == "alert" and e["what"] == "brownout" for e in events), "the running watch uses it"
    br.end_session()


def test_a_fixed_knowledge_file_is_read_at_the_next_scan(tmp_path):
    from benchscout import knowledge as K

    kdir = tmp_path / "ws" / ".benchscout" / "knowledge"
    kdir.mkdir(parents=True)
    (kdir / "lab.toml").write_text("[[usb]\nvid = 1\n")
    br = bench(tmp_path)
    assert K.PROBLEMS
    (kdir / "lab.toml").write_text("[[usb]]\nvid = 0x1A86\npid = 0x7523\nname = \"x\"\n")
    br.scan()
    assert not K.PROBLEMS, "Rescan reads it again, as the inbox card says"
    br.end_session()
    K.load()


# ---- exclusive means exclusive -------------------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "linux", reason="a pseudo-terminal stands in for a serial port only on Linux: macOS ignores "
                    "TIOCEXCL on ptys (its serial ports go through IOSerialFamily); Windows COM ports are exclusive anyway")
def test_a_port_held_by_benchscout_cannot_be_opened_by_another_program():
    """Review 7 (10): pyserial's `exclusive` is an flock, which only cooperating programs respect; a plain open got in."""
    import serial

    from benchscout.io import LiveIo

    master, slave = os.openpty()
    path = os.ttyname(slave)
    held = LiveIo().serial_stream(path, exclusive=True, lines="keep")
    try:
        with pytest.raises(serial.SerialException):
            serial.Serial(path, timeout=0.1)
    finally:
        held.close()
        os.close(master)
        os.close(slave)
