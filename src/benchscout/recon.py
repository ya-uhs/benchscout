"""Recon: build the bench graph, run only SAFE probes, and propose the rest."""

from __future__ import annotations

import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from . import knowledge as K
from .io import VANISHED_MARK, Io
from .model import Bench, Confidence, Device, Evidence, Port, Target, device_id, hex4
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
    # what each port's SAFE probes got, so a later scan that leaves the device alone (it is in use, or only new devices
    # are probed) still shows it: port id -> [{"probe": "output" | "identity" | "chain", "time": ..., ...}]
    heard: dict[str, list[dict]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"bench": self.bench.to_dict(), "proposals": [asdict(p) for p in self.proposals], "ran": self.ran}


_hex, _dev_id = hex4, device_id


def place(location) -> str | None:
    """Where a device is plugged in, written one way for both lists: pyserial's "1-1.2" (Linux "3-3:1.0", its interface
    after the colon) and macOS's USB locationID 0x01120000 (the same digits, one per hub level). None if unknown."""
    if not location:
        return None
    loc = str(location).split()[0]
    if loc.lower().startswith("0x"):  # macOS: what pyserial's list_ports_osx does with the same number
        n = int(loc, 16)
        out = [f"{n >> 24}-"]
        while n & 0xF00000:
            if len(out) > 1:
                out.append(".")
            out.append(str((n >> 20) & 0xF))
            n <<= 4
        return "".join(out)
    return loc.split(":")[0]


def build(io: Io) -> Bench:
    """Discovery only: nothing is opened here."""
    b = Bench(tools={k: v for k, v in io.tools().items()})
    raw_usb: dict[str, dict] = {}
    ports = io.serial_ports()
    with_serial = {(s["vid"], s["pid"]) for s in ports}
    usb = io.usb_devices()  # once: on Windows it is a PowerShell query and a read through every hub
    # a serial number names one unit only if no other plugged-in unit carries it (none, or one shared by a model)
    from collections import Counter

    seen = Counter((u["vid"], u["pid"], u.get("serial")) for u in usb)

    for u in usb:
        # Every USB device is listed with its raw clues (design.md section 3: benchscout enumerates; the agent
        # identifies). One with a serial port, described in the data (a debug probe), or that the OS has no driver
        # for is bench hardware; any other (a keyboard, a camera, or a programmer benchscout has no data for) is
        # listed apart, so the agent still sees it. Hubs carry nothing: USB device class 09 where the OS says it (a
        # Linux root hub is "xHCI Host Controller"), else the name.
        if u.get("device_class") == 0x09 or "hub" in (u.get("name") or "").lower():
            continue
        other = (u["vid"], u["pid"]) not in K.USB_IDS and (u["vid"], u["pid"]) not in with_serial and not u.get("no_driver")
        unique = seen[(u["vid"], u["pid"], u.get("serial"))] == 1
        did = _dev_id(u["vid"], u["pid"], u.get("serial") if unique else None, u.get("location"))
        if not unique and u.get("serial"):
            did = f"{did}@{u['serial']}"  # two units with one serial: told apart by where they are plugged in
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
                access=[{"need": "driver", "interfaces": list(u["no_driver"])}] if u.get("no_driver") else [],
                other=other,
                drivers={int(k): v for k, v in (u.get("drivers") or {}).items()},
                usb_node=u.get("usb_node"),
                usb_writable=u.get("usb_writable"),
                evidence=[Evidence("usb-descriptor", f"{_hex(u['vid'])}:{_hex(u['pid'])} {u.get('vendor') or ''} {u.get('name') or ''}".strip())],
            )
        )

    # Serial ports hang off USB devices. A port goes to the one USB device with its ids and serial number; among
    # several such units, to the one plugged in where the port is; never by guess (design.md section 3: a port of one
    # board must not be usable by choosing another). A port that cannot be placed is a device of its own.
    by_dev: dict[str, list[dict]] = {}
    unplaced: set[str] = set()
    usb_serials = {(d.vid, d.pid, d.serial) for d in b.devices.values()}
    for s in ports:
        s = _with_channel(s, usb_serials)
        same = [d for d in b.devices.values() if (d.vid, d.pid) == (s["vid"], s["pid"]) and d.serial == s.get("serial_number")]
        if len(same) > 1:
            here = [d for d in same if place(d.location) and place(d.location) == place(s.get("location"))]
            unplaced |= {d.id for d in same}
            same = here
        if len(same) == 1:
            did = same[0].id
        else:
            ambiguous = seen[(s["vid"], s["pid"], s.get("serial_number"))] > 1
            did = _dev_id(s["vid"], s["pid"], None if ambiguous else s.get("serial_number"), None)
            if ambiguous or did in b.devices:
                did = f"usb:{_hex(s['vid'])}:{_hex(s['pid'])}:port:{s['device']}"
            if did not in b.devices:
                name = next((d.name for d in b.devices.values() if (d.vid, d.pid) == (s["vid"], s["pid"])), None) \
                    or s.get("description") or f"USB {_hex(s['vid'])}:{_hex(s['pid'])}"
                b.add_device(Device(did, name, "usb", s["vid"], s["pid"], s.get("serial_number"),
                                    evidence=[Evidence("serial-port", f"{_hex(s['vid'])}:{_hex(s['pid'])} {s.get('description') or ''}".strip())]
                                    + ([Evidence("serial-port", "several identical USB devices are plugged in and this port could not "
                                                                "be told to belong to one of them: listed as a device of its own")]
                                       if ambiguous else [])))
        by_dev.setdefault(did, []).append(s)
    for d in unplaced - set(by_dev):  # identical units whose ports went elsewhere: their ports stand for them
        if not any(s for s in by_dev.get(d, [])):
            del b.devices[d]

    for did, dev in list(b.devices.items()):
        info = K.USB_IDS.get((dev.vid, dev.pid), {"role": "unknown"}) if dev.vid is not None and dev.pid is not None else {"role": "unknown"}
        serials = sorted(by_dev.get(did, []), key=lambda s: (s.get("channel", 99), s["device"]))
        _apply_usb_knowledge(b, dev, info, serials, raw_usb.get(did, {"serial": dev.serial}))

    _libusb_interfaces(b)
    names = {(u["vid"], u["pid"], u.get("serial")): _descriptor_name(u.get("vendor"), u.get("name")) for u in usb}
    from . import protocols

    for proto in protocols.PROTOCOLS.values():  # devices reached only through a protocol (an instrument on VISA)
        proto.enumerate(io, b, names)
    for dev in b.devices.values():  # one that got a port after all (a VISA instrument on USB) is bench hardware
        if dev.other and b.ports_of(dev.id):
            dev.other = False
    return b


