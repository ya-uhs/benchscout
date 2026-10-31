"""Recon: build the bench graph, run only SAFE probes, and propose the rest."""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field

from . import knowledge as K
from .io import Io
from .model import Bench, Confidence, Device, Evidence, Port, Target
from .policy import Effect, Impact, classify


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

    for u in io.usb_devices():
        info = K.USB_IDS.get((u["vid"], u["pid"]))
        if not info:
            continue  # keyboards, hubs, audio... not bench hardware
        did = _dev_id(u["vid"], u["pid"], u.get("serial"), u.get("location"))
        raw_usb[did] = u
        b.add_device(
            Device(
                id=did,
                name=info["name"],
                transport="usb",
                vid=u["vid"],
                pid=u["pid"],
                serial=u.get("serial"),
                location=u.get("location"),
                evidence=[Evidence("usb-descriptor", f"{_hex(u['vid'])}:{_hex(u['pid'])} {u.get('vendor') or ''} {u.get('name') or ''}".strip())],
            )
        )

    # Serial ports hang off USB devices; on Linux the USB list is empty, so create devices here.
    by_dev: dict[str, list[dict]] = {}
    for s in io.serial_ports():
        did = _dev_id(s["vid"], s["pid"], s.get("serial_number"), None)
        if did not in b.devices:
            match = [d for d in b.devices.values() if (d.vid, d.pid) == (s["vid"], s["pid"]) and d.serial == s.get("serial_number")]
            if match:
                did = match[0].id
            else:
                info = K.USB_IDS.get((s["vid"], s["pid"]), {"name": s.get("description") or "USB serial device", "role": "unknown"})
                b.add_device(Device(did, info["name"], "usb", s["vid"], s["pid"], s.get("serial_number"),
                                    evidence=[Evidence("serial-port", f"{_hex(s['vid'])}:{_hex(s['pid'])} {s.get('description') or ''}".strip())]))
        by_dev.setdefault(did, []).append(s)

    for did, dev in list(b.devices.items()):
        info = K.USB_IDS.get((dev.vid, dev.pid), {"role": "unknown", "name": dev.name})
        serials = sorted(by_dev.get(did, []), key=lambda s: (s.get("channel", 99), s["device"]))
        _apply_usb_knowledge(b, dev, info, serials, raw_usb.get(did, {"serial": dev.serial}))

    for res in io.visa_resources():
        _add_visa(b, res)
    return b


def _serial_port(b: Bench, dev: Device, s: dict, kind: str = "serial", may_reset: bool = False) -> Port:
    pid = "serial:" + os.path.basename(s["device"]).removeprefix("cu.").removeprefix("tty.")
    return b.add_port(Port(pid, dev.id, kind, s["device"], s.get("channel"), may_reset,
                           [Evidence("serial-port", s["device"])]))


def _apply_usb_knowledge(b: Bench, dev: Device, info: dict, serials: list[dict], raw: dict) -> None:
    role = info.get("role")
    short = (dev.serial or dev.id.split(":")[-1]).replace(" ", "_")  # ids get passed as tool args

    if role == "native":
        t = b.add_target(Target(f"target:{short}", None, "mcu", Confidence.FAMILY,
                                facts=[info["family"]], evidence=[Evidence("usb-descriptor", f"{info['name']} enumerates from the chip itself")]))
        for s in serials:
            p = _serial_port(b, dev, s, may_reset=info.get("may_reset_on_open", False))
            b.link(p.id, t.id, "uart", Evidence("usb-descriptor", "CDC-ACM of the chip"))
        if info.get("jtag"):
            p = b.add_port(Port(f"jtag:{short}", dev.id, "jtag", evidence=[Evidence("usb-descriptor", "built-in USB-JTAG interface")]))
            b.link(p.id, t.id, "jtag")
        return

    if role == "multi":
        board = next((bd for bd in K.MULTI_CHANNEL_BOARDS if bd["match"](raw)), None)
        if board:
            ev = Evidence("board-knowledge", board["source"])
            if board.get("debugger"):
                dev.name = board["debugger"]
            if dev.serial in K.SHARED_SERIALS:
                dev.evidence.append(Evidence("usb-descriptor", f"serial '{dev.serial}' is shared by every unit of this model; two of these boards would be indistinguishable"))
            fpga = b.add_target(Target(f"target:{short}:fpga", None, "fpga", Confidence.UNKNOWN,
                                       facts=[f"on a {board['board']}"], evidence=[ev]))
            jp = b.add_port(Port(f"jtag:{short}", dev.id, "jtag", interface=0, evidence=[ev]))
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
                                facts=[f"behind {info['name']}"], evidence=[Evidence("usb-descriptor", info["name"])]))
        p = b.add_port(Port(f"{kind}:{short}", dev.id, kind, evidence=[Evidence("usb-descriptor", info["name"])]))
        b.link(p.id, t.id, kind)
        for s in serials:  # virtual COM port of the probe goes to the same target
            sp = _serial_port(b, dev, s)
            b.link(sp.id, t.id, "uart", Evidence("usb-descriptor", f"{info['name']} virtual COM port"))
        return

    if role in ("bridge", "unknown"):
        for s in serials:
            p = _serial_port(b, dev, s, may_reset=info.get("may_reset_on_open", True))
            t = b.add_target(Target(f"target:{p.id.split(':', 1)[1]}", None, "unknown", Confidence.UNKNOWN,
                                    facts=[f"behind {dev.name}"]))
            b.link(p.id, t.id, "uart")


