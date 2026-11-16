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
import threading
import sys
import time
from pathlib import Path
from typing import Protocol

from .model import DEFAULT_BAUD

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
    # VISA: a failure (no VISA library, a resource busy or gone, no answer in time) raises OSError with VISA's words
    def visa_query(self, resource: str, command: str) -> str: ...
    def visa_query_raw(self, resource: str, command: str) -> bytes: ...  # the answer's bytes (a block, an image)
    def visa_write(self, resource: str, command: str) -> None: ...
    def serial_listen(self, path: str, seconds: float, baud: int, exclusive: bool = False, lines: str = "low",
                      send: bytes | None = None, until: re.Pattern | None = None, cancel=None, may_reopen=None) -> str: ...
    # `send` is written once, right after opening; a whole line matching `until` ends the read early (codec.py)
    def run_probe(self, argv: list[str], timeout: float) -> str | None: ...  # a probe tool's output (tools.toml `probe`)


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
        from .execution import kill_tree

        kill_tree(p)
        try:
            out, err = p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return (out or "") + (err or "") + f"\n(stopped after {timeout:g} s)", None


LISTING_S = 60.0  # how long the OS's USB list may take before a scan says it did not answer


def _listing(argv: list[str]) -> str:
    """The OS's own USB list (it opens nothing). One that does not answer stops the scan with that said, instead of
    holding the session (and the human's selections) forever, or passing for an empty bench."""
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=LISTING_S).stdout
    except subprocess.TimeoutExpired:
        raise OSError(f"{os.path.basename(argv[0])} did not list the USB devices within {LISTING_S:g} s") from None


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
    if sys.platform == "win32":
        return
    import fcntl
    import termios

    try:
        fcntl.ioctl(s.fileno(), termios.TIOCEXCL)
    except (OSError, AttributeError, ValueError):
        s.close()
        raise


class LiveIo:
    # VISA (pyvisa and the VISA library under it) is not made for several threads at once: one call at a time, whoever
    # makes it (recon hears different devices at the same time; design.md section 12, 2026-11-08)
    _visa_lock = threading.Lock()
    _pnp: tuple[float, str] | None = None  # Windows: the device list serial_ports() read with its query, and when

    def serial_stream(self, path: str, baud: int = DEFAULT_BAUD, exclusive: bool = False, lines: str = "low") -> _LiveStream:
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
            # the device tree serial_ports() read with its own query moments ago (recon lists ports first), else now
            cached, self._pnp = self._pnp, None
            out = cached[1] if cached and time.monotonic() - cached[0] < 10 else \
                _listing(["powershell", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_PNP_QUERY])
            from .winusb_strings import usb_strings

            try:
                strings = usb_strings()
            except OSError:
                strings = []
            return parse_windows_pnp(out, strings)
        if system != "Darwin":
            return []
        out = _listing(["/usr/sbin/system_profiler", "SPUSBHostDataType", "-json"])
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

        ports = [
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
        if ports and platform.system() == "Windows":
            # one PowerShell for both of Windows' lists (it takes a second to start; design.md section 12, 2026-11-08):
            # the ports' parents now, the USB devices for usb_devices(), which recon calls next
            both = _listing(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                             f"{WINDOWS_PNP_QUERY}; '{_SPLIT}'; {WINDOWS_PORT_PARENTS}"])
            pnp, _, port_text = both.partition(_SPLIT)
            self._pnp = (time.monotonic(), pnp)
            parents = parse_windows_port_parents(port_text)
            for p in ports:
                if p["device"] in parents:
                    p["usb_interface"] = parents[p["device"]]
        return ports

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

    def _visa_call(self, resource: str, timeout_ms: int, use):
        """Open `resource`, use it, close it. A failure (no VISA library, a resource busy or gone, a time-out) raises
        OSError with VISA's own words: "no answer" would lose why (a raw clue for the agent and the person)."""
        try:
            with self._visa_lock:
                inst = self._visa().open_resource(resource, timeout=timeout_ms)
                try:
                    return use(inst)
                finally:
                    inst.close()
        except Exception as e:
            raise OSError(f"VISA {resource}: {type(e).__name__}: {e}") from e

    def visa_query(self, resource: str, command: str) -> str:
        return self._visa_call(resource, 1500, lambda inst: inst.query(command).strip())

    def visa_query_raw(self, resource: str, command: str) -> bytes:
        """The whole answer as bytes, for a screen capture or a waveform (seconds, and megabytes, on some scopes)."""
        def use(inst):
            inst.write(command)
            return bytes(inst.read_raw())
        return self._visa_call(resource, 15000, use)

    def visa_write(self, resource: str, command: str) -> None:
        self._visa_call(resource, 1500, lambda inst: inst.write(command))

    def serial_listen(self, path: str, seconds: float, baud: int = DEFAULT_BAUD, exclusive: bool = False,
                      lines: str = "low", send: bytes | None = None, until: re.Pattern | None = None, cancel=None, may_reopen=None) -> str:
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
        if cancel is not None and cancel.is_set():
            return ""
        s = open_port()
        reopens = 0
        try:
            if send and not (cancel is not None and cancel.is_set()):
                s.write(send)
                s.flush()
            from .codec import ended

            while time.monotonic() < end and not (cancel is not None and cancel.is_set()):
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
                    if reopens < REOPENS and may_reopen is not None:
                        s = self._wait_reopen(open_port, max(end, time.monotonic() + REAPPEAR_S),
                                              cancel, lambda: may_reopen(gone_at))
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
    def _wait_reopen(open_port, deadline: float, cancel, allowed):
        while time.monotonic() < deadline:
            if cancel is not None:
                if cancel.wait(0.1):
                    return None
            else:
                time.sleep(0.1)
            if not allowed():
                return None
            try:
                return open_port()
            except Exception:
                continue
        return None

    def run_probe(self, argv: list[str], timeout: float) -> str | None:
        """Its output, with its exit code when it failed; None if it did not finish in time."""
        text, code = run_tool(argv, timeout)
        if code is None:
            return None
        return text if code == 0 else f"{text}\n(exit code {code})"