WINUSB_CLASS = {"winusb", "libusbk", "libusb0"}  # drivers libusb can open (the OS's own names for them)


def _needs(dev: Device, need: str, tools: list[str], interface: int | None = None, node: str | None = None) -> None:
    """Record why a tool cannot open this device yet (one record per need, interfaces and tools gathered)."""
    item = next((a for a in dev.access if a["need"] == need), None)
    if item is None:
        item = {"need": need, "interfaces": [], "tools": []}
        dev.access.append(item)
    if interface is not None and interface not in item["interfaces"]:
        item["interfaces"].append(interface)
    item["tools"] = sorted(set(item["tools"]) | set(tools))
    if node:
        item["node"] = node


def _libusb_interfaces(b: Bench) -> None:
    """A port a libusb tool opens (tools.toml usb_access) that this user cannot reach yet: on Linux the device's USB
    node is not theirs to open (a udev rule is missing or not applied yet); on Windows the interface has another
    driver (the maker's serial driver, a COM port). Recorded as what is needed (`access`); report.access_text says it."""
    for p in b.ports.values():
        dev = b.devices.get(p.device_id)
        if not dev or not p.tools or not any(K.USB_ACCESS.get(t) == "libusb" for t in p.tools):
            continue
        if dev.usb_writable is False:
            _needs(dev, "udev", tools=sorted(p.tools), node=dev.usb_node)
            continue
        driver = dev.drivers.get(p.interface or 0)
        if driver and driver.lower() not in WINUSB_CLASS:
            _needs(dev, "winusb", tools=sorted(p.tools), interface=p.interface or 0)
            item = next(a for a in dev.access if a["need"] == "winusb")
            item["drivers"] = sorted(set(item.get("drivers", [])) | {driver})


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
            p.own_usb = True
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
            # the wiring as data says: what each channel is wired as, and which channel's far end is inside which
            # (the same relation as wiring the agent learns, `inside`); nothing here knows what kind of part it is
            reached: dict[int, Target] = {}

            def target(ch: int, fact: str) -> Target:
                return b.add_target(Target(f"target:{short}:{board['ids'].get(ch, f'ch{ch}')}", None, "unknown",
                                           Confidence.UNKNOWN, facts=[fact], evidence=[ev]))

            jtags = [ch for ch, role in sorted(board["channels"].items()) if role == "jtag"]
            for ch in jtags:
                reached[ch] = target(ch, "the part on this board's JTAG, as the shipped wiring assumes (for safety; it names nothing)")
                jp = b.add_port(Port(f"jtag:{short}" + (f":{ch}" if len(jtags) > 1 else ""), dev.id, "jtag", interface=ch,
                                     evidence=[ev]))
                jp.tools = _tool_args(info, dev, jp)
                b.link(jp.id, reached[ch].id, "jtag", ev)
            for i, s in enumerate(serials):
                ch = s.get("channel", i)
                if board["channels"].get(ch) == "uart":
                    reached[ch] = target(ch, "a UART, as the shipped wiring assumes (for safety; it names nothing)")
                    p = _serial_port(b, dev, s)
                    b.link(p.id, reached[ch].id, "uart", ev)
            for ch, host in board["inside"].items():
                if ch in reached and host in reached:
                    reached[ch].hosted_on = reached[host].id
                    reached[ch].facts.append(f"inside the part on channel {host}, as the shipped wiring assumes")
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


