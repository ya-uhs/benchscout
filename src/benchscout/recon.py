"""Recon: build the bench graph, run only SAFE probes, and propose the rest."""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from typing import Callable

from . import knowledge as K
from .io import VANISHED_MARK, Io
from .model import Bench, Confidence, Device, Evidence, Port, Target
from .policy import SCPI_PORTS, Effect, Impact, classify


@dataclass
class Proposal:
    text: str
    action: str | None = None
    port: str | None = None
    effect: str = "safe"
    needs: str | None = None  # missing tool or a question only the human can answer
    reason: str | None = None  # why it is classified as it is, straight from the policy


@dataclass
class Recon:
    bench: Bench
    proposals: list[Proposal] = field(default_factory=list)
    ran: list[str] = field(default_factory=list)  # audit of probes actually executed

    def to_dict(self) -> dict:
        return {"bench": self.bench.to_dict(), "proposals": [asdict(p) for p in self.proposals], "ran": self.ran}


def _hex(n: int | None) -> str:
    return f"{n:04x}" if n is not None else "????"


def _dev_id(vid, pid, serial, location) -> str:
    return f"usb:{_hex(vid)}:{_hex(pid)}:{serial or location or 'noserial'}"


def build(io: Io) -> Bench:
    """Discovery only: nothing is opened here."""
    b = Bench(tools={k: v for k, v in io.tools().items()})
    raw_usb: dict[str, dict] = {}
    ports = io.serial_ports()
    with_serial = {(s["vid"], s["pid"]) for s in ports}
    usb = io.usb_devices()  # once: on Windows it is a PowerShell query and a read through every hub

    for u in usb:
        # Every USB device is listed with its raw clues (design.md section 3: benchscout enumerates; the agent
        # identifies). One with a serial port, described in the data (a debug probe), or that the OS has no driver
        # for is bench hardware; any other (a keyboard, a camera, or a programmer benchscout has no data for) is
        # listed apart, so the agent still sees it. Hubs carry nothing: USB device class 09 where the OS says it (a
        # Linux root hub is "xHCI Host Controller"), else the name.
        if u.get("device_class") == 0x09 or "hub" in (u.get("name") or "").lower():
            continue
        other = (u["vid"], u["pid"]) not in K.USB_IDS and (u["vid"], u["pid"]) not in with_serial and not u.get("no_driver")
        did = _dev_id(u["vid"], u["pid"], u.get("serial"), u.get("location"))
        raw_usb[did] = u
        b.add_device(
            Device(
                id=did,
                name=_descriptor_name(u.get("vendor"), u.get("name")) or f"USB {_hex(u['vid'])}:{_hex(u['pid'])}",
                transport="usb",
                vid=u["vid"],
                pid=u["pid"],
                serial=u.get("serial"),
                location=u.get("location"),
                no_driver=list(u.get("no_driver") or []),
                other=other,
                drivers={int(k): v for k, v in (u.get("drivers") or {}).items()},
                usb_node=u.get("usb_node"),
                usb_writable=u.get("usb_writable"),
                evidence=[Evidence("usb-descriptor", f"{_hex(u['vid'])}:{_hex(u['pid'])} {u.get('vendor') or ''} {u.get('name') or ''}".strip())],
            )
        )

    # Serial ports hang off USB devices; if the USB list lacks one, create the device here.
    by_dev: dict[str, list[dict]] = {}
    usb_serials = {(d.vid, d.pid, d.serial) for d in b.devices.values()}
    for s in ports:
        s = _with_channel(s, usb_serials)
        did = _dev_id(s["vid"], s["pid"], s.get("serial_number"), None)
        if did not in b.devices:
            match = [d for d in b.devices.values() if (d.vid, d.pid) == (s["vid"], s["pid"]) and d.serial == s.get("serial_number")]
            if match:
                did = match[0].id
            else:
                name = s.get("description") or f"USB {_hex(s['vid'])}:{_hex(s['pid'])}"
                b.add_device(Device(did, name, "usb", s["vid"], s["pid"], s.get("serial_number"),
                                    evidence=[Evidence("serial-port", f"{_hex(s['vid'])}:{_hex(s['pid'])} {s.get('description') or ''}".strip())]))
        by_dev.setdefault(did, []).append(s)

    for did, dev in list(b.devices.items()):
        info = K.USB_IDS.get((dev.vid, dev.pid), {"role": "unknown"}) if dev.vid is not None and dev.pid is not None else {"role": "unknown"}
        serials = sorted(by_dev.get(did, []), key=lambda s: (s.get("channel", 99), s["device"]))
        _apply_usb_knowledge(b, dev, info, serials, raw_usb.get(did, {"serial": dev.serial}))

    _libusb_interfaces(b)
    names = {(u["vid"], u["pid"], u.get("serial")): _descriptor_name(u.get("vendor"), u.get("name")) for u in usb}
    for res in io.visa_resources():
        _add_visa(b, res, names)
    for dev in b.devices.values():  # one that got a port after all (a VISA instrument on USB) is bench hardware
        if dev.other and b.ports_of(dev.id):
            dev.other = False
    return b


