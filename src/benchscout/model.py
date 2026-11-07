"""Bench graph: physical devices expose ports, ports reach targets over protocols.

Protocols (UART, JTAG, SCPI...) are labels on links, not layers. A single USB
device can expose several ports (FT2232H: JTAG + UART), and a single target can
be reachable through several ports (ESP32-S3: CDC serial + USB-JTAG).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class Confidence(str, Enum):
    IDENTIFIED = "identified"  # exact part, backed by evidence
    FAMILY = "family"  # family known, exact part not yet
    UNKNOWN = "unknown"  # only raw facts; we do not guess


@dataclass
class Evidence:
    source: str  # e.g. "usb-descriptor", "boot-log", "scpi *IDN?", "jtag idcode"
    detail: str


@dataclass
class Device:
    """Unit of plug/unplug: a USB device or a LAN host."""

    id: str
    name: str
    transport: str  # "usb" | "lan"
    vid: int | None = None
    pid: int | None = None
    serial: str | None = None
    location: str | None = None
    evidence: list[Evidence] = field(default_factory=list)
    # what the OS says about its interfaces: each one's driver (Windows), its USB node and whether this user may open
    # it (Linux). Raw facts; what follows from them is in `access`
    drivers: dict[int, str] = field(default_factory=dict)
    usb_node: str | None = None
    usb_writable: bool | None = None
    # why something cannot open it yet, one form for every OS: {"need": "driver" (no working driver: nothing can open
    # those interfaces) | "winusb" (a libusb tool needs WinUSB there) | "udev" (this user cannot open the USB node a
    # libusb tool needs), "interfaces": [...], "node": ..., "tools": [...]}
    access: list[dict] = field(default_factory=list)
    # a USB device benchscout knows nothing about (no serial port, no data, not a hub): shown to the agent with its raw
    # clues, apart from the bench, so a programmer or probe it does not know can still be named and given a tool
    other: bool = False
    # lines learned to mean trouble on this device (memory), checked with the shipped line rules everywhere
    line_rules: list[dict] = field(default_factory=list)


def hex4(n: int | None) -> str:
    return f"{n:04x}" if n is not None else "????"


def device_id(vid, pid, serial, location) -> str:
    """How a USB device is known on the bench: ids and serial (or where it is plugged in, without a serial)."""
    return f"usb:{hex4(vid)}:{hex4(pid)}:{serial or location or 'noserial'}"


DEFAULT_BAUD = 115200  # the rate a serial port is opened at when none was learned for it (memory config {"baud": n})
PORT_KINDS = ("serial", "jtag", "swd", "usbtmc", "lan-scpi", "resource")  # adapter kinds, not a list of supported devices
PROBE_KINDS = ("jtag", "swd")  # what a debug probe offers


@dataclass
class Port:
    """Unit of opening/leasing: /dev/cu.*, an FTDI channel, a VISA resource."""

    id: str
    device_id: str
    kind: str  # "serial" | "jtag" | "swd" | "usbtmc" | "lan-scpi"
    path: str | None = None
    interface: int | None = None
    # Opening this serial port may pulse DTR/RTS into an auto-reset circuit.
    may_reset_on_open: bool = False
    # How to set DTR/RTS when opening: "low" (drop both; avoids Arduino-style DTR resets) or
    # "asserted" (leave them as the OS opened them; for chips that reset on the transition).
    open_lines: str = "low"
    baud: int | None = None  # the rate learned for it (memory, config {"baud": n}); None: the common default
    # the chip itself is this USB device (no bridge in between): if it resets, the port drops off the bus, so a port that
    # stays put after opening shows the chip did not reset. Behind a USB-UART bridge only the MCU resets and the port stays.
    own_usb: bool = False
    evidence: list[Evidence] = field(default_factory=list)
    # firmware commands remembered for this port (design.md section 4): name -> {effect, eol, text, pending}
    commands: dict[str, dict] = field(default_factory=dict)
    # how an external tool is pointed at this port, from the USB data (`tools` of its usb entry),
    # e.g. {"openFPGALoader": ["-c", "ft2232", "--ftdi-serial", "<serial>", "--ftdi-channel", "0"]}
    tools: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class Target:
    """Unit of interest and of impact: a chip, an FPGA, an instrument."""

    id: str
    name: str | None  # None means unknown; never filled in with a guess
    kind: str  # a plain label ("mcu" for a chip on its own USB, "instrument" behind SCPI, else "unknown"); decides nothing
    confidence: Confidence = Confidence.UNKNOWN
    hosted_on: str | None = None  # e.g. a soft CPU console living inside an FPGA
    facts: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class Link:
    port_id: str
    target_id: str
    protocol: str  # "uart" | "jtag" | "swd" | "scpi"
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class Bench:
    devices: dict[str, Device] = field(default_factory=dict)
    ports: dict[str, Port] = field(default_factory=dict)
    targets: dict[str, Target] = field(default_factory=dict)
    links: list[Link] = field(default_factory=list)
    tools: dict[str, str | None] = field(default_factory=dict)

    def add_device(self, d: Device) -> Device:
        return self.devices.setdefault(d.id, d)

    def add_port(self, p: Port) -> Port:
        return self.ports.setdefault(p.id, p)

    def add_target(self, t: Target) -> Target:
        return self.targets.setdefault(t.id, t)

    def link(self, port_id: str, target_id: str, protocol: str, *evidence: Evidence) -> None:
        self.links.append(Link(port_id, target_id, protocol, list(evidence)))

    def ports_of(self, device_id: str) -> list[Port]:
        return [p for p in self.ports.values() if p.device_id == device_id]

    def targets_via(self, port_id: str) -> list[Target]:
        return [self.targets[l.target_id] for l in self.links if l.port_id == port_id]

    def links_of(self, port_id: str) -> list[Link]:
        return [l for l in self.links if l.port_id == port_id]

    def ports_reaching(self, target_id: str) -> list[Port]:
        return [self.ports[l.port_id] for l in self.links if l.target_id == target_id]

    # ---- telling units apart (design.md section 3: units that cannot be told apart are never matched by guess) ----
    def selector_serial(self, dev: Device) -> str | None:
        """The serial number that singles this unit out among those plugged in now, for a tool told which unit to use by
        its serial. None when it has none, or another unit of the same product carries the same one or one the OS did not
        give (it may be the same): a tool given that value could reach the other unit."""
        if not dev.serial:
            return None
        for d in self.devices.values():
            if d.id == dev.id:
                continue
            same_product = d.vid is not None and (d.vid, d.pid) == (dev.vid, dev.pid)
            if (d.vid, d.pid, d.serial) == (dev.vid, dev.pid, dev.serial) or same_product and not d.serial:
                return None
        return dev.serial

    def one_unit(self, dev: Device, shared_serials: set[str]) -> bool:
        """Memory is kept by product and serial number. It is about this very unit only if the serial singles it out now
        and the product does not give every unit the same one (`shared_serials`): memory that loosens safety, and a name
        remembered for it, apply only then."""
        return bool(dev.serial) and dev.serial not in shared_serials and self.selector_serial(dev) is not None

    def dependents(self, target_id: str) -> list[Target]:
        """Targets that go down when this one is reset/reprogrammed (transitively)."""
        out: list[Target] = []
        stack = [target_id]
        while stack:
            tid = stack.pop()
            for t in self.targets.values():
                if t.hosted_on == tid and t not in out:
                    out.append(t)
                    stack.append(t.id)
        return out

    def to_dict(self) -> dict:
        return {
            "devices": [asdict(d) for d in self.devices.values()],
            "ports": [asdict(p) for p in self.ports.values()],
            "targets": [asdict(t) for t in self.targets.values()],
            "links": [asdict(l) for l in self.links],
            "tools": self.tools,
        }