def run(io: Io, listen_seconds: float = 2.0, busy: Callable[[str], dict | None] | None = None,
        keep: set[str] | frozenset[str] = frozenset(), heard: dict[str, list[dict]] | None = None,
        prepare: Callable[[Bench], None] | None = None) -> Recon:
    """`busy(device_id)` names another benchscout window holding that device; its ports are
    left alone (docs/notes.md review S1/S2). Devices in `keep` are not probed either (the session is using them,
    or only newly plugged devices are being looked at): what their probes got before (`heard`) is shown again."""
    b = build(io)
    if prepare:  # memory first: a port learned to reset when opened is not opened by a probe (design.md section 3)
        prepare(b)
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
        if port.device_id in keep:
            for got in (heard or {}).get(port.id, []):
                r.heard.setdefault(port.id, []).append(got)
                _fold(b, targets, got, earlier=True, port=port)
            continue

        for got in _probe(io, b, port, listen_seconds, r.ran):
            got["time"] = time.strftime("%Y-%m-%d %H:%M")
            r.heard.setdefault(port.id, []).append(got)
            _fold(b, targets, got, port=port)

    r.proposals = propose(b)
    return r


def _probe(io: Io, b: Bench, port: Port, seconds: float, ran: list[str]) -> list[dict]:
    """The SAFE probes for this port: its protocol's (what the device prints, or says it is), and every tool defined as
    a probe that reaches it (tools.toml `probe`; the tool's own database names the parts). Nothing here names one."""
    from . import actions as A
    from . import protocols

    out = []
    proto = protocols.for_port(port)
    if proto and proto.probe_action and port.path:
        action, args = proto.probe_action
        if classify(b, action, port.id, args).effect == Effect.SAFE:
            ran.append(" ".join([action, *[str(v) for v in args.values()], port.path]))
            got = proto.probe(io, port, seconds)
            if got:
                out.append(got)
    dev = b.devices[port.device_id]
    for spec in A.all_actions().values():
        if not spec.probe or spec.port_kinds and port.kind not in spec.port_kinds \
                or any(a["need"] == "driver" or spec.argv[0] in a.get("tools", []) for a in dev.access):
            continue
        exe = b.tools.get(spec.argv[0])
        argv = _probe_argv(spec, port, dev, exe)
        if argv is None or classify(b, spec.name, port.id).effect != Effect.SAFE:
            continue
        ran.append(f"{spec.name} {port.id}")
        text = io.run_probe(argv, spec.timeout)
        out.append({"probe": "chain", "result": read_parts(spec.probe, text or ""),
                    "source": spec.probe.get("source", spec.argv[0])})
    return out