WINUSB_CLASS = {"winusb", "libusbk", "libusb0"}  # drivers libusb can open on Windows


def _libusb_interfaces(b: Bench) -> None:
    """A port a libusb tool opens (tools.toml usb_access) that this user cannot reach: on Linux the device's USB node
    is not theirs to open (a udev rule is missing or not applied yet); on Windows the interface has another driver
    (the maker's serial driver, a COM port). Say what is needed."""
    for p in b.ports.values():
        dev = b.devices.get(p.device_id)
        if not dev or not p.tools:
            continue
        if dev.usb_writable is False and any(K.USB_ACCESS.get(t) == "libusb" for t in p.tools):
            dev.needs_udev = True  # Linux: the node is not this user's to open
            for t in b.targets_via(p.id):
                t.facts.append(f"Linux: this user cannot open {dev.usb_node or 'the USB device'}, which {', '.join(sorted(p.tools))} "
                               "opens through libusb. It needs a udev rule giving access (the tool's package often installs one, "
                               "e.g. 99-openfpgaloader.rules); a rule takes effect when the device is plugged in again, or after "
                               "`sudo udevadm control --reload-rules && sudo udevadm trigger`. Tell the user; that is theirs to do")
            continue
        if not dev.drivers:  # what follows is Windows: which driver each interface has
            continue
        iface = p.interface or 0
        driver = dev.drivers.get(iface)
        if driver and driver.lower() not in WINUSB_CLASS and any(K.USB_ACCESS.get(t) == "libusb" for t in p.tools):
            if iface not in dev.needs_winusb:
                dev.needs_winusb.append(iface)
            for t in b.targets_via(p.id):
                t.facts.append(f"Windows: interface {iface} has the {driver} driver; {', '.join(sorted(p.tools))} opens it "
                               "through libusb, which needs WinUSB there (Zadig installs it; that interface then stops "
                               "being a COM port). Tell the user; replacing a driver is theirs to do")


def _descriptor_name(vendor: str | None, product: str | None) -> str:
    vendor, product = (vendor or "").strip(), (product or "").strip()
    if product and vendor and product.lower().startswith(vendor.lower()):
        return product
    return " ".join(x for x in (vendor, product) if x)


_LINUX_IFACE = re.compile(r":\d+\.(\d+)$")  # pyserial location "1-1.2:1.1" -> interface 1


def _with_channel(s: dict, usb_serials: set) -> dict:
    """Which channel of a multi-channel USB-UART (FT2232H...) this port is, from what each OS
    tells us, instead of the order the ports happen to be listed in:

    - Windows: the FTDI driver appends the channel letter to the serial (210319A8C7D1B)
    - Linux:   the USB interface number is in the location (1-1:1.1)
    - macOS:   the port name ends with the channel digit (cu.usbserial-210319A8C7D11)
    """
    if s.get("channel") is not None:
        return s
    if K.USB_IDS.get((s["vid"], s["pid"]), {}).get("role") != "multi":
        return s
    sn = s.get("serial_number") or ""
    base = os.path.basename(s["device"])
    ch = None
    if (m := _LINUX_IFACE.search(s.get("location") or "")):
        ch = int(m.group(1))
    elif re.fullmatch(r".+[A-D]", sn) and ((s["vid"], s["pid"], sn[:-1]) in usb_serials or base.upper().startswith("COM")):
        ch, sn = "ABCD".index(sn[-1]), sn[:-1]
    else:
        tail = base.split("-", 1)[-1]
        if tail[-1:].isdigit() and len(tail) > 1 and sn.replace(" ", "").startswith(tail[:-1]):
            ch = int(tail[-1])
    return dict(s, channel=ch, serial_number=sn or None) if ch is not None else s


