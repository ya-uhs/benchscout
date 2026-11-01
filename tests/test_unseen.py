"""A bench of devices benchscout ships no data about (review 2026-11-02): other MCUs, an FPGA board whose
wiring it does not know, a debug probe, instruments. benchscout must name them only from what they say,
open nothing, point no tool at them, and still notice a crash once a port is watched."""

import json
import time
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


def test_without_shipped_data_the_unseen_bench_is_no_less_strict(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(K, "SHIPPED", empty)
    try:
        K.load()
        io = FixtureIo(UNSEEN)
        b = recon.run(io).bench
        assert not [c for c in io.calls if c[0] in ("serial_listen", "jtag_scan")]
        assert all(classify(b, "serial.listen", p.id).effect >= Effect.DISRUPTIVE for p in b.ports.values() if p.kind == "serial")
    finally:
        monkeypatch.undo()
        K.load()


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