def _probe_argv(spec, port: Port, dev, exe: str | None) -> list[str] | None:
    """The probe's command line for this port, or None when it cannot run here (not installed, no known way to reach
    the port, or it needs arguments a probe cannot have)."""
    if not exe:
        return None
    argv = [exe]
    for a in spec.argv[1:]:
        if a == "{port.select*}":
            sel = port.tools.get(spec.argv[0])
            if not sel:
                return None  # only where the USB data says how this tool reaches this port
            argv += sel
            continue
        val = a.replace("{port.path}", port.path or "").replace("{device.serial}", dev.serial or "")
        if "{" in val:
            return None
        argv.append(val)
    return argv


def read_parts(probe: dict, text: str) -> list[dict]:
    """The parts a probe tool listed, read as its definition says (`each` block: `idcode`, `maker`, `model`)."""
    each = probe.get("each")
    blocks = re.split(each, text, flags=re.M)[1:] if each else [text]
    parts = []
    for block in blocks:
        def grab(key: str, blk: str = block) -> str | None:
            m = re.search(probe[key], blk, re.M) if probe.get(key) else None
            return m.group(1).strip() if m else None

        code = grab("idcode")
        if code:
            parts.append({"idcode": int(code, 0), "maker": grab("maker"), "model": grab("model")})
    return parts


def _fold(b: Bench, targets: list[Target], got: dict, earlier: bool = False, port: Port | None = None) -> None:
    """What a probe got, into the targets it reaches. `earlier`: from a scan before this one (the device was left
    alone this time); a log is then labelled with when it was heard, as output kept from before is."""
    result = got.get("result")
    if got["probe"] == "identity":
        source = got.get("source", "identity")
        for t in targets:
            if got.get("maker") or got.get("model"):
                t.name = " ".join(x for x in (((got.get("maker") or "").split() or [""])[0].title(), got.get("model") or "") if x)
                t.confidence = Confidence.IDENTIFIED
                t.evidence.append(Evidence(source, str(result)))
            elif result:
                t.evidence.append(Evidence(source, str(result)))  # an answer that names nothing
            else:
                t.facts.append(f"no answer to {source}")
        if got.get("serial") and port is not None:
            dev = b.devices[port.device_id]
            if dev.transport == "lan" and not dev.serial:
                # a LAN instrument has no USB serial: the unit is what it says it is (maker, model, serial), not its address
                dev.serial = " ".join(x for x in (got.get("maker"), got.get("model"), got.get("serial")) if x)
    elif got["probe"] == "output":
        _fold_listen(targets, result, got.get("error"), got["time"] if earlier else None,
                     learned=b.devices[port.device_id].line_rules if port is not None else None)
    elif got["probe"] == "chain":
        chain = list(result or [])
        for i, found in enumerate(chain):
            code = found["idcode"]
            # the tool's own database names it; benchscout keeps no table (design.md section 5)
            name = " ".join(x for x in (found.get("maker"), found.get("model")) if x)
            src = got.get("source", "the tool") if found.get("model") else f"not in {got.get('source', 'the tool')}"
            if len(chain) > 1:
                src += f"; part {i + 1} of {len(chain)} on the chain"
            parts = targets
            if i > 0 and port is not None:  # another part on the same chain is a target of its own, not a rename of the first
                tid = f"{targets[0].id if targets else 'target:' + port.id}:part{i + 1}"
                if tid not in b.targets:
                    b.add_target(Target(tid, None, "unknown", Confidence.UNKNOWN, facts=[f"part {i + 1} of {len(chain)} on the JTAG chain"]))
                    b.link(port.id, tid, "jtag")
                parts = [b.targets[tid]]
            for t in parts:
                if name:  # what the tool's database says; the kind of part is not guessed from it
                    t.name, t.confidence = name, Confidence.IDENTIFIED
                t.evidence.append(Evidence("jtag idcode", f"0x{code:08X} ({src})"))