def _serial_port(b: Bench, dev: Device, s: dict, kind: str = "serial", may_reset: bool = False) -> Port:
    pid = "serial:" + os.path.basename(s["device"]).removeprefix("cu.").removeprefix("tty.")
    return b.add_port(Port(pid, dev.id, kind, s["device"], interface=s.get("channel"), may_reset_on_open=may_reset,
                           evidence=[Evidence("serial-port", s["device"])]))


def _tool_args(info: dict, dev: Device, port: Port) -> dict[str, list[str]]:
    """The usb entry's `tools`, filled in for this port. An argument whose value is unknown (no serial
    number) is dropped together with the option that introduces it."""
    out = {}
    for tool, template in info.get("tools", {}).items():
        args: list[str] = []
        for a in template:
            v = a.replace("{device.serial}", dev.serial or "").replace("{port.interface}", str(port.interface or 0))
            if not v:
                if args and args[-1].startswith("-"):
                    args.pop()
                continue
            args.append(v)
        out[tool] = args
    return out


def _apply_usb_knowledge(b: Bench, dev: Device, info: dict, serials: list[dict], raw: dict) -> None:
    role = info.get("role")
    short = (dev.serial or dev.id.split(":")[-1]).replace(" ", "_")  # ids get passed as tool args

    if role == "native":
        t = b.add_target(Target(f"target:{short}", None, "mcu", Confidence.UNKNOWN,
                                facts=["the chip enumerates on USB itself (no separate USB-UART bridge)"],
                                evidence=[Evidence("usb-descriptor", f"{_hex(dev.vid)}:{_hex(dev.pid)} {dev.name}")]))
        for s in serials:
            p = _serial_port(b, dev, s, may_reset=info.get("may_reset_on_open", False))
            p.open_lines = info.get("open_lines", "low")
            b.link(p.id, t.id, "uart", Evidence("usb-descriptor", "CDC-ACM of the chip"))
        if info.get("jtag"):
            p = b.add_port(Port(f"jtag:{short}", dev.id, "jtag", evidence=[Evidence("usb-descriptor", "built-in USB-JTAG interface")]))
            p.tools = _tool_args(info, dev, p)
            b.link(p.id, t.id, "jtag")
        return

    if role == "multi":
        board = next((bd for bd in K.MULTI_CHANNEL_BOARDS if bd["match"](raw)), None)
        if board:
            ev = Evidence("board-knowledge", board["source"])
            if dev.serial in K.SHARED_SERIALS:
                dev.evidence.append(Evidence("usb-descriptor", f"serial '{dev.serial}' is shared by every unit of this model; two of these boards would be indistinguishable"))
            fpga = b.add_target(Target(f"target:{short}:fpga", None, "fpga", Confidence.UNKNOWN,
                                       facts=[f"on a {board['board']}"], evidence=[ev]))
            jtag_ch = next((ch for ch, what in board["channels"].items() if what == "jtag-fpga"), 0)
            jp = b.add_port(Port(f"jtag:{short}", dev.id, "jtag", interface=jtag_ch, evidence=[ev]))
            jp.tools = _tool_args(info, dev, jp)
            b.link(jp.id, fpga.id, "jtag", ev)
            for i, s in enumerate(serials):
                ch = s.get("channel", i)
                if board["channels"].get(ch) == "uart-fpga-fabric":
                    fab = b.add_target(Target(f"target:{short}:fabric-uart", None, "fabric-endpoint", Confidence.UNKNOWN,
                                              hosted_on=fpga.id, facts=["UART endpoint implemented in FPGA fabric"], evidence=[ev]))
                    p = _serial_port(b, dev, s)
                    b.link(p.id, fab.id, "uart", ev)
            return
        # Unknown board: channels are just unknown serial ports.
        role = "bridge"

    if role == "probe":
        kind = info.get("probe", "jtag")
        t = b.add_target(Target(f"target:{short}", None, "unknown", Confidence.UNKNOWN,
                                facts=[f"behind {dev.name}"], evidence=[Evidence("usb-descriptor", dev.name)]))
        p = b.add_port(Port(f"{kind}:{short}", dev.id, kind, evidence=[Evidence("usb-descriptor", dev.name)]))
        p.tools = _tool_args(info, dev, p)
        b.link(p.id, t.id, kind)
        for s in serials:  # virtual COM port of the probe goes to the same target
            sp = _serial_port(b, dev, s)
            b.link(sp.id, t.id, "uart", Evidence("usb-descriptor", f"{dev.name} virtual COM port"))
        return

    if role in ("bridge", "unknown"):
        for s in serials:
            p = _serial_port(b, dev, s, may_reset=info.get("may_reset_on_open", True))
            t = b.add_target(Target(f"target:{p.id.split(':', 1)[1]}", None, "unknown", Confidence.UNKNOWN,
                                    facts=[f"behind {dev.name}"]))
            b.link(p.id, t.id, "uart")


