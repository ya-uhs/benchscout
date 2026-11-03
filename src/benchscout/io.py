"""L0 discovery (enumerate, open nothing) and L1 access (open and talk).

Both come in two flavours behind one interface: Live talks to real hardware,
Fixture replays a recorded/simulated bench from JSON so everything above it can
be developed and tested without a board on the desk.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import time
from pathlib import Path
from typing import Protocol

# A listen that loses its port (reset, re-enumeration) waits for it and reads on.
VANISHED_MARK = "[benchscout] port vanished (device reset or re-enumerated)"
REOPENS = 2
REAPPEAR_S = 3.0
AFTER_REOPEN_S = 2.0



class Io(Protocol):
    # discovery: must not open any port
    def usb_devices(self) -> list[dict]: ...
    def serial_ports(self) -> list[dict]: ...
    def visa_resources(self) -> list[str]: ...
    def tools(self) -> dict[str, str | None]: ...
    # what is plugged in, cheaply enough to ask every few seconds (noticing plugs and unplugs, design.md 17)
    def plugged(self) -> frozenset[tuple]: ...

    # access: opens things; callers go through policy first
    def visa_query(self, resource: str, command: str) -> str | None: ...
    def visa_query_raw(self, resource: str, command: str) -> bytes | None: ...  # the answer's bytes (a block, an image)
    def visa_write(self, resource: str, command: str) -> bool: ...  # True once sent
    def serial_listen(self, path: str, seconds: float, baud: int, exclusive: bool = False, lines: str = "low",
                      send: bytes | None = None, until: re.Pattern | None = None) -> str: ...
    # `send` is written once, right after opening; a whole line matching `until` ends the read early (codec.py)
    def run_probe(self, argv: list[str], timeout: float) -> str | None: ...  # a probe tool's output (tools.toml `probe`)


def ieee_block(data: bytes) -> bytes:
    """The data of an IEEE 488.2 definite-length block (#<n><length, n digits><bytes>), or the answer as it came if it
    is not one. An indefinite block (#0...) runs to the end, less the newline that ends the message."""
    if len(data) >= 2 and data[:1] == b"#" and data[1:2].isdigit():
        n = int(data[1:2])
        if n == 0:
            return data[2:].rstrip(b"\n")
        if len(data) >= 2 + n and data[2:2 + n].isdigit():
            length = int(data[2:2 + n])
            return data[2 + n:2 + n + length]
    return data


def run_tool(argv: list[str], timeout: float, cwd: str | Path | None = None) -> tuple[str, int | None]:
    """Run an external program and return (stdout + stderr, exit code; None if it was stopped at `timeout`).

    Its stdin is empty, never the core's: the core's stdin is the control channel, and on Windows a child that
    inherits a pipe another thread is reading waits for that read to finish (esptool sat there until the next line
    on the control channel, Windows VM 2026-11-02). At the time limit the whole process tree is stopped: tools built
    with PyInstaller leave a child holding the output pipes, and waiting on those never ends."""
    posix = platform.system() != "Windows"
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd,
                         text=True, encoding="utf-8", errors="replace", start_new_session=posix)
    try:
        out, err = p.communicate(timeout=timeout)
        return (out or "") + (err or ""), p.returncode
    except subprocess.TimeoutExpired:
        if platform.system() == "Windows":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], stdin=subprocess.DEVNULL, capture_output=True)
        else:  # its own process group: the children it started go too (one may hold the port)
            import signal

            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                p.kill()
        try:
            out, err = p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return (out or "") + (err or "") + f"\n(stopped after {timeout:g} s)", None


class PortGone(OSError):
    """The port vanished (the device reset or re-enumerated, or was unplugged)."""


class _LiveStream:
    """A serial port held open (design.md section 17: watching a selected device)."""

    def __init__(self, s):
        self.s = s

    def read(self, timeout: float) -> bytes:
        import serial

        try:
            self.s.timeout = timeout
            return self.s.read(4096)
        except (serial.SerialException, OSError) as e:
            raise PortGone(str(e)) from e

    def write(self, data: bytes) -> None:
        import serial

        try:
            self.s.write(data)
            self.s.flush()
        except (serial.SerialException, OSError) as e:
            raise PortGone(str(e)) from e

    def close(self) -> None:
        try:
            self.s.close()
        except Exception:
            pass