WINDOWS_PNP_QUERY = (  # UTF-8 out: Windows PowerShell would print a device's name in the console's code page
    "[Console]::OutputEncoding = [Text.Encoding]::UTF8; $all = @(Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'USB\\VID_*' }); $prop = @{}; "
    "if ($all.Count) { Get-PnpDeviceProperty -InstanceId $all.InstanceId -KeyName DEVPKEY_Device_BusReportedDeviceDesc, "
    "DEVPKEY_Device_Parent -ErrorAction SilentlyContinue | ForEach-Object { $prop[\"$($_.InstanceId)|$($_.KeyName)\"] = $_.Data } }; "
    "$all | ForEach-Object { [pscustomobject]@{ id = $_.InstanceId; status = [string]$_.Status; problem = [string]$_.Problem; "
    "service = [string]$_.Service; "
    "bus = $prop[\"$($_.InstanceId)|DEVPKEY_Device_BusReportedDeviceDesc\"]; "
    "parent = $prop[\"$($_.InstanceId)|DEVPKEY_Device_Parent\"] } } | ConvertTo-Json -Compress"
)
# Which USB interface each COM port hangs off, as Windows' device tree says: the port's device node (FTDIBUS\..., or a
# usbser child) has a parent USB\VID_..&PID_..&MI_xx. pyserial does not say it for FTDI's driver (no location, no
# interface; real VM, 2026-11-08: both of a Tang Nano's ports were "SER=FACTORYAIOT_PRO"), so a multi-channel part's
# channel would be unknown
WINDOWS_PORT_PARENTS = (
    "[Console]::OutputEncoding = [Text.Encoding]::UTF8; @(Get-PnpDevice -Class Ports -PresentOnly | ForEach-Object { "
    "[pscustomobject]@{ port = (Get-ItemProperty -ErrorAction SilentlyContinue "
    "(\"HKLM:\\SYSTEM\\CurrentControlSet\\Enum\\\" + $_.InstanceId + \"\\Device Parameters\")).PortName; "
    "parent = (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName DEVPKEY_Device_Parent "
    "-ErrorAction SilentlyContinue).Data } }) | ConvertTo-Json -Compress"
)
_MI = re.compile(r"&MI_([0-9A-F]{2})\\", re.I)
_SPLIT = "BENCHSCOUT-NEXT-LIST"  # between the two lists one PowerShell prints