_VISA_USB = re.compile(r"USB\d*::(0x[0-9A-Fa-f]+)::(0x[0-9A-Fa-f]+)::([^:]+)::")
_VISA_TCP = re.compile(r"TCPIP\d*::([^:]+)::")


def _add_visa(b: Bench, res: str, names: dict | None = None) -> None:
    if res.upper().startswith("ASRL"):
        # VISA also lists serial ports. Sending *IDN? there would write bytes into an
        # unknown UART, so serial ports are only handled by the serial path.
        return
    if m := _VISA_USB.match(res):
        vid, pid, sn = int(m.group(1), 16), int(m.group(2), 16), m.group(3)
        did = _dev_id(vid, pid, sn, None)
        if did not in b.devices:
            b.add_device(Device(did, (names or {}).get((vid, pid, sn)) or f"USBTMC instrument {_hex(vid)}:{_hex(pid)}", "usb", vid, pid, sn))
        kind = "usbtmc"
    elif m := _VISA_TCP.match(res):
        did = f"lan:{m.group(1)}"
        b.add_device(Device(did, m.group(1), "lan", evidence=[Evidence("visa", res)]))
        kind = "lan-scpi"
    else:
        did = f"visa:{res}"
        b.add_device(Device(did, res, "usb"))
        kind = "usbtmc"
    p = b.add_port(Port(f"visa:{res}", did, kind, path=res, evidence=[Evidence("visa", res)]))
    t = b.add_target(Target(f"target:{res}", None, "instrument", Confidence.UNKNOWN))
    b.link(p.id, t.id, "scpi")


def run(io: Io, listen_seconds: float = 2.0, busy: Callable[[str], dict | None] | None = None) -> Recon:
    """`busy(device_id)` names another benchscout window holding that device; its ports are
    left alone (docs/notes.md review S1/S2)."""
    b = build(io)
    r = Recon(b)

    for port in list(b.ports.values()):
        targets = b.targets_via(port.id)
        holder = busy(port.device_id) if busy else None
        if holder:
            where = holder.get("workspace") or f"pid {holder.get('pid')}"
            for t in targets:
                fact = f"in use by another benchscout window ({where}); not probed"
                if fact not in t.facts:
                    t.facts.append(fact)
            continue

        if port.kind in SCPI_PORTS and port.path:
            if classify(b, "scpi.query", port.id, {"command": "*IDN?"}).effect == Effect.SAFE:
                r.ran.append(f"scpi.query *IDN? {port.path}")
                idn = io.visa_query(port.path, "*IDN?")
                for t in targets:
                    if idn:
                        info = K.parse_idn(idn)
                        t.name = f"{info['maker'].split()[0].title()} {info['model']}"
                        t.confidence = Confidence.IDENTIFIED
                        t.evidence.append(Evidence("scpi *IDN?", idn))
                    else:
                        t.facts.append("no answer to *IDN?")

        elif port.kind == "serial" and port.path:
            if classify(b, "serial.listen", port.id).effect == Effect.SAFE:
                r.ran.append(f"serial.listen {port.path}")
                listen_into(io, port.path, targets, listen_seconds)

        elif port.kind == "jtag" and b.tools.get("openFPGALoader") and port.tools.get("openFPGALoader") \
                and not b.devices[port.device_id].needs_udev and not b.devices[port.device_id].needs_winusb:
            # only where the USB data says how openFPGALoader reaches this probe; others stay unknown
            if classify(b, "jtag.scan", port.id).effect == Effect.SAFE:
                r.ran.append(f"jtag.scan {port.id}")
                for found in io.jtag_scan(port.tools["openFPGALoader"]):
                    code = found["idcode"]
                    # the tool's own IDCODE database names it; benchscout keeps no table (design.md section 5)
                    name = " ".join(x for x in (found.get("manufacturer"), found.get("model")) if x)
                    src = "openFPGALoader database" if found.get("model") else "not in openFPGALoader's database"
                    for t in targets:
                        if name and t.kind in ("fpga", "mcu", "unknown"):
                            t.name, t.confidence = name, Confidence.IDENTIFIED
                            if t.kind == "unknown":
                                t.kind = "fpga"
                        t.evidence.append(Evidence("jtag idcode", f"0x{code:08X} ({src})"))

    r.proposals = propose(b)
    return r


