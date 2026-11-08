"""Windows and Linux: discovery parsers and FT2232H channels. Runs on any OS (no hardware)."""

import json
from pathlib import Path

import pytest

from benchscout import recon
from benchscout.cli import windows_locale_code
from benchscout.io import FixtureIo, linux_usb_devices, parse_windows_pnp

FIXTURE = Path(__file__).parent.parent / "fixtures" / "bench_example.json"



LINUX = json.loads((Path(__file__).parent.parent / "fixtures" / "real" / "linux_arm64_2026-11-02.json").read_text(encoding="utf-8"))


def _sysfs(root: Path) -> Path:
    """The recorded /sys/bus/usb/devices, rebuilt as files."""
    for name, attrs in LINUX["sysfs"].items():
        d = root / name.replace(":", "_colon_")  # Windows cannot name a file with ':'; the parser only looks for ':'
        d.mkdir(parents=True)
        for k, v in attrs.items():
            (d / k).write_text(v + "\n", encoding="utf-8")
    return root


def test_linux_sysfs_as_recorded(tmp_path):
    """Ubuntu 24.04 ARM64 VM with the real boards, 2026-11-02."""
    devs = {d["location"]: d for d in linux_usb_devices(_sysfs(tmp_path / "sys"))}
    assert (devs["3-2"]["vendor"], devs["3-2"]["name"], devs["3-2"]["serial"]) == ("Espressif", "USB JTAG/serial debug unit", "24:58:7C:E9:40:00")
    assert (devs["3-3"]["vendor"], devs["3-3"]["name"], devs["3-3"]["serial"]) == ("SIPEED", "JTAG Debugger", "FactoryAIOT Pro")
    assert {loc for loc, d in devs.items() if d["device_class"] == 0x09} == {"usb1", "usb2", "usb3", "usb4"}  # root hubs
    assert linux_usb_devices(tmp_path / "missing") == []


def test_linux_bench_as_recorded_root_hubs_left_out_tang_nano_channels_right(tmp_path):
    """The root hubs ("xHCI Host Controller", class 09) were listed as other USB devices: hubs are left out by class.
    The FT2232's channel B (interface 1, /dev/ttyUSB1) is the UART into the FPGA, as on macOS."""
    usb = [dict(d, vid=f"0x{d['vid']:04x}", pid=f"0x{d['pid']:04x}") for d in linux_usb_devices(_sysfs(tmp_path / "sys"))]
    serial = [dict(p, vid=f"0x{p['vid']:04x}", pid=f"0x{p['pid']:04x}") for p in LINUX["serial"]]
    b = _bench(tmp_path, usb, serial)
    assert not any("Host Controller" in d.name for d in b.devices.values())
    assert fabric_port(b).path == "/dev/ttyUSB1"
    esp = next(d for d in b.devices.values() if d.vid == 0x303A)
    assert [p.path for p in b.ports_of(esp.id) if p.kind == "serial"] == ["/dev/ttyACM0"]


# raw output of openFPGALoader v1.2.0 on the real Tang Nano 9K (2026-10-31)
TANG_DETECT = ("No cable or board specified: using direct ft2232 interface\n"
               "Jtag frequency : requested 6.00MHz    -> real 6.00MHz   \nindex 0:\n\tidcode 0x100481b\n"
               "\tmanufacturer Gowin\n\tfamily GW1N\n\tmodel  GW1N(R)-9C\n\tirlength 8\n")


def test_tang_nano_9k_as_recorded_on_linux(tmp_path):
    """The real Tang Nano 9K as Linux listed it and openFPGALoader read it: the FPGA named by the tool's database, the
    UART on channel B inside it. Audit 2026-11-07 (T12): this was shown on macOS port names with a channel number no
    OS gives; on the real macOS its ports are unknown ports (#33)."""
    from benchscout.model import Confidence

    usb = [dict(d, vid=f"0x{d['vid']:04x}", pid=f"0x{d['pid']:04x}") for d in linux_usb_devices(_sysfs(tmp_path / "sys"))]
    serial = [dict(p, vid=f"0x{p['vid']:04x}", pid=f"0x{p['pid']:04x}") for p in LINUX["serial"]]
    data = json.loads(FIXTURE.read_text())
    data.update(usb=usb, serial=serial, visa={}, listen={}, tools=["openFPGALoader"], probes={"FactoryAIOT Pro": TANG_DETECT})
    f = tmp_path / "bench.json"
    f.write_text(json.dumps(data))
    b = recon.run(FixtureIo(f)).bench
    fpga = next(t for t in b.targets.values() if t.name == "Gowin GW1N(R)-9C")
    assert fpga.confidence == Confidence.IDENTIFIED
    assert any("openFPGALoader's database" in e.detail for e in fpga.evidence), "the evidence says which database decided"
    assert fabric_port(b).path == "/dev/ttyUSB1" and next(t for t in b.targets.values() if t.hosted_on).hosted_on == fpga.id
    tang = next(d for d in b.devices.values() if d.vid == 0x0403)
    assert tang.name == "SIPEED JTAG Debugger" and all(" " not in pid for pid in b.ports)  # what the BL702 debugger calls itself