def parse_windows_port_parents(text: str) -> dict[str, int]:
    """{COM port: the USB interface it hangs off} from WINDOWS_PORT_PARENTS; ports whose parent is not a USB
    interface (a single-interface device, a PCI port) are left out."""
    try:
        data = json.loads(text) if text.strip() else []
    except json.JSONDecodeError:
        return {}
    out = {}
    for e in data if isinstance(data, list) else [data]:
        if isinstance(e, dict) and e.get("port") and (m := _MI.search(e.get("parent") or "")):
            out[e["port"]] = int(m.group(1), 16)
    return out


WINDOWS_HUB_DRIVERS = ("USBHUB3", "USBHUB")  # Windows' own USB hub drivers (USB 3 and USB 2)
_PNP_ID = re.compile(r"^USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})(?:&MI_([0-9A-F]+))?\\(.+)$", re.I)


def same_serial(a: str | None, b: str | None) -> bool:
    """The same serial number, as a device gives it or as Windows writes it into an instance id, which pyserial then
    reports for a port: upper-cased, spaces as '_' ("FactoryAIOT Pro" -> FACTORYAIOT_PRO; real VM, 2026-11-08)."""
    if not a or not b:
        return False
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
               and (serial and x.get("serial") and same_serial(x["serial"], serial) or not serial and len(same_ids) == 1)]
        desc = own[0] if len(own) == 1 else {}
        bus = e.get("bus") or next((ce.get("bus") for _, ce in kids if ce.get("bus")), None)
        # no working driver: Windows says so (a status other than OK), or there is none at all: an interface listed as OK,
        # no problem, no service (real VM, 2026-11-08, the Tang Nano's JTAG side with no FTDI driver for ARM64). A listing
        # without the service field (older recordings) says nothing either way
        bad = lambda x: (x.get("status") or "OK").upper() != "OK" or ("service" in x and not x["service"])  # noqa: E731
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


# What LiveIo's lists give (pyserial's ports; the USB lists of macOS, Linux and Windows): a fixture says nothing else,
# or a test passes on a clue no OS gives (audit 2026-11-07, T12: a "channel" field made a test right that macOS is not)
SERIAL_FIELDS = frozenset({"device", "vid", "pid", "serial_number", "location", "interface", "description",
                           "usb_interface"})  # the last on Windows, from its device tree (WINDOWS_PORT_PARENTS)
USB_FIELDS = frozenset({"name", "vendor", "vid", "pid", "serial", "location", "device_class", "usb_node", "usb_writable",
                        "drivers", "no_driver"})


def _as_listed(items: list[dict], fields: frozenset, what: str) -> list[dict]:
    for item in items:
        if set(item) - fields:
            raise ValueError(f"fixture {what} {item.get('device') or item.get('name')!r}: {sorted(set(item) - fields)} "
                             "is not something the OS gives")
    return [dict(i, vid=int(i["vid"], 16), pid=int(i["pid"], 16)) for i in items]


class _Seen(list):
    """What reached the fixture bench. With BENCHSCOUT_FIXTURE_LOG set, each also goes to that file as a JSON line
    ({"what", "where", "data"}), so a test outside the core sees what a real device would have: blackbox/."""

    def __init__(self, log: str | None):
        super().__init__()
        self.log, self.lock = log, threading.Lock()

    def append(self, call: tuple) -> None:
        super().append(call)
        if self.log:
            data = call[2] if len(call) > 2 else None
            line = {"what": call[0], "where": list(call[1]) if isinstance(call[1], tuple) else call[1],
                    "data": data.decode("utf-8", "backslashreplace") if isinstance(data, bytes) else data}
            with self.lock, open(self.log, "a", encoding="utf-8") as f:
                f.write(json.dumps(line) + "\n")


