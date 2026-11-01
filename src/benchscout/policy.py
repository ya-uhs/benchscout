"""Effect-based policy: classify by what an action does to device state, not read/write.

`esptool chip_id` reads, but resets the chip. A JTAG reconfig of an FPGA also
kills the soft-CPU console on another port. So impact is computed by walking the
graph: action -> targets it disturbs -> everything hosted on them -> every port
reaching any of those.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

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


# SCPI writes that can push energy into the board.
# Any command in a ;-joined line, after any leading nodes ("CH1:VOLT 5", "INST:NSEL 1;VOLT 5", ":SOUR1:VOLT 3.3"):
# these SCPI subsystems set or switch outputs on supplies, generators and SMUs, whatever the vendor.
_ENERGIZING = re.compile(r"^:?([A-Za-z0-9*]+:)*(OUTP|OUTPUT|VOLT|CURR|APPL|SOUR)", re.I)


def _energizing(cmd: str) -> bool:
    """Some command in the line sets an output; queries ("MEAS:VOLT?") only read."""
    for c in (c.strip() for c in cmd.split(";")):
        if c and not c.split()[0].endswith("?") and _ENERGIZING.match(c):
            return True
    return False


def _is_pure_query(cmd: str) -> bool:
    """Every ;-separated command's header ends in '?' (args may follow: MEAS:CURR? CH1)."""
    parts = [c.strip() for c in cmd.split(";") if c.strip()]
    return bool(parts) and all(c.split()[0].endswith("?") for c in parts)


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
    if action == "scpi.query" and arg is not None and not _is_pure_query(arg):
        action = "scpi.write"
        effect, disturbs_targets, reason = _base("scpi.write")
        code = "action:scpi.write"
    if action == "scpi.write" and arg is not None and _energizing(arg):
        effect = Effect.DANGEROUS
        reason = "changes outputs that drive energy into the board"
        code = "energizing"

    disturbed: list[str] = []
    if disturbs_targets:
        for t in bench.targets_via(port_id):
            for x in [t, *bench.dependents(t.id)]:
                if x.id not in disturbed:
                    disturbed.append(x.id)
    affected = sorted({p.id for tid in disturbed for p in bench.ports_reaching(tid)} - {port_id})
    return Impact(action, port_id, effect, reason, disturbed, affected, code)