_VISA_USB = re.compile(r"USB\d*::(0x[0-9A-Fa-f]+)::(0x[0-9A-Fa-f]+)::([^:]+)::")
_VISA_TCP = re.compile(r"TCPIP\d*::([^:]+)::")


def _add_visa(b: Bench, res: str) -> None:
    if res.upper().startswith("ASRL"):
        # VISA also lists serial ports. Sending *IDN? there would write bytes into an
        # unknown UART, so serial ports are only handled by the serial path.
        return
    if m := _VISA_USB.match(res):
        vid, pid, sn = int(m.group(1), 16), int(m.group(2), 16), m.group(3)
        did = _dev_id(vid, pid, sn, None)
        if did not in b.devices:
            b.add_device(Device(did, K.USB_IDS.get((vid, pid), {}).get("name", "USBTMC instrument"), "usb", vid, pid, sn))
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


def run(io: Io, listen_seconds: float = 2.0) -> Recon:
    b = build(io)
    r = Recon(b)

    for port in list(b.ports.values()):
        targets = b.targets_via(port.id)

        if port.kind in ("usbtmc", "lan-scpi"):
            if classify(b, "scpi.query", port.id, "*IDN?").effect == Effect.SAFE:
                r.ran.append(f"scpi.query *IDN? {port.path}")
                idn = io.visa_query(port.path, "*IDN?")
                for t in targets:
                    if idn:
                        info = K.parse_idn(idn)
                        t.name = f"{info['maker'].split()[0].title()} {info['model']}"
                        t.confidence = Confidence.IDENTIFIED
                        t.facts.append(info["kind"])
                        t.evidence.append(Evidence("scpi *IDN?", idn))
                    else:
                        t.facts.append("no answer to *IDN?")

        elif port.kind == "serial":
            if classify(b, "serial.listen", port.id).effect == Effect.SAFE:
                r.ran.append(f"serial.listen {port.path}")
                listen_into(io, port.path, targets, listen_seconds)

        elif port.kind == "jtag" and b.tools.get("openFPGALoader") and b.devices[port.device_id].vid == 0x0403:
            # openFPGALoader drives FTDI-compatible adapters; other JTAG (e.g. ESP32's
            # built-in USB-JTAG) needs a different tool, so we do not point it there.
            if classify(b, "jtag.scan", port.id).effect == Effect.SAFE:
                dev = b.devices[port.device_id]
                r.ran.append(f"jtag.scan {port.id}")
                for found in io.jtag_scan(dev.serial):
                    code = found["idcode"]
                    part = K.IDCODES.get(code & 0x0FFFFFFF)
                    # Prefer the tool's own IDCODE database; fall back to ours.
                    name = " ".join(x for x in (found.get("manufacturer"), found.get("model")) if x) or (part or {}).get("name")
                    src = "openFPGALoader database" if found.get("model") else ("benchscout table" if part else "not in any table")
                    for t in targets:
                        if name and t.kind in ("fpga", "mcu", "unknown"):
                            t.name, t.confidence = name, Confidence.IDENTIFIED
                            if t.kind == "unknown":
                                t.kind = (part or {}).get("kind", "fpga")
                        t.evidence.append(Evidence("jtag idcode", f"0x{code:08X} ({src})"))

    r.proposals = propose(b)
    return r


