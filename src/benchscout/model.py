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
    kind: str  # "mcu" | "fpga" | "instrument" | "fabric-endpoint" | "unknown"
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
