"""Effect-based policy: classify by what an action does to device state, not read/write.

`esptool chip_id` reads, but resets the chip. A JTAG reconfig of an FPGA also
kills the soft-CPU console on another port. So impact is computed by walking the
graph: action -> targets it disturbs -> everything hosted on them -> every port
reaching any of those.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum

from .model import Bench


class Effect(IntEnum):
    SAFE = 0  # auto-run
    DISRUPTIVE = 1  # changes device/instrument state; needs approval
    DANGEROUS = 2  # can damage hardware or destroy state; approval + warning


@dataclass
class Impact:
    action: str
    port_id: str
    effect: Effect
    reason: str
    disturbs: list[str] = field(default_factory=list)  # target ids
    affected_ports: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "port": self.port_id,
            "effect": self.effect.name.lower(),
            "reason": self.reason,
            "disturbs": self.disturbs,
            "affected_ports": self.affected_ports,
        }


# action -> (effect, does it disturb the targets behind the port?, reason)
ACTIONS: dict[str, tuple[Effect, bool, str]] = {
    "serial.listen": (Effect.SAFE, False, "passively reads console output"),
    "serial.send": (Effect.DISRUPTIVE, True, "sends bytes the firmware will act on"),
    "esptool.chip_id": (Effect.DISRUPTIVE, True, "reads chip ID but resets the chip into the ROM bootloader via DTR/RTS"),
    "esptool.write_flash": (Effect.DANGEROUS, True, "overwrites flash"),
    "jtag.scan": (Effect.SAFE, False, "reads IDCODEs; needs exclusive use of the probe channel"),
    "jtag.halt": (Effect.DISRUPTIVE, True, "stops the CPU"),
    "fpga.program": (Effect.DANGEROUS, True, "reconfigures the FPGA; everything inside the fabric restarts"),
    "scpi.query": (Effect.SAFE, False, "query only (ends with '?')"),
    "scpi.write": (Effect.DISRUPTIVE, False, "changes instrument settings the user may be looking at"),
}

# SCPI writes that can push energy into the board.
_ENERGIZING = re.compile(r"^\s*:?(OUTP|OUTPUT|VOLT|CURR|APPL|SOUR)", re.I)


def _is_pure_query(cmd: str) -> bool:
    """Every ;-separated command's header ends in '?' (args may follow: MEAS:CURR? CH1)."""
    parts = [c.strip() for c in cmd.split(";") if c.strip()]
    return bool(parts) and all(c.split()[0].endswith("?") for c in parts)


def classify(bench: Bench, action: str, port_id: str, arg: str | None = None) -> Impact:
    if port_id not in bench.ports:
        raise KeyError(f"unknown port {port_id}")
    port = bench.ports[port_id]
    effect, disturbs_targets, reason = ACTIONS.get(action, (Effect.DANGEROUS, True, "unknown action"))

    if action == "serial.listen" and port.may_reset_on_open:
        effect = Effect.DISRUPTIVE
        reason = "opening this port can reset the device (DTR/RTS into an auto-reset circuit, or a native-USB chip resetting on open)"
        disturbs_targets = True
    if action == "scpi.query" and arg is not None and not _is_pure_query(arg):
        action = "scpi.write"
        effect, disturbs_targets, reason = ACTIONS["scpi.write"]
    if action == "scpi.write" and arg is not None and _ENERGIZING.match(arg):
        effect = Effect.DANGEROUS
        reason = "changes outputs that drive energy into the board"

    disturbed: list[str] = []
    if disturbs_targets:
        for t in bench.targets_via(port_id):
            for x in [t, *bench.dependents(t.id)]:
                if x.id not in disturbed:
                    disturbed.append(x.id)
    affected = sorted({p.id for tid in disturbed for p in bench.ports_reaching(tid)} - {port_id})
    return Impact(action, port_id, effect, reason, disturbed, affected)