def listen_into(io: Io, path: str, targets: list[Target], seconds: float) -> str | None:
    """Listen and fold what was heard into the targets. A port that vanishes mid-read
    (USB re-enumeration, usually a reset) is recorded as a fact, not a crash."""
    try:
        text = io.serial_listen(path, seconds, 115200)
    except Exception as e:  # serial.SerialException / OSError: device went away
        for t in targets:
            t.facts.append(f"ALERT: port disappeared while listening ({e}); the device likely reset or re-enumerated")
        return None
    apply_log(targets, text)
    return text


def apply_log(t_list: list[Target], text: str) -> None:
    c = K.classify_log(text)
    for t in t_list:
        if not text.strip():
            t.facts.append("silent for the listen window")
            continue
        if c["exact"]:
            t.name, t.confidence = c["exact"], Confidence.IDENTIFIED
            t.kind = "mcu" if t.kind == "unknown" else t.kind
            t.evidence.append(Evidence("boot-log", f"ESP-ROM banner -> {c['exact']}"))
        elif c["family"] and t.confidence == Confidence.UNKNOWN:
            t.confidence = Confidence.FAMILY
            t.facts.append(c["family"])
        t.facts += c["facts"]
        t.facts += [f"ALERT: {a}" for a in c["alerts"]]
        t.facts += [f"log: {line}" for line in c["sample"]]


def _from_impact(text: str, imp: Impact, needs: str | None = None) -> Proposal:
    """A proposal that carries the broker's own reason, so the agent never has to guess it."""
    reason = imp.reason
    if imp.affected_ports:
        reason += f"; also disturbs {', '.join(imp.affected_ports)}"
    return Proposal(text, imp.action, imp.port_id, imp.effect.name.lower(), needs, reason)


def propose(b: Bench) -> list[Proposal]:
    out: list[Proposal] = []
    scopes = [t for t in b.targets.values() if "oscilloscope" in t.facts]

    for port in b.ports.values():
        for t in b.targets_via(port.id):
            if port.kind == "serial" and t.confidence != Confidence.IDENTIFIED and port.may_reset_on_open \
                    and not any(f.startswith("log:") for f in t.facts):
                imp = classify(b, "serial.listen", port.id)
                out.append(_from_impact(f"Listen on {port.path} to see what is behind {b.devices[port.device_id].name}", imp))
            if t.kind == "mcu" and t.confidence == Confidence.FAMILY and "ESP32" in " ".join(t.facts):
                has = b.tools.get("esptool.py") or b.tools.get("esptool")
                if port.kind == "serial":
                    imp = classify(b, "esptool.chip_id", port.id)
                    out.append(_from_impact("Read the exact chip with esptool chip_id", imp, None if has else "install esptool"))
            if port.kind == "jtag" and t.confidence == Confidence.UNKNOWN and not b.tools.get("openFPGALoader"):
                imp = classify(b, "jtag.scan", port.id)
                out.append(_from_impact(f"Scan the JTAG chain on {port.id} to identify the part", imp, "install openFPGALoader"))
            if any("brownout" in f for f in t.facts):
                if scopes:
                    scope_port = b.ports_reaching(scopes[0].id)[0]
                    imp = classify(b, "scpi.write", scope_port.id, ":SINGle")
                    out.append(_from_impact(
                        f"{t.name or t.id} is browning out. Capture the 3V3 rail on {scopes[0].name} during the reset",
                        imp, "which net is the scope probe on?"))
                else:
                    out.append(Proposal(f"{t.name or t.id} is browning out. Check the supply rail; no oscilloscope found on the bench",
                                        reason="nothing on the bench can capture the rail"))
    # one proposal per (text) is enough
    seen, uniq = set(), []
    for p in out:
        if p.text not in seen:
            seen.add(p.text)
            uniq.append(p)
    return uniq
