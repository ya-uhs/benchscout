"""Effect-based policy: classify by what an action does to device state, not read/write.

`esptool chip_id` reads, but resets the chip. A JTAG reconfig of an FPGA also
kills the soft-CPU console on another port. So impact is computed by walking the
graph: action -> targets it disturbs -> everything hosted on them -> every port
reaching any of those.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

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

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "port": self.port_id,
            "effect": self.effect.name.lower(),
            "reason": self.reason,
            "disturbs": self.disturbs,
            "affected_ports": self.affected_ports,
            "code": self.code,
        }


# ---- SCPI ------------------------------------------------------------------------------------
# SCPI fixes the syntax for every instrument, but the meaning of only a few commands: IEEE 488.2's common
# commands and SCPI-99's required SYSTem queries. Only those are judged by name here. What any other command
# does depends on the instrument, so no list of patterns can say it is harmless (a query is meant to only
# read, yet MEASure?/READ? start a measurement and a source-measure unit may switch its output on for it;
# a vendor may call its output INPut, BSWV AMP or *RCL a stored state). Hence:
#   the standard's status and identity queries         SAFE
#   its status-register writes (*CLS, *ESE ...)        DISRUPTIVE
#   *TST? and *CAL? (they run a self-test/calibration) DANGEROUS
#   any other query                                    DISRUPTIVE (one card; the human knows what the instrument is)
#   any other command                                  DANGEROUS (one card per command)
# Loosening one command on one instrument is memory the human confirmed (bench_remember kind command).
# _ENERGIZING only words the reason on the card; missing a vendor's syntax there changes nothing.
SCPI_PORTS = ("usbtmc", "lan-scpi")
_SCPI_SAFE = {"*IDN?", "*OPC?", "*ESR?", "*STB?", "*ESE?", "*SRE?", "*OPT?", "*LRN?", "*PSC?", "*PRE?",
              "SYST:ERR?", "SYST:ERR:NEXT?", "SYST:ERR:COUN?", "SYST:ERR:ALL?", "SYST:VERS?"}
_SCPI_STATUS_WRITES = {"*CLS", "*ESE", "*SRE", "*WAI", "*OPC"}
_SCPI_PROCEDURES = {"*TST?", "*CAL?"}
_ENERGIZING = re.compile(r"(^|:)(OUTP|VOLT|CURR|APPL|SOUR|INP|POW|AMPL|LOAD|PROG|\*RCL|\*RST)", re.I)
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
    out, path = [], []
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
    if header in _SCPI_STATUS_WRITES:
        return Effect.DISRUPTIVE, "scpi_status_write"
    if query:
        return Effect.DISRUPTIVE, "scpi_query"
    return Effect.DANGEROUS, "energizing" if _ENERGIZING.search(header) else "scpi_command"


_SCPI_REASONS = {
    "scpi_status": "a status or identity query whose meaning the standard fixes (IEEE 488.2 / SCPI)",
    "scpi_procedure": "the standard defines it to run a self-test or calibration, which can switch outputs",
    "scpi_status_write": "changes only the status registers (IEEE 488.2)",
    "scpi_query": "a query is meant to only read, but some act (MEASure?/READ? start a measurement; a source-measure "
                  "unit may switch its output on for it); what this instrument does is not known",
    "scpi_command": "what this command does depends on the instrument; the standard does not fix it",
    "energizing": "sets an output, voltage, current or source: it may drive energy into the board",
}


def _scpi(port, cmd: str) -> tuple[Effect, str, str]:
    """The strictest of every unit, read every way the instrument may read it."""
    worst = (Effect.SAFE, "", "")
    for ways in scpi_headers(cmd):
        for h in ways:
            known = port.commands.get(h)
            if known and not known.get("pending"):
                effect = Effect[known["effect"].upper()]
                code = f"command_{known['effect']}"
                reason = (f"remembered for this instrument as {known['effect'].upper()}"
                          + (" (confirmed by the human)" if known.get("loosens") else "") + f": {h}")
            else:
                effect, code = scpi_default(h)
                reason = f"{_SCPI_REASONS[code]}: {h}"
            if effect > worst[0] or not worst[1]:
                worst = (effect, reason, code)
    return worst


def _base(action: str) -> tuple[Effect, bool, str]:
    from .actions import all_actions

    a = all_actions().get(action)
    return (a.effect, a.disturbs, a.reason) if a else (Effect.DANGEROUS, True, "unknown action")


def classify(bench: Bench, action: str, port_id: str, args: dict | None = None) -> Impact:
    """`args` are the action's arguments; for SCPI the command line decides the effect."""
    if port_id not in bench.ports:
        raise KeyError(f"unknown port {port_id}")
    port = bench.ports[port_id]
    effect, disturbs_targets, reason = _base(action)
    code = f"action:{action}" if reason != "unknown action" else "unknown_action"
    arg = (args or {}).get("command")

    if action in ("serial.listen", "serial.watch") and port.may_reset_on_open:
        effect = Effect.DISRUPTIVE
        reason = "opening this port can reset the device (DTR/RTS into an auto-reset circuit, or a native-USB chip resetting on open)"
        code = "reset_on_open"
        disturbs_targets = True
    if action == "serial.command":
        known = port.commands.get((args or {}).get("name", ""))
        if known and not known.get("pending"):
            effect = Effect[known["effect"].upper()]
            disturbs_targets = effect > Effect.SAFE
            reason = (f"remembered as {known['effect'].upper()}" + (" (confirmed by the human)" if effect == Effect.SAFE else "")
                      + (f": {known['text']}" if known.get("text") else ""))
            code = f"command_{known['effect']}"
        else:  # not remembered, or its SAFE is not confirmed yet: like sending bytes
            reason = "a firmware command not confirmed as safe acts like sending bytes the firmware will act on"
            code = "command_unconfirmed"
    if action in ("scpi.query", "scpi.write") and arg is not None:
        # one rule for both: the command line decides, not which of the two the agent called
        effect, reason, code = _scpi(port, arg)
        disturbs_targets = effect > Effect.SAFE

    disturbed: list[str] = []
    if disturbs_targets:
        for t in bench.targets_via(port_id):
            for x in [t, *bench.dependents(t.id)]:
                if x.id not in disturbed:
                    disturbed.append(x.id)
    affected = sorted({p.id for tid in disturbed for p in bench.ports_reaching(tid)} - {port_id})
    return Impact(action, port_id, effect, reason, disturbed, affected, code)
