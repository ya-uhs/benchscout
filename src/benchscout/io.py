"""L0 discovery (enumerate, open nothing) and L1 access (open and talk).

Both come in two flavours behind one interface: Live talks to real hardware,
Fixture replays a recorded/simulated bench from JSON so everything above it can
be developed and tested without a board on the desk.
"""

from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Protocol

TOOLS = ("esptool.py", "esptool", "openocd", "openFPGALoader", "probe-rs", "sigrok-cli")


class Io(Protocol):
    # discovery: must not open any port
    def usb_devices(self) -> list[dict]: ...
    def serial_ports(self) -> list[dict]: ...
    def visa_resources(self) -> list[str]: ...
    def tools(self) -> dict[str, str | None]: ...

    # access: opens things; callers go through policy first
    def visa_query(self, resource: str, command: str) -> str | None: ...
    def serial_listen(self, path: str, seconds: float, baud: int, exclusive: bool = False) -> str: ...
    def jtag_scan(self, usb_serial: str | None) -> list[dict]: ...  # [{idcode, manufacturer, model}]


class LiveIo:
    def usb_devices(self) -> list[dict]:
        system = platform.system()
        if system == "Linux":
            return linux_usb_devices(Path("/sys/bus/usb/devices"))
        if system == "Windows":
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_PNP_QUERY],
                                 capture_output=True, text=True).stdout
            return parse_windows_pnp(out)
        if system != "Darwin":
            return []
        out = subprocess.run(
            ["system_profiler", "SPUSBHostDataType", "-json"], capture_output=True, text=True
        ).stdout
        try:
            root = json.loads(out).get("SPUSBHostDataType", [])
        except json.JSONDecodeError:
            return []
        found: list[dict] = []

        def walk(items: list[dict]) -> None:
            for it in items:
                if "USBDeviceKeyVendorID" in it:
                    sn = it.get("USBDeviceKeySerialNumber")
                    found.append(
                        {
                            "name": it.get("_name"),
                            "vendor": it.get("USBDeviceKeyVendorName"),
                            "vid": int(it["USBDeviceKeyVendorID"], 16),
                            "pid": int(it.get("USBDeviceKeyProductID", "0x0"), 16),
                            "serial": None if sn in (None, "Not Provided") else sn,
                            "location": it.get("USBKeyLocationID"),
                        }
                    )
                walk(it.get("_items", []))

        walk(root)
        return found

    def serial_ports(self) -> list[dict]:
        from serial.tools import list_ports

        return [
            {
                "device": p.device,
                "vid": p.vid,
                "pid": p.pid,
                "serial_number": p.serial_number,
                "location": p.location,
                "interface": p.interface,
                "description": p.description,
            }
            for p in list_ports.comports()
            if p.vid is not None  # skip Bluetooth/debug-console pseudo ports
        ]

    def _visa(self):
        """NI-VISA / Keysight VISA if installed (common on Windows benches, and the only easy
        way to USBTMC there), else pure-Python pyvisa-py."""
        if getattr(self, "_rm", None) is None:
            import warnings

            import pyvisa

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # pyvisa-py nags about optional psutil/zeroconf
                try:
                    self._rm = pyvisa.ResourceManager()
                except Exception:
                    self._rm = pyvisa.ResourceManager("@py")
        return self._rm

    def visa_resources(self) -> list[str]:
        try:
            return list(self._visa().list_resources())
        except Exception:
            return []

    def tools(self) -> dict[str, str | None]:
        return {t: shutil.which(t) for t in TOOLS}

    def visa_query(self, resource: str, command: str) -> str | None:
        try:
            inst = self._visa().open_resource(resource, timeout=1500)
            try:
                return inst.query(command).strip()
            finally:
                inst.close()
        except Exception:
            return None

    def serial_listen(self, path: str, seconds: float, baud: int = 115200, exclusive: bool = False) -> str:
        import serial

        s = serial.Serial()
        s.port, s.baudrate, s.timeout = path, baud, 0.1
        if exclusive and platform.system() != "Windows":
            # TIOCEXCL: while we hold a selected device's port, nothing else (an agent's
            # shell, another tool) can open it. Windows COM ports are exclusive anyway.
            s.exclusive = True
        # Preset both lines low so opening does not request a reset/bootloader.
        # Some OS drivers still pulse DTR on open; policy marks those ports.
        s.dtr = False
        s.rts = False
        s.open()
        buf = bytearray()
        try:
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                buf += s.read(4096)
        finally:
            s.close()
        return buf.decode("utf-8", errors="replace")

    def jtag_scan(self, usb_serial: str | None) -> list[dict]:
        exe = shutil.which("openFPGALoader")
        if not exe:
            return []
        args = [exe, "--detect"] + (["--ftdi-serial", usb_serial] if usb_serial else [])
        out = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return parse_detect(out.stdout + out.stderr)


