"""Windows and Linux: discovery parsers and FT2232H channels. Runs on any OS (no hardware)."""

import json
from pathlib import Path

import pytest

from benchscout import recon
from benchscout.cli import windows_locale_code
from benchscout.io import FixtureIo, linux_usb_devices, parse_windows_pnp

FIXTURE = Path(__file__).parent.parent / "fixtures" / "bench_example.json"

PNP = json.dumps([
    {"Name": "USB Composite Device", "Manufacturer": "Digilent", "PNPDeviceID": "USB\\VID_0403&PID_6010\\210319A8C7D1"},
    {"Name": "USB Serial Converter A", "Manufacturer": "FTDI", "PNPDeviceID": "USB\\VID_0403&PID_6010&MI_00\\6&2A1B3C&0&0000"},
    {"Name": "USB-SERIAL CH340 (COM4)", "Manufacturer": "wch.cn", "PNPDeviceID": "USB\\VID_1A86&PID_7523\\5&1F2E3D4C&0&2"},
    {"Name": "HID Keyboard", "Manufacturer": "(Standard keyboards)", "PNPDeviceID": "HID\\VID_046D&PID_C31C\\7&1"},
])


def test_windows_pnp_parsing():
    devs = parse_windows_pnp(PNP)
    assert [(d["vid"], d["pid"]) for d in devs] == [(0x0403, 0x6010), (0x1A86, 0x7523)]
    assert devs[0]["serial"] == "210319A8C7D1"
    assert devs[1]["serial"] is None and devs[1]["location"]  # generated instance id is not a serial
    assert parse_windows_pnp("") == [] and parse_windows_pnp("not json") == []
    assert len(parse_windows_pnp(json.dumps(json.loads(PNP)[0]))) == 1  # a single object, not a list


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="sysfs names like 1-1:1.0 cannot be made on Windows")
def test_linux_sysfs(tmp_path):
    def dev(name, **attrs):
        d = tmp_path / name
        d.mkdir()
        for k, v in attrs.items():
            (d / k).write_text(v + "\n")

    dev("1-1", idVendor="0403", idProduct="6010", serial="210319A8C7D1", manufacturer="Digilent", product="Digilent USB Device")
    dev("1-1:1.0", bInterfaceNumber="00")
    dev("usb1", idVendor="1d6b", idProduct="0002")
    devs = linux_usb_devices(tmp_path)
    assert {d["location"] for d in devs} == {"1-1", "usb1"}
    digilent = next(d for d in devs if d["vid"] == 0x0403)
    assert (digilent["serial"], digilent["vendor"]) == ("210319A8C7D1", "Digilent")
    assert linux_usb_devices(tmp_path / "missing") == []


def _bench(tmp_path, usb, serial):
    data = json.loads(FIXTURE.read_text())
    data.update(usb=usb, serial=serial, visa={}, listen={})
    f = tmp_path / "bench.json"
    f.write_text(json.dumps(data))
    return recon.build(FixtureIo(f))


def fabric_port(b):
    fab = next(t for t in b.targets.values() if t.kind == "fabric-endpoint")
    (p,) = b.ports_reaching(fab.id)
    return p


DIGILENT_USB = [{"name": "Digilent USB Device", "vendor": "Digilent", "vid": "0x0403", "pid": "0x6010", "serial": "210319A8C7D1", "location": "x"}]


def test_windows_ft2232h_one_device_channel_from_serial_letter(tmp_path):
    # Windows: channel A is on D2XX for JTAG, so only channel B shows up as a COM port,
    # with the channel letter appended to the serial.
    b = _bench(tmp_path, DIGILENT_USB, [{"device": "COM7", "vid": "0x0403", "pid": "0x6010", "serial_number": "210319A8C7D1B"}])
    assert len(b.devices) == 1
    assert fabric_port(b).path == "COM7"
    assert any(p.kind == "jtag" for p in b.ports.values())


def test_linux_ft2232h_channel_from_interface_number(tmp_path):
    ports = [{"device": f"/dev/ttyUSB{i}", "vid": "0x0403", "pid": "0x6010", "serial_number": "210319A8C7D1",
              "location": f"1-1:1.{i}"} for i in (1, 0)]  # listed out of order on purpose
    b = _bench(tmp_path, [], ports)
    assert fabric_port(b).path == "/dev/ttyUSB1"


def test_macos_ft2232h_channel_from_port_name(tmp_path):
    ports = [{"device": f"/dev/cu.usbserial-210319A8C7D1{i}", "vid": "0x0403", "pid": "0x6010", "serial_number": "210319A8C7D1"}
             for i in (1, 0)]
    b = _bench(tmp_path, DIGILENT_USB, ports)
    assert fabric_port(b).path == "/dev/cu.usbserial-210319A8C7D11"