def listen_into(io: Io, path: str, targets: list[Target], seconds: float, exclusive: bool = False,
                baud: int = 115200, lines: str = "low") -> str | None:
    """Listen and fold what was heard into the targets. A port that vanishes mid-read
    (USB re-enumeration, usually a reset) is recorded as a fact, not a crash."""
    try:
        text = io.serial_listen(path, seconds, baud, exclusive, lines)
    except Exception as e:  # serial.SerialException / OSError: device went away
        for t in targets:
            t.facts.append(f"ALERT: port disappeared while listening ({e}); the device likely reset or re-enumerated")
        return None
    if VANISHED_MARK in text:
        for t in targets:
            fact = "the port vanished while listening (the device reset or re-enumerated) and came back; kept listening"
            if fact not in t.facts:
                t.facts.append(fact)
    apply_log(targets, "\n".join(l for l in text.splitlines() if VANISHED_MARK not in l))
    return text


def apply_log(t_list: list[Target], text: str, seen: str | None = None) -> None:
    """Fold console output into the targets. `seen` marks output heard earlier (before this
    scan): it still proves what the part is, but its lines are labelled with when."""
    c = K.classify_log(text)
    when = f" (seen {seen})" if seen else ""
    for t in t_list:
        if not text.strip():
            if not seen:
                t.facts.append("silent for the listen window")
            continue
        t.facts += [f"ALERT: {a}{when}" for a in c["alerts"]]
        t.facts += [f"{'earlier log' if seen else 'log'}: {line}" for line in c["sample"]]


def _from_impact(text: str, imp: Impact, needs: str | None = None) -> Proposal:
    """A proposal that carries the broker's own reason, so the agent never has to guess it."""
    reason = imp.reason
    if imp.affected_ports:
        reason += f"; also disturbs {', '.join(imp.affected_ports)}"
    return Proposal(text, imp.action, imp.port_id, imp.effect.name.lower(), needs, reason)


def propose(b: Bench) -> list[Proposal]:
    """Only the generic next steps a cautious bench needs before any LLM is involved. What to do
    about a particular device (read its chip id, capture a rail) is the agent's call (design.md 3)."""
    out: list[Proposal] = []
    for port in b.ports.values():
        for t in b.targets_via(port.id):
            if port.kind == "serial" and t.confidence != Confidence.IDENTIFIED and port.may_reset_on_open \
                    and not any(f.startswith("log:") for f in t.facts):
                imp = classify(b, "serial.listen", port.id)
                out.append(_from_impact(f"Listen on {port.path} to see what is behind {b.devices[port.device_id].name}", imp))
            if port.kind == "jtag" and t.confidence == Confidence.UNKNOWN and not b.tools.get("openFPGALoader") \
                    and port.tools.get("openFPGALoader"):
                imp = classify(b, "jtag.scan", port.id)
                out.append(_from_impact(f"Scan the JTAG chain on {port.id} to identify the part", imp, "install openFPGALoader"))
    # one proposal per (text) is enough
    seen, uniq = set(), []
    for p in out:
        if p.text not in seen:
            seen.add(p.text)
            uniq.append(p)
    return uniq
