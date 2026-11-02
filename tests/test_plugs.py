"""Plugs and unplugs (design.md section 17: noticing).

The core asks the OS what is plugged in every few seconds and, on a change, scans only the new devices. Two promises
come with it: a scan never opens a port the session is using (the watch holds it; recon opening it too would steal
its bytes or report a busy port as a crash), and what the human allowed for a board does not carry over to whatever
is plugged in after it is pulled out (another board can come back under the same port name).
"""

import json
import shutil
from pathlib import Path

import pytest

from benchscout.broker import Broker
from benchscout.io import FixtureIo, parse_ioreg
from benchscout.policy import Effect

FIXTURES = Path(__file__).parent.parent / "fixtures"
ESP = "usb:303a:1001:F4:12:FA:3B:9C:10"
ESP_PORT = "/dev/cu.usbmodem1101"


class Bench:
    """The fixture file is the bench: writing a device into it plugs it in."""

    def __init__(self, path: Path):
        self.path = path

    def edit(self, change) -> None:
        d = json.loads(self.path.read_text(encoding="utf-8"))
        change(d)
        self.path.write_text(json.dumps(d), encoding="utf-8")
        import os
        import time

        os.utime(self.path, ns=(time.time_ns(), time.time_ns() + 1_000_000))  # a new mtime even within one tick

    def unplug(self, vid: str) -> tuple[list, list]:
        taken: tuple[list, list] = ([], [])

        def out(d):
            taken[0].extend(u for u in d["usb"] if u["vid"] == vid)
            taken[1].extend(s for s in d["serial"] if s["vid"] == vid)
            d["usb"] = [u for u in d["usb"] if u["vid"] != vid]
            d["serial"] = [s for s in d["serial"] if s["vid"] != vid]

        self.edit(out)
        return taken

    def plug(self, usb: list, serial: list) -> None:
        self.edit(lambda d: (d["usb"].extend(usb), d["serial"].extend(serial)))


@pytest.fixture
def bench(tmp_path):
    f = tmp_path / "bench.json"
    shutil.copy(FIXTURES / "bench_e2e.json", f)
    io = FixtureIo(f)
    br = Broker(io, tmp_path / "ws" / ".benchscout", approval_wait=0)
    br.scan()
    yield br, io, Bench(f)
    br.end_session()


def test_what_was_allowed_for_a_board_leaves_with_it(bench):
    br, io, b = bench
    br.select([ESP])
    port = br.resolve_port(ESP_PORT)
    asked = br.request("serial.listen", port, {"seconds": 0.1})
    assert asked["status"] == "approval_required", "opening the ESP32's native USB may reset it: it asks"
    br.decide(asked["approval_id"], True, "session")
    assert br.approvals.covered("serial.listen", port, Effect.DISRUPTIVE)
    pending = br.request("serial.send", port, {"data": "x", "seconds": 0.1})["approval_id"]

    usb, serial = b.unplug("0x303a")
    br.scan(only_new=True)
    assert not br.approvals.covered("serial.listen", port, Effect.DISRUPTIVE), "the session grant went with the board"
    assert br.approvals.get(pending).status == "withdrawn"
    assert ESP in br.selected, "still chosen: it is watched again when it is back"

    # another board comes back on the same port name: chosen by nobody, allowed nothing
    other = dict(usb[0], serial="AA:BB:CC:DD:EE:FF")
    b.plug([other], [dict(serial[0], serial_number="AA:BB:CC:DD:EE:FF")])
    br.scan(only_new=True)
    assert br.resolve_port(ESP_PORT) == port
    assert br.request("serial.listen", port, {"seconds": 0.1})["status"] == "not_selected"
    assert not br.approvals.covered("serial.listen", port, Effect.DISRUPTIVE)


def test_a_scan_does_not_open_what_the_session_is_using(bench):
    br, io, b = bench
    stlink = next(d for d in br.bench.devices.values() if d.name.endswith("STLINK-V3"))
    path = "/dev/cu.usbmodem21403"
    heard_first = [f for t in br.bench.targets_via(br.resolve_port(path)) for f in t.facts if f.startswith("log:")]
    assert heard_first, "the first scan listened to it (SAFE)"
    br.select([stlink.id])
    assert br.watches[br.resolve_port(path)].alive, "watched: the watch holds the port"

    io.calls.clear()
    br.scan()  # the human pressed Rescan
    assert ("serial_listen", path) not in io.calls and ("serial_stream", path) not in io.calls
    kept = [f for t in br.bench.targets_via(br.resolve_port(path)) for f in t.facts if f.startswith("earlier log:")]
    assert kept, "what the first scan heard is still shown, marked as earlier"
    assert ("serial_listen", "/dev/cu.usbserial-210319A8C7D11") in io.calls, "devices not in the session are probed again"

    # after a plug only the new device is probed
    io.calls.clear()
    b.plug([{"name": "CP2102N USB to UART Bridge Controller", "vendor": "Silicon Labs", "vid": "0x10c4", "pid": "0xea60",
             "serial": "0001", "location": "0x01400000"}],
           [{"device": "/dev/cu.usbserial-0001", "vid": "0x10c4", "pid": "0xea60", "serial_number": "0001", "description": "CP2102N"}])
    events = []
    br.on_event = events.append
    br.scan(only_new=True)
    probed = [c for c in io.calls if c[0] in ("serial_listen", "serial_stream", "visa_query", "jtag_scan")]
    assert all(c[1] == "/dev/cu.usbserial-0001" for c in probed), probed
    changed = next(e for e in events if e["event"] == "devices_changed")
    assert [d["id"] for d in changed["added"]] == ["usb:10c4:ea60:0001"] and not changed["removed"]


def test_ioreg_lists_the_bench_as_macos_saw_it():
    """Real bench, 2026-11-02 (macOS 26, Apple Silicon): what the core polls to notice plugs."""
    found = parse_ioreg((FIXTURES / "real" / "macos_ioreg_usb_2026-11-02.plist").read_bytes())
    got = {(d["vid"], d["pid"], d["serial"]) for d in found}
    assert got == {(0x303A, 0x1001, "24:58:7C:E9:40:00"), (0x0403, 0x6010, "FactoryAIOT Pro")}
    assert all(d["location"] and d["location"].startswith("0x") for d in found)
    assert parse_ioreg(b"") == [] and parse_ioreg(b"<plist") == []


def test_a_port_handed_to_a_vm_is_noticed_though_its_device_stays_listed(bench):
    """Real bench, 2026-11-03: the ESP32-S3 handed to a UTM VM stayed in macOS's USB list (ioreg, recorded while the
    VM had it); only its serial port went, so a core that watched the USB list alone noticed nothing."""
    in_vm = parse_ioreg((FIXTURES / "real" / "macos_ioreg_usb_esp32_in_vm_2026-11-03.plist").read_bytes())
    assert any(d["vid"] == 0x303A for d in in_vm), "still listed while the VM has it"

    br, io, b = bench
    br.select([ESP])
    port = br.resolve_port(ESP_PORT)
    br.decide(br.request("serial.listen", port, {"seconds": 0.1})["approval_id"], True, "session")
    looked = io.plugged()
    b.edit(lambda d: d.update(serial=[s for s in d["serial"] if s["vid"] != "0x303a"]))  # the USB device stays
    assert io.plugged() != looked, "what the core polls changes"
    br.scan(only_new=True)
    assert ESP in br.bench.devices and port not in br.bench.ports
    assert not br.approvals.covered("serial.listen", port, Effect.DISRUPTIVE), "nothing allowed carries over"