def _bench(tmp_path, usb, serial):
    data = json.loads(FIXTURE.read_text())
    data.update(usb=usb, serial=serial, visa={}, listen={})
    f = tmp_path / "bench.json"
    f.write_text(json.dumps(data))
    return recon.build(FixtureIo(f))


def fabric_port(b):
    fab = next(t for t in b.targets.values() if t.hosted_on)
    (p,) = b.ports_reaching(fab.id)
    return p


DIGILENT_USB = [{"name": "Digilent USB Device", "vendor": "Digilent", "vid": "0x0403", "pid": "0x6010", "serial": "210319A8C7D1", "location": "x"}]


@pytest.mark.assumed
def test_windows_ft2232h_one_device_channel_from_serial_letter(tmp_path):
    # Windows: channel A is on D2XX for JTAG, so only channel B shows up as a COM port,
    # with the channel letter appended to the serial.
    b = _bench(tmp_path, DIGILENT_USB, [{"device": "COM7", "vid": "0x0403", "pid": "0x6010", "serial_number": "210319A8C7D1B"}])
    assert len(b.devices) == 1
    assert fabric_port(b).path == "COM7"
    assert any(p.kind == "jtag" for p in b.ports.values())





@pytest.mark.assumed  # macOS is not a target; the one macOS recording (Tang Nano) named its ports by location instead
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