def listen_into(io: Io, path: str, targets: list[Target], seconds: float, exclusive: bool = False,
                baud: int = 115200, lines: str = "low", cancel=None) -> str | None:
    """Listen and fold what was heard into the targets. A port that vanishes mid-read
    (USB re-enumeration, usually a reset) is recorded as a fact, not a crash."""
    try:
        text = io.serial_listen(path, seconds, baud, exclusive, lines, **({"cancel": cancel} if cancel is not None else {}))
    except Exception as e:  # serial.SerialException / OSError: device went away
        _fold_listen(targets, None, str(e))
        return None
    _fold_listen(targets, text, None)
    return text


def _fold_listen(targets: list[Target], text: str | None, error: str | None, seen: str | None = None,
                 learned: list[dict] | None = None) -> None:
    when = f" (seen {seen})" if seen else ""
    if text is None:
        for t in targets:
            t.facts.append(f"ALERT: port disappeared while listening ({error}); the device likely reset or re-enumerated{when}")
        return
    if VANISHED_MARK in text:
        for t in targets:
            fact = f"the port vanished while listening (the device reset or re-enumerated) and came back; kept listening{when}"
            if fact not in t.facts:
                t.facts.append(fact)
    apply_log(targets, "\n".join(l for l in text.splitlines() if VANISHED_MARK not in l), seen=seen, learned=learned)


def apply_log(t_list: list[Target], text: str, seen: str | None = None, learned: list[dict] | None = None) -> None:
    """Fold console output into the targets. `seen` marks output heard earlier (before this
    scan): it still proves what the part is, but its lines are labelled with when."""
    c = K.classify_log(text, learned)
    when = f" (seen {seen})" if seen else ""
    for t in t_list:
        if not text.strip():
            if not seen:
                t.facts.append("silent for the listen window")
            continue
        t.facts += [f"ALERT: {a}{when}" for a in c["alerts"]]
        t.facts += [f"{'earlier log' if seen else 'log'}: {line}" for line in c["sample"]]


def _probe_tools_missing(b: Bench, port: Port) -> list:
    """Probe tools defined for this kind of port that know how to reach it, but are not installed."""
    from . import actions as A

    return [spec for spec in A.all_actions().values()
            if spec.probe and (not spec.port_kinds or port.kind in spec.port_kinds) and not b.tools.get(spec.argv[0])
            and ("{port.select*}" not in spec.argv or port.tools.get(spec.argv[0]))]


def _from_impact(text: str, imp: Impact, needs: str | None = None) -> Proposal:
    """A proposal that carries the broker's own reason, so the agent never has to guess it."""
    reason = imp.reason
    if imp.affected_ports:
        reason += f"; also disturbs {', '.join(imp.affected_ports)}"
    return Proposal(text, imp.action, imp.port_id, imp.effect.name.lower(), needs, reason)


def propose(b: Bench) -> list[Proposal]:
    """Only the generic next steps a cautious bench needs before any LLM is involved. What to do
    about a particular device (read its chip id, capture a rail) is the agent's call (design.md 3)."""
    from . import protocols

    out: list[Proposal] = []
    for port in b.ports.values():
        for t in b.targets_via(port.id):
            proto = protocols.for_port(port)
            if proto and proto.probe_action and port.path and t.confidence != Confidence.IDENTIFIED \
                    and port.may_reset_on_open and not any(f.startswith("log:") for f in t.facts):
                action, args = proto.probe_action  # what recon would have done, had opening this port been safe
                imp = classify(b, action, port.id, args)
                out.append(_from_impact(f"{action} {port.path} to see what is behind {b.devices[port.device_id].name}", imp))
            if t.confidence == Confidence.UNKNOWN:
                for spec in _probe_tools_missing(b, port):  # a probe that would name it, not installed (yet)
                    from . import fetch

                    exe = spec.argv[0]
                    imp = classify(b, spec.name, port.id)
                    out.append(_from_impact(f"Read {port.id} with {spec.name} to identify the part", imp,
                                            f"{exe} (downloaded on first use)" if fetch.plan(exe) else f"install {exe}"))
    # one proposal per (text) is enough
    seen, uniq = set(), []
    for p in out:
        if p.text not in seen:
            seen.add(p.text)
            uniq.append(p)
    return uniq
