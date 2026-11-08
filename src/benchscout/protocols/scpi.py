"""SCPI over VISA (USBTMC, LAN): IEEE 488.2 messages, read every way an instrument may read them."""

from __future__ import annotations

import re

from ..action_types import ArgError
from ..policy_types import Effect
from ..model import Confidence, Device, Evidence, Port, Target, device_id, hex4
from .base import Context, Judged, Protocol, Values

# ---- SCPI ------------------------------------------------------------------------------------
# SCPI fixes the syntax for every instrument, but the meaning of only a few commands: IEEE 488.2's common
# commands and SCPI-99's required SYSTem queries. What any other command does depends on the instrument, so
# no list of patterns can say it is harmless. So nothing outside the standard runs without the human having
# seen that very command; what the human allows is bound to it (design.md section 12, 2026-11-02):
#   the standard's status and identity queries                  SAFE
#   its status-register writes (*CLS, *ESE ...)                 DISRUPTIVE
#   *TST?, *CAL? (self-test, calibration), *RST, *RCL, *SAV,
#   *PUD (reset, recall or overwrite the whole stored state)     DANGEROUS (the standard says what they do)
#   any other query or command                                  DISRUPTIVE: asks; "for the session" or "always on
#                                                               this instrument" covers that command header only
#   ... that names an output, voltage, current, source, load    DANGEROUS: each value is the human's call
# The last row is a list, and a list misses things (a generator's BSWV AMP). It only adds caution on top of a
# default that is acceptable alone: a missed one still asks, shows the command, and its grant covers only it.
SCPI_PORTS = ("usbtmc", "lan-scpi")  # the port kinds that carry SCPI
_SCPI_SAFE = {"*IDN?", "*OPC?", "*STB?", "*ESE?", "*SRE?", "*OPT?", "*LRN?", "*PSC?", "*PRE?",
              "SYST:ERR:COUN?", "SYST:VERS?"}
_SCPI_STATUS_WRITES = {"*CLS", "*ESE", "*SRE", "*WAI", "*OPC"}
_SCPI_PROCEDURES = {"*TST?", "*CAL?"}
_SCPI_WHOLE_STATE = {"*RST", "*RCL", "*SAV", "*PUD"}
_ENERGIZING = re.compile(r"(^|:)(OUTP|VOLT|CURR|APPL|SOUR|INP|POW|AMPL|LOAD|PROG)", re.I)
_NODE = re.compile(r"(\*?[A-Za-z_]+)(\d*)")


class ScpiError(ArgError):
    pass


def _short(node: str) -> str:
    """SCPI-99 6.2.1: a node's short form is its first four letters, three when the fourth is a vowel."""
    m = _NODE.fullmatch(node)
    if not m:
        return node.upper()
    word, suffix = m.group(1).upper(), m.group(2)
    if not word.startswith("*") and len(word) > 4:
        word = word[:3] if word[3] in "AEIOU" else word[:4]
    return word + suffix


def scpi_headers(cmd: str) -> list[list[str]]:
    """Each ;-joined unit's header, in short form and upper case, as every way an instrument may read it:
    relative to the path the previous unit left (SCPI-99 6.2.4) and from the root (some do not keep a path).
    A line break or other control character would end the message early and start another one the checks
    never saw, so a command line with anything but printable ASCII (SCPI's own character set) is refused."""
    if any(not (32 <= ord(c) < 127 or c == "\t") for c in cmd):
        raise ScpiError("one SCPI message per call, in printable ASCII: no line breaks or other control characters "
                        "(benchscout ends the message)")
    out: list[list[str]] = []
    path: list[str] = []
    for unit in (u.strip() for u in cmd.split(";")):  # a ; inside a quoted string only adds units: stricter
        if not unit:
            continue
        head = unit.split()[0]
        query = head.endswith("?")
        head = head.rstrip("?")
        if head.startswith("*"):
            out.append([head.upper() + "?" * query])
            continue
        nodes = [_short(n) for n in head.lstrip(":").split(":")]
        full = nodes if head.startswith(":") else path + nodes
        path = full[:-1]
        out.append(sorted({":".join(x) + "?" * query for x in (full, nodes)}))
    if not out:
        raise ScpiError("an empty SCPI command")
    return out


def scpi_default(header: str) -> tuple[Effect, str]:
    """(effect, code) of one unit's header when nothing is remembered about it."""
    query = header.endswith("?")
    if header in _SCPI_SAFE:
        return Effect.SAFE, "scpi_status"
    if header in _SCPI_PROCEDURES:
        return Effect.DANGEROUS, "scpi_procedure"
    if header in _SCPI_WHOLE_STATE:
        return Effect.DANGEROUS, "scpi_whole_state"
    if header in _SCPI_STATUS_WRITES:
        return Effect.DISRUPTIVE, "scpi_status_write"
    if query:
        return Effect.DISRUPTIVE, "scpi_query"
    if _ENERGIZING.search(header):
        return Effect.DANGEROUS, "energizing"
    return Effect.DISRUPTIVE, "scpi_command"


_SCPI_REASONS = {
    "scpi_status": "a status or identity query whose meaning the standard fixes (IEEE 488.2 / SCPI)",
    "scpi_procedure": "the standard defines it to run a self-test or calibration, which can switch outputs",
    "scpi_whole_state": "resets or recalls the instrument's whole state, or overwrites a stored one (IEEE 488.2)",
    "scpi_status_write": "changes only the status registers (IEEE 488.2)",
    "scpi_query": "a query is meant to only read, but some act (MEASure?/READ? start a measurement; a source-measure "
                  "unit may switch its output on for it); what this instrument does is not known",
    "scpi_command": "what this command does depends on the instrument; the standard does not fix it. Allowing it "
                    "for the session or always covers this command only",
    "energizing": "sets an output, voltage, current or source: it may drive energy into the board",
}