WINDOWS_PNP_QUERY = (
    "Get-CimInstance Win32_PnPEntity | Where-Object { $_.PNPDeviceID -like 'USB\\VID_*' } | "
    "Select-Object Name,Manufacturer,PNPDeviceID | ConvertTo-Json -Compress"
)
_PNP_ID = re.compile(r"^USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})(&MI_[0-9A-F]+)?\\(.+)$", re.I)


def parse_windows_pnp(text: str) -> list[dict]:
    """USB devices from Win32_PnPEntity. Interface children (&MI_xx) are skipped; an instance
    id containing '&' is generated by Windows, not a serial number."""
    try:
        data = json.loads(text) if text.strip() else []
    except json.JSONDecodeError:
        return []
    out = []
    for e in data if isinstance(data, list) else [data]:
        m = _PNP_ID.match(e.get("PNPDeviceID") or "")
        if not m or m.group(3):
            continue
        inst = m.group(4)
        serial = None if "&" in inst else inst
        out.append({"name": e.get("Name"), "vendor": e.get("Manufacturer"), "vid": int(m.group(1), 16),
                    "pid": int(m.group(2), 16), "serial": serial, "location": None if serial else inst})
    return out


def linux_usb_devices(root: Path) -> list[dict]:
    """USB devices from sysfs (/sys/bus/usb/devices). Interface entries (1-1:1.0) are skipped."""
    def read(d: Path, name: str) -> str | None:
        try:
            return (d / name).read_text().strip() or None
        except OSError:
            return None

    out = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        vid = read(d, "idVendor")
        if ":" in d.name or not vid:
            continue
        out.append({"name": read(d, "product"), "vendor": read(d, "manufacturer"), "vid": int(vid, 16),
                    "pid": int(read(d, "idProduct") or "0", 16), "serial": read(d, "serial"), "location": d.name})
    return out


def parse_detect(text: str) -> list[dict]:
    """Parse `openFPGALoader --detect`. Each `index N:` block carries the tool's own
    database lookup (manufacturer/family/model), which beats our small table.

        index 0:
            idcode 0x100481b
            manufacturer Gowin
            family GW1N
            model  GW1N(R)-9C
    """
    devices: list[dict] = []
    for line in text.splitlines():
        key, _, val = line.strip().partition(" ")
        val = val.strip()
        if key == "index":
            devices.append({"idcode": None, "manufacturer": None, "family": None, "model": None})
        elif devices and key in ("idcode", "manufacturer", "family", "model") and val:
            devices[-1][key] = int(val, 16) if key == "idcode" else val
    return [d for d in devices if d["idcode"] is not None]


class FixtureIo:
    """Replays a bench described in JSON (see fixtures/)."""

    def __init__(self, path: str | Path):
        self.data = json.loads(Path(path).read_text())
        self.calls: list[tuple] = []  # what access was attempted, for tests/audit
        self.exclusive_opens: list[str] = []
        self.bauds: list[int] = []

    def usb_devices(self) -> list[dict]:
        return [dict(d, vid=int(d["vid"], 16), pid=int(d["pid"], 16)) for d in self.data.get("usb", [])]

    def serial_ports(self) -> list[dict]:
        return [dict(p, vid=int(p["vid"], 16), pid=int(p["pid"], 16)) for p in self.data.get("serial", [])]

    def visa_resources(self) -> list[str]:
        return list(self.data.get("visa", {}).keys())

    def tools(self) -> dict[str, str | None]:
        have = self.data.get("tools", [])
        return {t: (f"/fixture/bin/{t}" if t in have else None) for t in TOOLS}

    def visa_query(self, resource: str, command: str) -> str | None:
        self.calls.append(("visa_query", resource, command))
        return self.data.get("visa", {}).get(resource, {}).get(command)

    def serial_listen(self, path: str, seconds: float, baud: int = 115200, exclusive: bool = False) -> str:
        self.calls.append(("serial_listen", path))
        if exclusive:
            self.exclusive_opens.append(path)
        self.bauds.append(baud)
        v = self.data.get("listen", {}).get(path, "")
        if isinstance(v, dict) and "error" in v:  # simulate a port that vanishes mid-read
            raise OSError(6, v["error"])
        return v

    def jtag_scan(self, usb_serial: str | None) -> list[dict]:
        self.calls.append(("jtag_scan", usb_serial))
        v = self.data.get("jtag", {}).get(usb_serial or "", [])
        if isinstance(v, str):  # recorded raw tool output
            return parse_detect(v)
        return [{"idcode": int(c, 16), "manufacturer": None, "family": None, "model": None} for c in v]