def _exclusive(s) -> None:
    """POSIX: TIOCEXCL on the open tty, so no other program can open it while benchscout holds it (pyserial's
    `exclusive` is an flock, which only programs that also take it respect). Windows COM ports are exclusive anyway."""
    if platform.system() == "Windows":
        return
    import fcntl
    import termios

    try:
        fcntl.ioctl(s.fileno(), termios.TIOCEXCL)
    except (OSError, AttributeError, ValueError):
        pass  # (a fixture or a port without a tty: nothing to hold)


class LiveIo:
    def serial_stream(self, path: str, baud: int = 115200, exclusive: bool = False, lines: str = "low") -> _LiveStream:
        import serial

        s = serial.Serial()
        s.port, s.baudrate, s.timeout = path, baud, 0.1
        if exclusive and platform.system() != "Windows":
            s.exclusive = True
        if lines == "low":
            s.dtr = False
            s.rts = False
        s.open()
        if exclusive:
            _exclusive(s)
        return _LiveStream(s)

    def port_present(self, path: str) -> bool:
        if path.startswith("/dev/"):
            return os.path.exists(path)
        from serial.tools import list_ports

        return any(p.device == path for p in list_ports.comports())

    def usb_devices(self) -> list[dict]:
        system = platform.system()
        if system == "Linux":
            return linux_usb_devices(Path("/sys/bus/usb/devices"))
        if system == "Windows":
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_PNP_QUERY],
                                 stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
            from .winusb_strings import usb_strings

            try:
                strings = usb_strings()
            except OSError:
                strings = []
            return parse_windows_pnp(out, strings)
        if system != "Darwin":
            return []
        out = subprocess.run(
            ["/usr/sbin/system_profiler", "SPUSBHostDataType", "-json"], stdin=subprocess.DEVNULL, capture_output=True, text=True
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

    def plugged(self) -> frozenset[tuple]:
        """(vid, pid, serial, location) of each USB device and ("port", path) of each serial port, from the OS's own
        lists: nothing is opened and no device is asked anything (on Windows the hubs answer from what they hold; no
        string descriptors are read). Ports too: a device handed to a VM stays in macOS's USB list and only its port
        goes (real bench, 2026-11-03, UTM)."""
        from serial.tools import list_ports

        system = platform.system()
        if system == "Darwin":
            out = subprocess.run(["/usr/sbin/ioreg", "-r", "-c", "IOUSBHostDevice", "-w0", "-a", "-d1"], stdin=subprocess.DEVNULL,
                                 capture_output=True, timeout=10).stdout
            found = parse_ioreg(out)
        elif system == "Linux":
            found = linux_usb_devices(Path("/sys/bus/usb/devices"))
        elif system == "Windows":
            from .winusb_strings import usb_strings

            try:
                found = usb_strings(read_strings=False)
            except OSError:
                found = []
        else:
            found = []
        return frozenset([(u["vid"], u["pid"], u.get("serial"), u.get("location")) for u in found]
                         + [("port", p.device) for p in list_ports.comports() if p.vid is not None])

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
        import warnings

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # pyvisa-py: optional psutil/zeroconf notices
                return list(self._visa().list_resources())
        except Exception:
            return []

    def tools(self) -> dict[str, str | None]:
        from .fetch import find

        from . import knowledge as K

        return {t: find(t) for t in K.tool_names()}  # also the ones shipped with benchscout or fetched before

    def visa_query(self, resource: str, command: str) -> str | None:
        try:
            inst = self._visa().open_resource(resource, timeout=1500)
            try:
                return inst.query(command).strip()
            finally:
                inst.close()
        except Exception:
            return None

    def visa_query_raw(self, resource: str, command: str) -> bytes | None:
        """The whole answer as bytes, for a screen capture or a waveform (seconds, and megabytes, on some scopes)."""
        try:
            inst = self._visa().open_resource(resource, timeout=15000)
            try:
                inst.write(command)
                return bytes(inst.read_raw())
            finally:
                inst.close()
        except Exception:
            return None

    def visa_write(self, resource: str, command: str) -> bool:
        try:
            inst = self._visa().open_resource(resource, timeout=1500)
            try:
                inst.write(command)
                return True
            finally:
                inst.close()
        except Exception:
            return False

    def serial_listen(self, path: str, seconds: float, baud: int = 115200, exclusive: bool = False,
                      lines: str = "low", send: bytes | None = None, until: re.Pattern | None = None) -> str:
        """Read for `seconds` (after writing `send`, if given), or until a whole line matches `until`. If the port vanishes (a native-USB chip resets on open and
        re-enumerates; real bench 2026-10-31), wait for it to come back and keep reading, so
        the boot log is caught. Reopening can reset the chip again, so at most REOPENS times."""
        import serial

        def open_port():
            s = serial.Serial()
            s.port, s.baudrate, s.timeout = path, baud, 0.1
            if exclusive and platform.system() != "Windows":
                # TIOCEXCL: while we hold a selected device's port, nothing else (an agent's
                # shell, another tool) can open it. Windows COM ports are exclusive anyway.
                s.exclusive = True
            if lines == "low":
                # Preset both lines low so opening does not request a reset/bootloader.
                # Some OS drivers still pulse DTR on open; policy marks those ports.
                s.dtr = False
                s.rts = False
            s.open()
            if exclusive:
                _exclusive(s)
            return s

        start = time.monotonic()
        end = start + seconds
        buf = bytearray()
        s = open_port()
        if send:
            s.write(send)
            s.flush()
        reopens = 0
        try:
            from .codec import ended

            while time.monotonic() < end:
                try:
                    chunk = s.read(4096)  # (0.1 s timeout: an empty read means the output has settled)
                    buf += chunk
                    if ended(bytes(buf), until, settled=not chunk):
                        break
                    continue
                except (serial.SerialException, OSError) as e:
                    s.close()
                    gone_at = time.monotonic() - start
                    s = None
                    if reopens < REOPENS:
                        s = self._wait_reopen(open_port, max(end, time.monotonic() + REAPPEAR_S))
                    if s is None:
                        why = f"vanished again after {reopens} reopen(s)" if reopens >= REOPENS else "did not come back"
                        if not buf.strip():
                            raise OSError(6, f"port {why} (at {gone_at:.1f} s): {e}") from e
                        # keep what was heard; it may hold the boot log
                        buf += f"\n{VANISHED_MARK} at {gone_at:.1f} s; {why}\n".encode()
                        return buf.decode("utf-8", errors="replace")
                    reopens += 1
                    buf += f"\n{VANISHED_MARK} at {gone_at:.1f} s; reopened at {time.monotonic() - start:.1f} s\n".encode()
                    end = max(end, time.monotonic() + AFTER_REOPEN_S)  # give the boot log time to arrive
        finally:
            if s is not None:
                s.close()
        return buf.decode("utf-8", errors="replace")

    @staticmethod
    def _wait_reopen(open_port, deadline: float):
        while time.monotonic() < deadline:
            time.sleep(0.1)
            try:
                return open_port()
            except Exception:
                continue
        return None

    def run_probe(self, argv: list[str], timeout: float) -> str | None:
        text, code = run_tool(argv, timeout)
        return text if code is not None else None


WINDOWS_PNP_QUERY = (  # UTF-8 out: Windows PowerShell would print a device's name in the console's code page
    "[Console]::OutputEncoding = [Text.Encoding]::UTF8; $all = @(Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'USB\\VID_*' }); $prop = @{}; "
    "if ($all.Count) { Get-PnpDeviceProperty -InstanceId $all.InstanceId -KeyName DEVPKEY_Device_BusReportedDeviceDesc, "
    "DEVPKEY_Device_Parent -ErrorAction SilentlyContinue | ForEach-Object { $prop[\"$($_.InstanceId)|$($_.KeyName)\"] = $_.Data } }; "
    "$all | ForEach-Object { [pscustomobject]@{ id = $_.InstanceId; status = [string]$_.Status; problem = [string]$_.Problem; "
    "service = [string]$_.Service; "
    "bus = $prop[\"$($_.InstanceId)|DEVPKEY_Device_BusReportedDeviceDesc\"]; "
    "parent = $prop[\"$($_.InstanceId)|DEVPKEY_Device_Parent\"] } } | ConvertTo-Json -Compress"
)
WINDOWS_HUB_DRIVERS = ("USBHUB3", "USBHUB")  # Windows' own USB hub drivers (USB 3 and USB 2)
_PNP_ID = re.compile(r"^USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})(?:&MI_([0-9A-F]+))?\\(.+)$", re.I)


def _same_serial(a: str, b: str) -> bool:
    """Windows writes a serial into the instance id upper-cased, spaces as '_' ("FactoryAIOT Pro" -> FACTORYAIOT_PRO)."""
    norm = lambda x: re.sub(r"[^0-9A-Z]", "", x.upper())  # noqa: E731
    return norm(a) == norm(b)


def parse_windows_pnp(text: str, strings: list[dict] | None = None) -> list[dict]:
    """USB devices from Get-PnpDevice (WINDOWS_PNP_QUERY): each device, not its &MI_xx interfaces, named as it names
    itself: from its string descriptors (`strings`, winusb_strings.usb_strings()) or else the product name Windows
    recorded from the bus (BusReportedDeviceDesc). Never a driver's name ("USB Composite Device") or an INF's maker
    ("(Standard USB Host Controller)"): those are not the device's evidence. `no_driver`: the interfaces Windows has no
    working driver for, which no program can open until one is installed. An instance id with '&' was made up by
    Windows, not a serial number."""
    try:
        data = json.loads(text) if text.strip() else []
    except json.JSONDecodeError:
        return []
    entries = [e for e in (data if isinstance(data, list) else [data]) if isinstance(e, dict)]
    parsed = [(m, e) for e in entries if (m := _PNP_ID.match(e.get("id") or e.get("PNPDeviceID") or ""))]
    parents = [(m, e) for m, e in parsed if m.group(3) is None]
    children = [(m, e) for m, e in parsed if m.group(3) is not None]
    out = []
    for m, e in parents:
        vid, pid, inst = int(m.group(1), 16), int(m.group(2), 16), m.group(4)
        key = (e.get("id") or e.get("PNPDeviceID") or "").upper()
        same_ids = [p for p in parents if (p[0].group(1), p[0].group(2)) == (m.group(1), m.group(2))]
        kids = [(cm, ce) for cm, ce in children if (ce.get("parent") or "").upper() == key
                or not ce.get("parent") and len(same_ids) == 1 and (cm.group(1), cm.group(2)) == (m.group(1), m.group(2))]
        serial = None if "&" in inst else inst
        own = [x for x in strings or [] if (x["vid"], x["pid"]) == (vid, pid)
               and (serial and x.get("serial") and _same_serial(x["serial"], serial) or not serial and len(same_ids) == 1)]
        desc = own[0] if len(own) == 1 else {}
        bus = e.get("bus") or next((ce.get("bus") for _, ce in kids if ce.get("bus")), None)
        bad = lambda x: (x.get("status") or "OK").upper() != "OK"  # noqa: E731
        no_driver = sorted(int(cm.group(3), 16) for cm, ce in kids if bad(ce)) or ([0] if not kids and bad(e) else [])
        # which driver each interface has (its service: usbser, FTDIBUS, WinUSB ...), for tools that need a certain kind
        drivers = {int(cm.group(3), 16): ce["service"] for cm, ce in kids if ce.get("service") and not bad(ce)} \
            or ({0: e["service"]} if not kids and e.get("service") and not bad(e) else {})
        # a hub, as Windows says by driving it with its own hub driver: the name it reports is not always returned
        # (real VM, 2026-11-03: "QEMU USB Hub" in one scan, nothing in the next, so it came and went)
        hub = (e.get("service") or "").upper() in WINDOWS_HUB_DRIVERS
        out.append({"name": desc.get("product") or bus, "vendor": desc.get("manufacturer"), "vid": vid, "pid": pid,
                    "serial": desc.get("serial") or serial, "location": None if serial else inst, "no_driver": no_driver,
                    "drivers": drivers, **({"device_class": 0x09} if hub else {})})
    return out


def linux_usb_devices(root: Path) -> list[dict]:
    """USB devices from sysfs (/sys/bus/usb/devices). Interface entries (1-1:1.0) are skipped."""
    def read(d: Path, name: str) -> str | None:
        try:
            return (d / name).read_text(encoding="utf-8", errors="replace").strip() or None
        except OSError:
            return None

    out: list[dict] = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        vid = read(d, "idVendor")
        if ":" in d.name or not vid:
            continue
        cls = read(d, "bDeviceClass")
        bus, dev = read(d, "busnum"), read(d, "devnum")
        # whether this user may open the device itself (what libusb tools do): its node under /dev/bus/usb
        node = Path(f"/dev/bus/usb/{int(bus):03d}/{int(dev):03d}") if bus and dev and bus.isdigit() and dev.isdigit() else None
        out.append({"name": read(d, "product"), "vendor": read(d, "manufacturer"), "vid": int(vid, 16),
                    "pid": int(read(d, "idProduct") or "0", 16), "serial": read(d, "serial"), "location": d.name,
                    "device_class": int(cls, 16) if cls else None,
                    "usb_node": str(node) if node else None,
                    "usb_writable": os.access(node, os.W_OK) if node and node.exists() else None})
    return out


def parse_ioreg(data: bytes) -> list[dict]:
    """USB devices from `ioreg -r -c IOUSBHostDevice -w0 -a -d1` (macOS): a plist array, one dict per device."""
    import plistlib

    try:
        items = plistlib.loads(data) if data.strip() else []
    except Exception:  # plistlib raises several kinds on a cut-off answer
        return []
    out = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and isinstance(it.get("idVendor"), int):
            out.append({"name": it.get("USB Product Name"), "vendor": it.get("USB Vendor Name"), "vid": it["idVendor"],
                        "pid": it.get("idProduct", 0), "serial": it.get("USB Serial Number"),
                        "location": f"0x{it['locationID']:08x}" if isinstance(it.get("locationID"), int) else None,
                        "device_class": it.get("bDeviceClass")})
    return out


class _FixtureStream:
    """A scripted port: `streams[path]` is a list of {"after": s, "text": ...} or {"after": s, "vanish": s}."""

    def __init__(self, io: "FixtureIo", path: str):
        self.io, self.path, self.t0 = io, path, time.monotonic()
        self.script = list(io.data.get("streams", {}).get(path, []))
        self.pending = b""

    def read(self, timeout: float) -> bytes:
        end = time.monotonic() + timeout
        while True:
            if self.pending:
                out, self.pending = self.pending, b""
                return out
            if not self.io.port_present(self.path):  # taken out of the fixture file: unplugged
                raise PortGone("Device not configured (fixture)")
            now = time.monotonic() - self.t0
            if self.script and self.script[0]["after"] <= now:
                ev = self.script.pop(0)
                if "vanish" in ev:
                    self.io.gone_until[self.path] = time.monotonic() + ev["vanish"]
                    raise PortGone("Device not configured (fixture)")
                return ev["text"].encode()
            if time.monotonic() >= end:
                return b""
            time.sleep(0.01)

    def write(self, data: bytes) -> None:
        self.io.calls.append(("serial_write", self.path, data))
        reply = self.io.data.get("replies", {}).get(self.path, {}).get(data.decode(errors="replace"))
        if reply:
            self.pending += reply.encode()

    def close(self) -> None:
        pass


class FixtureIo:
    """Replays a bench described in JSON (see fixtures/)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text(encoding="utf-8"))
        self._mtime = self.path.stat().st_mtime_ns
        self.calls: list[tuple] = []  # what access was attempted, for tests/audit
        self.exclusive_opens: list[str] = []
        self.bauds: list[int] = []
        self.lines: list[str] = []
        self.gone_until: dict[str, float] = {}
        self.unplugged: set[str] = set()  # serial ports taken out of the file since it was first read
        self.streams_opened: list[str] = []

    def serial_stream(self, path: str, baud: int = 115200, exclusive: bool = False, lines: str = "low") -> _FixtureStream:
        if not self.port_present(path):
            raise PortGone("not present (fixture)")
        self.calls.append(("serial_stream", path))
        self.streams_opened.append(path)
        self.bauds.append(baud)
        self.lines.append(lines)
        if exclusive:
            self.exclusive_opens.append(path)
        st = _FixtureStream(self, path)
        if getattr(self, "_streams", None) is None:
            self._streams: dict[str, _FixtureStream] = {}
        prev = self._streams.get(path)
        if prev:  # a reopen continues the same script
            st.script, st.t0 = prev.script, prev.t0
        self._streams[path] = st
        return st

    def port_present(self, path: str) -> bool:
        self._reread()
        return path not in self.unplugged and time.monotonic() >= self.gone_until.get(path, 0)

    def _reread(self) -> None:
        """The fixture file is the bench: writing a device into it (or taking one out) plugs (unplugs) it."""
        try:
            m = self.path.stat().st_mtime_ns
            if m != self._mtime:
                paths = lambda d: {s["device"] for s in d.get("serial", [])}  # noqa: E731
                was = paths(self.data)
                self.data, self._mtime = json.loads(self.path.read_text(encoding="utf-8")), m
                self.unplugged = (self.unplugged | (was - paths(self.data))) - paths(self.data)
        except (OSError, json.JSONDecodeError):
            pass  # half written: read it next time

    def plugged(self) -> frozenset[tuple]:
        return frozenset([(u["vid"], u["pid"], u.get("serial"), u.get("location")) for u in self.usb_devices()]
                         + [("port", p["device"]) for p in self.serial_ports()])

    def usb_devices(self) -> list[dict]:
        self._reread()
        return [dict(d, vid=int(d["vid"], 16), pid=int(d["pid"], 16)) for d in self.data.get("usb", [])]

    def serial_ports(self) -> list[dict]:
        self._reread()
        return [dict(p, vid=int(p["vid"], 16), pid=int(p["pid"], 16)) for p in self.data.get("serial", [])]

    def visa_resources(self) -> list[str]:
        return list(self.data.get("visa", {}).keys())

    def tools(self) -> dict[str, str | None]:
        from . import knowledge as K

        have = self.data.get("tools", [])
        return {t: (f"/fixture/bin/{t}" if t in have else None) for t in K.tool_names()}

    def visa_query_raw(self, resource: str, command: str) -> bytes | None:
        self.calls.append(("visa_query_raw", resource, command))
        v = self.data.get("visa_raw", {}).get(resource, {}).get(command)  # recorded as hex
        return bytes.fromhex(v) if v is not None else None

    def visa_query(self, resource: str, command: str) -> str | None:
        self.calls.append(("visa_query", resource, command))
        return self.data.get("visa", {}).get(resource, {}).get(command)

    def visa_write(self, resource: str, command: str) -> bool:
        self.calls.append(("visa_write", resource, command))
        return resource in self.data.get("visa", {})

    def serial_listen(self, path: str, seconds: float, baud: int = 115200, exclusive: bool = False,
                      lines: str = "low", send: bytes | None = None, until: re.Pattern | None = None) -> str:
        if send is not None:  # what a device answers is recorded per sent text in "replies"
            self.calls.append(("serial_send", path, send))
            return self.data.get("replies", {}).get(path, {}).get(send.decode(errors="replace"), "")
        self.calls.append(("serial_listen", path))
        self.lines.append(lines)
        if exclusive:
            self.exclusive_opens.append(path)
        self.bauds.append(baud)
        v = self.data.get("listen", {}).get(path, "")
        if isinstance(v, dict) and "then" in v:  # vanishes, comes back, keeps printing
            return f"\n{VANISHED_MARK} at 0.1 s; reopened at 0.9 s\n" + v["then"]
        if isinstance(v, dict) and "error" in v:  # simulate a port that vanishes for good
            raise OSError(6, v["error"])
        return v

    def run_probe(self, argv: list[str], timeout: float) -> str | None:
        """A probe tool's recorded output, found by any of its arguments (a probe's serial, say) in "probes"."""
        self.calls.append(("probe", tuple(argv)))
        recorded = self.data.get("probes", {})
        return next((recorded[a] for a in argv if a in recorded), "")