def parse_idn(idn: str) -> dict:
    """IEEE 488.2 *IDN?: maker, model, serial, firmware, comma-separated. Only what the instrument says."""
    parts = [p.strip() for p in idn.split(",")]
    maker, model, serial = (parts + ["", "", ""])[:3]
    return {"maker": maker, "model": model, "serial": serial}


def _same_values(values: list[str]) -> Values:
    return lambda _name: values


class Scpi(Protocol):
    name = "scpi"
    port_kinds = SCPI_PORTS
    many_readings = True  # relative to the path the last unit left, and from the root: the instrument may read either

    def check(self, text: str) -> None:
        scpi_headers(text)

    def commands(self, text: str) -> list[tuple[list[str], Values]]:
        out: list[tuple[list[str], Values]] = []
        pieces = [u.strip() for u in text.split(";") if u.strip()]
        for ways, piece in zip(scpi_headers(text), pieces):
            rest = piece.split(None, 1)[1] if len(piece.split(None, 1)) > 1 else ""
            out.append((ways, _same_values([a.strip() for a in rest.split(",") if a.strip()])))
        return out

    def default(self, name: str) -> Judged:
        effect, code = scpi_default(name)
        return effect, f"{_SCPI_REASONS[code]}: {name}", code

    probe_action = ("scpi.query", {"command": "*IDN?"})

    def probe(self, io, port, seconds: float) -> dict | None:
        try:
            answer = io.visa_query(port.path, "*IDN?")
        except OSError as e:  # what VISA said is the clue
            return {"probe": "identity", "result": None, "error": str(e), "source": "scpi *IDN?"}
        return dict(parse_idn(answer) if answer else {}, probe="identity", result=answer, source="scpi *IDN?")

    def enumerate(self, io, bench, names: dict) -> None:
        for res in io.visa_resources():
            _add_visa(bench, res, names)

    def key(self, command: str) -> str:
        try:
            units = scpi_headers(command)
        except ArgError:
            return ""
        return units[0][0] if len(units) == 1 else ""


SCPI = Scpi()


def declared_length(data: bytes) -> int | None:
    """The length a definite-length IEEE 488.2 block says it has (None for anything else)."""
    if len(data) >= 2 and data[:1] == b"#" and data[1:2].isdigit() and data[1:2] != b"0":
        n = int(data[1:2])
        if len(data) >= 2 + n and data[2:2 + n].isdigit():
            return int(data[2:2 + n])
    return None


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


# ---- how instruments are found: VISA resources (the USB and serial lists do not show a LAN instrument) -----------

_VISA_USB = re.compile(r"USB\d*::(0x[0-9A-Fa-f]+)::(0x[0-9A-Fa-f]+)::([^:]+)::")
_VISA_TCP = re.compile(r"TCPIP\d*::([^:]+)::")


def _add_visa(b, res: str, names: dict | None = None) -> None:
    if res.upper().startswith("ASRL"):
        # VISA also lists serial ports. Sending *IDN? there would write bytes into an
        # unknown UART, so serial ports are only handled by the serial path.
        return
    if m := _VISA_USB.match(res):
        vid, pid, sn = int(m.group(1), 16), int(m.group(2), 16), m.group(3)
        did = device_id(vid, pid, sn, None)
        if did not in b.devices:
            b.add_device(Device(did, (names or {}).get((vid, pid, sn)) or f"USBTMC instrument {hex4(vid)}:{hex4(pid)}", "usb", vid, pid, sn))
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



# ---- operations (the gate finds them by name; `ctx` is the broker) -------------------------------------------------

def op_message(ctx: Context, port, args: dict):
    """A query or a command, by what the line is: an answer is read only when it asks for one."""
    try:
        if not any(h[0].endswith("?") for h in scpi_headers(args["command"])):
            ctx.io.visa_write(port.path, args["command"])
            return {"sent": True}
        if args.get("save"):
            return _save_capture(ctx, port, args["command"], args["save"])
        return ctx.io.visa_query(port.path, args["command"])
    except OSError as e:  # VISA's own words: not there, busy, or no answer in time
        return {"error": f"the call to the instrument failed: {e}"}


def _save_capture(ctx: Context, port, command: str, kind: str) -> dict:
    """A screen capture or a waveform into the workspace (design.md section 2: "saved here" is enough; the agent
    reports the numbers), without its IEEE 488.2 block header."""
    from .. import actions as A

    if kind not in A.SAVE_KINDS:
        return {"error": f"save must be one of {', '.join(A.SAVE_KINDS)}"}
    raw = ctx.io.visa_query_raw(port.path, command)  # (no answer in time: check the command in the programming manual)
    data = ieee_block(raw)
    p = ctx.new_artifact(f"{port.id}-capture", kind)
    p.write_bytes(data)
    out: dict = {"saved_to": str(p.resolve()), "bytes": len(data), "block": raw[:1] == b"#"}
    declared = declared_length(raw)
    if declared is not None and len(data) < declared:  # the answer stopped early (a time-out, a cut connection)
        out["warning"] = f"incomplete: the block says {declared} bytes, {len(data)} arrived; the file is cut short"
    if kind in ("csv", "txt"):
        out["preview"] = data[:600].decode("utf-8", errors="replace")
    elif kind == "png" and not data.startswith(b"\x89PNG"):
        out["warning"] = "this is not a PNG file (check the format the command asks for)"
    return out


OPERATIONS = {"scpi.query": op_message, "scpi.write": op_message}