class FixtureIo:
    """Replays a bench described in JSON (see fixtures/)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text(encoding="utf-8"))
        self._mtime = self.path.stat().st_mtime_ns
        self.calls: list[tuple] = _Seen(os.environ.get("BENCHSCOUT_FIXTURE_LOG"))  # what access was attempted
        self.exclusive_opens: list[str] = []
        self.bauds: list[int] = []
        self.lines: list[str] = []
        self.gone_until: dict[str, float] = {}
        self.unplugged: set[str] = set()  # serial ports taken out of the file since it was first read
        self.streams_opened: list[str] = []

    # every way this fixture opens a serial port: to listen, to send (and read the answer), to hold it open (a watch)
    OPENS = ("serial_listen", "serial_send", "serial_stream")

    def opened(self, path: str | None = None) -> list[str]:
        """The serial ports opened so far (each time), or only `path`'s. Tests ask this for "nothing was opened": asking
        for one way of opening let the others through (audit 2026-11-07, T5)."""
        paths = [c[1] for c in self.calls if c[0] in self.OPENS]
        return paths if path is None else [p for p in paths if p == path]

    def sent(self, path: str | None = None) -> list[bytes]:
        """What was written to serial ports (or to `path`), through a watch that holds the port or by opening it."""
        return [c[2] for c in self.calls if c[0] in ("serial_send", "serial_write") and (path is None or c[1] == path)]

    def serial_stream(self, path: str, baud: int = DEFAULT_BAUD, exclusive: bool = False, lines: str = "low") -> _FixtureStream:
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
        return _as_listed(self.data.get("usb", []), USB_FIELDS, "USB device")

    def serial_ports(self) -> list[dict]:
        self._reread()
        return _as_listed(self.data.get("serial", []), SERIAL_FIELDS, "serial port")

    def visa_resources(self) -> list[str]:
        return list(self.data.get("visa", {}).keys())

    def tools(self) -> dict[str, str | None]:
        from . import knowledge as K

        have = self.data.get("tools", [])
        return {t: (f"/fixture/bin/{t}" if t in have else None) for t in K.tool_names()}

    def _visa(self, resource: str) -> dict:
        """A recorded instrument fails as LiveIo does, with OSError: one not on the bench, one recorded with "error"
        (busy, gone), a command it has no answer for (VISA times out). Returning None here let tests pass on a shape
        the real one never gives (audit 2026-11-07, T9)."""
        entry = self.data.get("visa", {}).get(resource)
        if entry is None:
            raise OSError(f"VISA {resource}: not on this bench (fixture)")
        if "error" in entry:
            raise OSError(f"VISA {resource}: {entry['error']}")
        return entry

    def visa_query_raw(self, resource: str, command: str) -> bytes:
        self.calls.append(("visa_query_raw", resource, command))
        self._visa(resource)
        v = self.data.get("visa_raw", {}).get(resource, {}).get(command)  # recorded as hex
        if v is None:
            raise OSError(f"VISA {resource}: no answer to {command!r} in time (fixture: none recorded)")
        return bytes.fromhex(v)

    def visa_query(self, resource: str, command: str) -> str:
        self.calls.append(("visa_query", resource, command))
        answer = self._visa(resource).get(command)
        if answer is None:
            raise OSError(f"VISA {resource}: no answer to {command!r} in time (fixture: none recorded)")
        return answer

    def visa_write(self, resource: str, command: str) -> None:
        self.calls.append(("visa_write", resource, command))
        self._visa(resource)

    def serial_listen(self, path: str, seconds: float, baud: int = DEFAULT_BAUD, exclusive: bool = False,
                      lines: str = "low", send: bytes | None = None, until: re.Pattern | None = None, cancel=None, may_reopen=None) -> str:
        v = self.data.get("listen", {}).get(path, "")
        self.calls.append(("serial_send", path, send) if send is not None else ("serial_listen", path))
        self.lines.append(lines)  # how it was opened, a send as a listen: both open the port
        if exclusive:
            self.exclusive_opens.append(path)
        self.bauds.append(baud)
        if send is not None:  # what a device answers is recorded per sent text in "replies"
            if isinstance(v, dict) and "error" in v:  # gone as soon as it was opened
                raise OSError(6, v["error"])
            return self.data.get("replies", {}).get(path, {}).get(send.decode(errors="replace"), "")
        if isinstance(v, dict) and "then" in v:  # vanishes, comes back, keeps printing
            if may_reopen is None or not may_reopen(0.1):
                raise PortGone("port vanished; reopening was not authorized")
            return f"\n{VANISHED_MARK} at 0.1 s; reopened at 0.9 s\n" + v["then"]
        if isinstance(v, dict) and "error" in v:  # simulate a port that vanishes for good
            raise OSError(6, v["error"])
        return v

    def run_probe(self, argv: list[str], timeout: float) -> str | None:
        """A probe tool's recorded output, found by any of its arguments (a probe's serial, say) in "probes"."""
        self.calls.append(("probe", tuple(argv)))
        recorded = self.data.get("probes", {})
        return next((recorded[a] for a in argv if a in recorded), "")
