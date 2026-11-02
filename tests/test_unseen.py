"""A bench of devices benchscout ships no data about (review 2026-11-02): other MCUs, an FPGA board whose
wiring it does not know, a debug probe, instruments. benchscout must name them only from what they say,
open nothing, point no tool at them, and still notice a crash once a port is watched."""

import json
import time

import pytest
from pathlib import Path

from benchscout import knowledge as K
from benchscout import recon
from benchscout.broker import Broker
from benchscout.io import FixtureIo
from benchscout.model import Confidence
from benchscout.policy import Effect, classify

UNSEEN = Path(__file__).parent.parent / "fixtures" / "bench_unseen.json"
PICO = "/dev/cu.usbmodem14201"


def test_unseen_devices_are_named_only_by_what_they_say():
    r = recon.run(FixtureIo(UNSEEN))
    b = r.bench
    names = {d.name for d in b.devices.values()}
    assert "MicroPython Board in FS mode" in names and "SEGGER J-Link" in names
    identified = {t.name: [e.source for e in t.evidence] for t in b.targets.values() if t.confidence == Confidence.IDENTIFIED}
    assert identified == {"Keysight 34465A": ["scpi *IDN?"], "Tektronix MSO44": ["scpi *IDN?"]}
    others = [t for t in b.targets.values() if t.confidence != Confidence.IDENTIFIED]
    assert all(t.name is None for t in others), "no guessed names"


def test_unseen_devices_are_handled_cautiously():
    io = FixtureIo(UNSEEN)
    r = recon.run(io)
    b = r.bench
    assert not [c for c in io.calls if c[0] in ("serial_listen", "jtag_scan")], "nothing opened, no tool pointed at them"
    assert all(p.startswith("scpi.query *IDN?") for p in r.ran)
    assert not any("ASRL" in p for p in r.ran), "*IDN? never goes to a serial VISA resource"
    serial = [p for p in b.ports.values() if p.kind == "serial"]
    assert len(serial) == 6 and all(classify(b, "serial.listen", p.id).effect == Effect.DISRUPTIVE for p in serial)
    assert not [p for p in b.ports.values() if p.kind in ("jtag", "swd")], "an unknown board's channels are not taken for JTAG"


def test_a_crash_on_an_unseen_board_is_noticed_once_it_is_watched(tmp_path):
    data = json.loads(UNSEEN.read_text())
    data["streams"] = {PICO: [{"after": 0.1, "text": "MicroPython v1.24.1\n"},
                              {"after": 0.3, "text": "Traceback (most recent call last):\n  File \"main.py\", line 7\n"}]}
    f = tmp_path / "bench.json"
    f.write_text(json.dumps(data))
    br = Broker(FixtureIo(f), tmp_path / "ws" / ".benchscout")
    events = []
    br.on_event = events.append
    br.scan()
    br.select([br.bench.ports["serial:usbmodem14201"].device_id])
    offer = next(e["approval"] for e in events if e["event"] == "approval_requested")
    assert offer["by"] == "benchscout" and offer["action"] == "serial.watch", "watching an unknown board is asked first"
    br.decide(offer["id"], True)
    end = time.monotonic() + 4
    while not any(e.get("kind") == "alert" for e in events) and time.monotonic() < end:
        time.sleep(0.05)
    alert = next(e for e in events if e.get("kind") == "alert")
    assert alert["what"] == "exception: Python traceback printed" and alert["device_name"] == "MicroPython Board in FS mode"
    br.end_session()


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="the stand-in vendor tool is a shell script")
def test_without_any_shipped_tools_the_agent_can_still_bring_one(tmp_path, monkeypatch):
    """The twin of 'deleting the shipped data only makes benchscout stricter' (2026-11-02): shipped tool actions are
    a convenience, never the only way. With none at all, a programmer cable benchscout has no data for is still
    visible to the agent, the agent can propose the vendor's tool for it, and once the human confirms, it runs
    through the same gate. (FPGA = openFPGALoader had become the only way, and such cables were not even listed.)"""
    import json

    from benchscout import report
    from benchscout.broker import Broker

    empty = tmp_path / "empty"
    empty.mkdir()
    shipped, K.SHIPPED = K.SHIPPED, empty
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tool = bin_dir / "vendorprog"
    tool.write_text('#!/bin/sh\necho "vendorprog: cable $2 programmed $4"\n', encoding="utf-8")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")
    data = json.loads(UNSEEN.read_text(encoding="utf-8"))
    data["usb"].append({"name": "Platform Cable USB II", "vendor": "Xilinx", "vid": "0x03fd", "pid": "0x0013",
                        "serial": "000013C1B4A101", "location": "0x01200000"})
    f = tmp_path / "bench.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    ws = tmp_path / "ws"
    (ws / "top.bit").parent.mkdir(parents=True, exist_ok=True)
    (ws / "top.bit").write_bytes(b"\0" * 16)
    try:
        br = Broker(FixtureIo(f), ws / ".benchscout", approval_wait=0)
        r = br.scan()
        assert "Platform Cable USB II" in report.render(r).split("Other USB devices")[1], "the agent sees it"
        res = br.define_tool({"name": "vendorprog.program", "argv": ["vendorprog", "--cable", "{device.serial}", "--program", "{bitstream}"],
                              "effect": "dangerous", "reason": "loads a bitstream into the FPGA over JTAG",
                              "args": {"bitstream": {"type": "file", "required": True}}, "port_kinds": ["jtag"]},
                             "vendor guide: 'program loads the bitstream'", [{"vid": "0x03fd", "pid": "0x0013", "probe": "jtag"}])
        br.decide_tool(res["proposal_id"], True)
        cable = next(d for d in br.bench.devices.values() if d.vid == 0x03FD)
        port = br.bench.ports_of(cable.id)[0].id
        br.select([cable.id])
        ask = br.request("vendorprog.program", port, {"bitstream": "top.bit"})
        assert ask["status"] == "approval_required" and ask["impact"]["effect"] == "dangerous", "the same gate as anything"
        br.decide(ask["approval_id"], True)
        done = br.request("vendorprog.program", port, {"bitstream": "top.bit"}, approval_id=ask["approval_id"])
        assert done["status"] == "done" and "cable 000013C1B4A101 programmed" in done["result"]["preview"]
        br.end_session()
    finally:
        K.SHIPPED = shipped
        K.load()
