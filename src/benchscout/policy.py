"""Effect-based policy: classify by what an action does to device state, not read/write.

`esptool chip_id` reads, but resets the chip. A JTAG reconfig of an FPGA also
kills the soft-CPU console on another port. So impact is computed by walking the
graph: action -> targets it disturbs -> everything hosted on them -> every port
reaching any of those.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from .actions import ArgError
from .model import Bench
from .policy_types import Effect


@dataclass
class Impact:
    action: str
    port_id: str
    effect: Effect
    reason: str
    disturbs: list[str] = field(default_factory=list)  # target ids
    affected_ports: list[str] = field(default_factory=list)
    # which reason this is, so the panel can say it in the person's language: "action:<name>" for the
    # action's own reason, or a rule's name. The English `reason` stays what the agent reads.
    code: str = ""
    # what a standing grant for this call is bound to besides (action, port): for SCPI, the command headers
    # (every way the instrument may read them), so allowing one command never allows another. None: the action.
    subjects: list[str] | None = None

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "port": self.port_id,
            "effect": self.effect.name.lower(),
            "reason": self.reason,
            "disturbs": self.disturbs,
            "affected_ports": self.affected_ports,
            "code": self.code,
            "subjects": self.subjects,
        }


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
SCPI_PORTS = ("usbtmc", "lan-scpi")
_SCPI_SAFE = {"*IDN?", "*OPC?", "*ESR?", "*STB?", "*ESE?", "*SRE?", "*OPT?", "*LRN?", "*PSC?", "*PRE?",
              "SYST:ERR?", "SYST:ERR:NEXT?", "SYST:ERR:COUN?", "SYST:ERR:ALL?", "SYST:VERS?"}
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


# ---- commands: one rule for every port kind -------------------------------------------------------------------------
# What is sent to a device is taken apart into commands by its protocol (a firmware's lines; SCPI's ;-joined units),
# each with the names it may be known by and its argument values. A command is judged by the rule remembered for it
# (an effect, and optionally limits on its argument values), else by the protocol's default. Nothing here knows a
# device: the rules are memory (the agent proposes, the human confirms what asks less), the defaults are the protocol's.

def _number(text: str) -> float | None:
    """A plain number (3.3, -1, 2e-3). A value with a unit suffix or a keyword (MAX, DEF) is not one: it asks."""
    try:
        return float(text) if re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", text.strip()) else None
    except ValueError:
        return None


def outside(limits: list, args: list[str]) -> str:
    """Why these argument values are outside the limits ("" if within). `limits[i]` is None (any value) or
    {"min": x, "max": y} (either may be left out) for the i-th value after the command."""
    for i, lim in enumerate(limits or []):
        if lim is None:
            continue
        if i >= len(args):
            return f"value {i + 1} is missing"
        v = _number(args[i])
        if v is None:
            return f"value {i + 1} ({args[i]!r}) is not a plain number"
        if "min" in lim and v < lim["min"] or "max" in lim and v > lim["max"]:
            return f"value {i + 1} = {args[i]} is outside {lim.get('min', '')}..{lim.get('max', '')}"
    return ""


def judge_rule(rule: dict, name: str, args: list[str]) -> tuple[Effect, str, str]:
    """(effect, reason, code) of one command under the rule remembered for it."""
    effect = Effect[rule["effect"].upper()]
    why = outside(rule.get("limits") or [], args)
    if why:
        return (Effect.DANGEROUS, f"`{name}`: {why}, the limits remembered for it; outside them it asks every time",
                "outside_limits")
    reason = (f"`{name}` is remembered as {rule['effect'].upper()}" + (" (confirmed by the human)" if rule.get("loosens") else "")
              + (f": {rule['text']}" if rule.get("text") else "") + (" (within its value limits)" if rule.get("limits") else ""))
    return effect, reason, f"command_{rule['effect']}"


def _same_values(values: list[str]) -> Callable[[str], list[str]]:
    return lambda _name: values


def _values_after(words: list[str]) -> Callable[[str], list[str]]:
    return lambda name: words[len(name.split()):]


def commands(scpi: bool, text: str) -> list[tuple[list[str], Callable[[str], list[str]]]]:
    """Each command in `text` as (the names it may be known by; its argument values under a name). SCPI: every way the
    instrument may read the header (all of them count). A firmware line: its words from the most specific ("set 42") to
    the command alone ("set"), the most specific remembered one being the one meant."""
    out: list[tuple[list[str], Callable[[str], list[str]]]] = []
    if scpi:  # (by the action, never by the port: a SCPI line is read as SCPI wherever it is sent)
        pieces = [u.strip() for u in text.split(";") if u.strip()]
        for ways, piece in zip(scpi_headers(text), pieces):
            rest = piece.split(None, 1)[1] if len(piece.split(None, 1)) > 1 else ""
            out.append((ways, _same_values([a.strip() for a in rest.split(",") if a.strip()])))
        return out
    for line in sent_lines(text):
        words = line.split()
        out.append(([" ".join(words[:n]) for n in range(len(words), 0, -1)], _values_after(words)))
    return out


def judge_commands(port, text: str, default: Callable[[str], tuple[Effect, str, str]],
                   scpi: bool = False) -> tuple[Effect, str, str, list[str]]:
    """The strictest of the commands in `text`, each by its remembered rule or `default(name)`; and the names a standing
    grant for this text is bound to (every name it may be read by)."""
    def by(name: str, values) -> tuple[Effect, str, str]:
        rule = port.commands.get(name)
        return judge_rule(rule, name, values(name)) if rule and not rule.get("pending") else default(name)

    worst = (Effect.SAFE, "", "")
    subjects: set[str] = set()
    for names, values in commands(scpi, text):
        if scpi:  # the instrument may read it any of these ways: the strictest counts
            judged = max((by(n, values) for n in names), key=lambda j: j[0])
            subjects |= set(names)
        else:  # the most specific remembered form is the command meant; else the command word alone
            name = next((n for n in names if n in port.commands and not port.commands[n].get("pending")), names[-1])
            judged = by(name, values)
            subjects.add(name)
        if judged[0] > worst[0] or not worst[1]:
            worst = judged
    return (*worst, sorted(subjects))


def _scpi_default(h: str) -> tuple[Effect, str, str]:
    effect, code = scpi_default(h)
    return effect, f"{_SCPI_REASONS[code]}: {h}", code


def _scpi(port, cmd: str) -> tuple[Effect, str, str, list[str]]:
    """The strictest of every unit, read every way the instrument may read it; and every header, which is what
    a standing grant for this line is bound to."""
    return judge_commands(port, cmd, _scpi_default, scpi=True)


def _base(action: str) -> tuple[Effect, bool, str]:
    from .actions import all_actions

    a = all_actions().get(action)
    return (a.effect, a.disturbs, a.reason) if a else (Effect.DANGEROUS, True, "unknown action")


SERIAL_OPENING = ("serial.listen", "serial.watch", "serial.send", "serial.command")  # each opens the port unless held open


def sent_lines(text: str) -> list[str]:
    """The lines a firmware will read from what is sent (data and its line ending together)."""
    return [line.strip() for line in re.split(r"[\r\n]+", text) if line.strip()]


def classify(bench: Bench, action: str, port_id: str, args: dict | None = None, port_open: bool = False) -> Impact:
    """`args` are the action's arguments; for SCPI the command line decides the effect. `port_open`: the port is held
    open already at these settings (watched), so the call opens nothing. The effect is the strongest of the call's
    steps: opening the port, then what is sent (design.md section 3, runtime contracts)."""
    if port_id not in bench.ports:
        raise KeyError(f"unknown port {port_id}")
    port = bench.ports[port_id]
    subjects: list[str] | None = None
    effect, disturbs_targets, reason = _base(action)
    code = f"action:{action}" if reason != "unknown action" else "unknown_action"
    arg = (args or {}).get("command")

    if action in ("serial.listen", "serial.watch") and port_open:
        effect, disturbs_targets = Effect.SAFE, False
        reason, code = "the port is already open (watched); reading it opens nothing", "watched"
    if action == "serial.send":
        # what is sent decides, command by command: one remembered as stronger than sending bytes stays so (a raw send
        # is not a way around it), and a grant covers only the commands the human saw. A raw send never asks less
        # than sending bytes the firmware acts on.
        sent = str((args or {}).get("data", "")) + str((args or {}).get("eol", ""))
        judged, why, how, subjects = judge_commands(port, sent, lambda n: (effect, reason, code))
        subjects = subjects or [""]
        if judged > effect:
            effect, reason, code = judged, why, how
    if action == "serial.command":
        name = str((args or {}).get("name", ""))
        sent = f"{name} {(args or {}).get('args') or ''}".strip()
        subjects = [name]  # a grant for one remembered command is not one for another
        known = port.commands.get(name)
        if known and not known.get("pending"):
            effect, reason, code = judge_rule(known, name, commands(False, sent)[0][1](name) if sent else [])
            disturbs_targets = effect > Effect.SAFE
        else:  # not remembered, or its SAFE is not confirmed yet: like sending bytes
            reason = "a firmware command not confirmed as safe acts like sending bytes the firmware will act on"
            code = "command_unconfirmed"
    if action in ("scpi.query", "scpi.write") and arg is not None:
        # one rule for both: the command line decides, not which of the two the agent called
        effect, reason, code, subjects = _scpi(port, arg)
        disturbs_targets = effect > Effect.SAFE
    if action in SERIAL_OPENING and not port_open and port.may_reset_on_open and effect < Effect.DISRUPTIVE:
        # opening is a step of the call: a command that is safe once sent is not safe to send through a port that resets
        effect = Effect.DISRUPTIVE
        reason = "opening this port can reset the device (DTR/RTS into an auto-reset circuit, or a native-USB chip resetting on open)"
        code = "reset_on_open"
        disturbs_targets = True

    disturbed: list[str] = []
    if disturbs_targets:
        for t in bench.targets_via(port_id):
            for x in [t, *bench.dependents(t.id)]:
                if x.id not in disturbed:
                    disturbed.append(x.id)
    affected = sorted({p.id for tid in disturbed for p in bench.ports_reaching(tid)} - {port_id})
    return Impact(action, port_id, effect, reason, disturbed, affected, code, subjects)