def test_serial_ending_in_a_letter_is_not_cut_without_reason(tmp_path):
    # A CH340 whose serial happens to end in "B" is not a multi-channel part: leave it alone.
    b = _bench(tmp_path, [], [{"device": "COM3", "vid": "0x1a86", "pid": "0x7523", "serial_number": "ABCB"}])
    (dev,) = b.devices.values()
    assert dev.serial == "ABCB"


@pytest.mark.parametrize("name,code", [("Japanese_Japan", "ja"), ("ja_JP", "ja"), ("English_United States", "en"), (None, None)])
def test_windows_locale(name, code):
    assert windows_locale_code(name) == code


def test_runtime_dir_ignores_temp_dir_variables(monkeypatch):
    """MCP clients start the relay with a trimmed environment; it must find the same socket."""
    from benchscout.runtime import agent_address

    monkeypatch.delenv("BENCHSCOUT_RUNTIME", raising=False)
    monkeypatch.setenv("TMPDIR", "/var/folders/xx/T/")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/501")
    with_env = agent_address(Path("/work/bench"))
    for k in ("TMPDIR", "XDG_RUNTIME_DIR", "TEMP", "TMP"):
        monkeypatch.delenv(k, raising=False)
    assert agent_address(Path("/work/bench")) == with_env


WIN = json.loads((Path(__file__).parent.parent / "fixtures" / "real" / "windows_arm64_2026-11-02.json").read_text(encoding="utf-8"))


def _clean(s):
    return {k: (v.split("\0")[0] or None if isinstance(v, str) else v) for k, v in s.items()}


def test_windows_names_come_from_the_device_not_from_its_driver():
    """Windows VM, 2026-11-02: WMI named the ESP32-S3 "(Standard USB Host Controller) USB Composite Device" (a driver's
    name and an INF's maker) and benchscout showed that as the USB descriptor. Now: its own strings, read through
    the hub, or the product name Windows recorded from the bus."""
    devs = {d["vid"]: d for d in parse_windows_pnp(json.dumps(WIN["pnp"]), [_clean(s) for s in WIN["strings"]])}
    esp, ftdi = devs[0x303A], devs[0x0403]
    assert (esp["vendor"], esp["name"], esp["serial"], esp["no_driver"]) == ("Espressif", "USB JTAG/serial debug unit", "24:58:7C:E9:40:00", [])
    # no driver: it answered only its serial; the product name is what Windows kept from the bus
    assert (ftdi["vendor"], ftdi["name"], ftdi["serial"], ftdi["no_driver"]) == (None, "JTAG Debugger", "FactoryAIOT Pro", [0, 1])
    assert not any("Composite" in (d["name"] or "") or "Standard" in (d["vendor"] or "") for d in devs.values())
    # without the hub's strings: still no driver names
    plain = {d["vid"]: d for d in parse_windows_pnp(json.dumps(WIN["pnp"]))}
    assert plain[0x303A]["name"] == "USB JTAG/serial debug unit" and plain[0x303A]["vendor"] is None


def test_a_string_descriptor_ends_at_its_first_nul():
    from benchscout.winusb_strings import clean

    raw = bytes.fromhex("300346006100630074006f0072007900410049004f0054002000500072006f000000110022003300") + bytes(10)
    assert clean(raw) == "FactoryAIOT Pro"  # the Tang Nano FT2232's serial: bLength runs past the text
    assert clean(b"\x04\x03A\x00") == "A" and clean(b"\x02\x03") is None and clean(b"\x04\x02A\x00") is None


def test_a_device_windows_has_no_driver_for_is_shown_with_what_it_needs(tmp_path):
    from benchscout import report

    usb = [dict(d, vid=f"0x{d['vid']:04x}", pid=f"0x{d['pid']:04x}")
           for d in parse_windows_pnp(json.dumps(WIN["pnp"]), [_clean(s) for s in WIN["strings"]])]
    serial = [{"device": "COM3", "vid": "0x303a", "pid": "0x1001", "serial_number": "24:58:7C:E9:40:00",
               "description": "USB Serial Device (COM3)"}]
    b = _bench(tmp_path, usb, serial)
    ftdi = next(d for d in b.devices.values() if d.vid == 0x0403)
    assert ftdi.no_driver == [0, 1] and not b.ports_of(ftdi.id), "nothing can open it, so no port"
    text = report.render(recon.Recon(b))
    assert "■ JTAG Debugger  SN FactoryAIOT Pro" in text and "no driver for its USB interface(s) 0, 1" in text
    assert "■ Espressif USB JTAG/serial debug unit" in text