WIN_LOCALE = json.loads((Path(__file__).parent.parent / "fixtures" / "real" / "windows_locale_2026-11-08.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("key", [k for k in WIN_LOCALE if "LC_CTYPE" in k])
def test_windows_locale_names_as_recorded(key):
    """Each locale name the C runtime gave on the Windows VM (both forms: 'Japanese_Japan.932' and 'ja-JP'; Python's
    getlocale() raises on the second) says its language (audit 2026-11-07, T8: these were made up)."""
    name = WIN_LOCALE[key]
    assert windows_locale_code(name) == ("ja" if name.lower().startswith(("ja", "japanese")) else "en"), name


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
    assert parse_windows_pnp("") == [] and parse_windows_pnp("not json") == []
    assert len(parse_windows_pnp(json.dumps(WIN["pnp"][-1]))) == 1  # PowerShell prints one object, not a list, for one device
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
    assert ftdi.access == [{"need": "driver", "interfaces": [0, 1]}] and not b.ports_of(ftdi.id), "nothing can open it, so no port"
    text = report.render(recon.Recon(b))
    assert "■ JTAG Debugger  SN FactoryAIOT Pro" in text and "no driver for its USB interface(s) 0, 1" in text
    assert "■ Espressif USB JTAG/serial debug unit" in text


def _tang_nano_on_windows(jtag_driver):
    """A Tang Nano 9K on an ordinary Windows PC: Windows Update gave both FT2232 interfaces FTDI's serial driver
    (two COM ports); someone may have put WinUSB on interface 0 for openFPGALoader (Zadig)."""
    pnp = [{"id": "USB\\VID_0403&PID_6010\\FACTORYAIOT_PRO", "status": "OK", "service": "usbccgp", "bus": None, "parent": None},
           {"id": "USB\\VID_0403&PID_6010&MI_00\\6&1&0&0000", "status": "OK", "service": jtag_driver, "bus": "JTAG Debugger",
            "parent": "USB\\VID_0403&PID_6010\\FactoryAIOT_Pro"},
           {"id": "USB\\VID_0403&PID_6010&MI_01\\6&1&0&0001", "status": "OK", "service": "FTDIBUS", "bus": "JTAG Debugger",
            "parent": "USB\\VID_0403&PID_6010\\FactoryAIOT_Pro"}]
    strings = [{"vid": 0x0403, "pid": 0x6010, "manufacturer": "SIPEED", "product": "JTAG Debugger", "serial": "FactoryAIOT Pro"}]
    return [dict(d, vid=f"0x{d['vid']:04x}", pid=f"0x{d['pid']:04x}") for d in parse_windows_pnp(json.dumps(pnp), strings)]


@pytest.mark.assumed
def test_a_libusb_tool_behind_a_serial_driver_is_named_with_the_interface_that_needs_winusb(tmp_path):
    from benchscout import report

    serial = [{"device": "COM5", "vid": "0x0403", "pid": "0x6010", "serial_number": "FactoryAIOT ProB",  # FTDI's driver adds the channel letter
               "description": "USB Serial Port (COM5)"}]
    b = _bench(tmp_path, _tang_nano_on_windows("FTDIBUS"), serial)
    dev = next(d for d in b.devices.values() if d.vid == 0x0403)
    assert dev.drivers == {0: "FTDIBUS", 1: "FTDIBUS"} and [(a["need"], a["interfaces"]) for a in dev.access] == [("winusb", [0])]
    text = report.render(recon.Recon(b))  # said once, in one form for every OS (model.Device.access)
    assert "interface(s) 0 have the FTDIBUS driver" in text and "WinUSB" in text

    b2 = _bench(tmp_path, _tang_nano_on_windows("WinUSB"), serial)  # after Zadig: nothing to say
    dev2 = next(d for d in b2.devices.values() if d.vid == 0x0403)
    assert dev2.access == []


WIN_TANG_NO_DRIVER = json.loads((Path(__file__).parent.parent / "fixtures" / "real" / "windows_arm64_tangnano_nodriver_2026-11-08.json")
                                .read_text(encoding="utf-8"))


def test_an_interface_windows_lists_as_ok_without_a_driver_has_none(tmp_path):
    """Windows VM, 2026-11-08 (audit 2026-11-07): with no FTDI driver for ARM64, Windows listed the Tang Nano's JTAG
    interface as OK, no problem, no service, and only the other one as failed; benchscout said only interface 1 lacked a
    driver. (Through the hub this FT2232 answered only its serial there, so no board wiring applies: an unknown device.)"""
    usb = [dict(d, vid=f"0x{d['vid']:04x}", pid=f"0x{d['pid']:04x}")
           for d in parse_windows_pnp(json.dumps(WIN_TANG_NO_DRIVER["pnp"]), WIN_TANG_NO_DRIVER["strings"]) if d["vid"] == 0x0403]
    assert [(u["vendor"], u["name"], u["no_driver"]) for u in usb] == [(None, "JTAG Debugger", [0, 1])]
    b = _bench(tmp_path, usb, WIN_TANG_NO_DRIVER["serial"])
    dev = next(d for d in b.devices.values() if d.vid == 0x0403)
    assert dev.access == [{"need": "driver", "interfaces": [0, 1]}], "both interfaces: nothing can open either"


def test_nothing_is_said_about_drivers_off_windows(tmp_path):
    b = recon.build(FixtureIo(FIXTURE))
    assert all(not d.drivers and not d.access for d in b.devices.values())


def test_linux_a_libusb_tool_the_user_cannot_open_is_named_with_the_fix_not_tried(tmp_path):
    """Ubuntu VM with the real Tang Nano, 2026-11-02: openFPGALoader came from the distribution with its udev rule, but
    the board was plugged in before, so its /dev/bus/usb node stayed root's; the tool failed ("unable to open ftdi
    device: -4") and the recon's JTAG read failed without saying why. Now: say what is needed, and do not try."""
    from benchscout import report

    usb = [dict(d, vid=f"0x{d['vid']:04x}", pid=f"0x{d['pid']:04x}",
                **({"usb_writable": False, "usb_node": "/dev/bus/usb/003/002"} if d["vid"] == 0x0403 else {}))
           for d in linux_usb_devices(_sysfs(tmp_path / "sys"))]
    serial = [dict(p, vid=f"0x{p['vid']:04x}", pid=f"0x{p['pid']:04x}") for p in LINUX["serial"]]
    data = json.loads(FIXTURE.read_text())
    data.update(usb=usb, serial=serial, visa={}, listen={}, tools=["openFPGALoader"])
    f = tmp_path / "bench.json"
    f.write_text(json.dumps(data))
    io = FixtureIo(f)
    r = recon.run(io)
    tang = next(d for d in r.bench.devices.values() if d.vid == 0x0403)
    assert [a["need"] for a in tang.access] == ["udev"]
    assert not [c for c in io.calls if c[0] == "probe"], "not tried: it cannot open the device"
    text = report.render(r)
    assert "udev rule" in text and "/dev/bus/usb/003/002" in text and "udevadm trigger" in text


def test_a_windows_hub_is_known_by_its_driver_even_when_its_name_is_not_returned():
    """Windows 11 ARM64 VM, 2026-11-03: the VM's USB hub came and went in the device list while the Tang Nano was
    plugged and unplugged: its reported name ("QEMU USB Hub") was returned in one scan and not the next, and hubs were
    left out by name only. Its driver (USBHUB3) is always there."""
    rec = json.loads((Path(__file__).parent.parent / "fixtures" / "real" / "windows_arm64_pnp_hub_2026-11-03.json")
                      .read_text(encoding="utf-8"))
    nameless = [dict(e, bus=None) for e in rec["pnp"]]  # as in the scan that listed it as "USB 0409:55aa"
    for pnp in (rec["pnp"], nameless):
        hubs = [d for d in parse_windows_pnp(json.dumps(pnp), rec["strings"]) if d.get("device_class") == 0x09]
        assert [(d["vid"], d["pid"]) for d in hubs] == [(0x0409, 0x55AA)]
