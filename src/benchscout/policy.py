"""Effect-based policy: classify by what an action does to device state, not read/write.

`esptool chip_id` reads, but resets the chip. A JTAG reconfig of an FPGA also
kills the soft-CPU console on another port. So impact is computed by walking the
graph: action -> targets it disturbs -> everything hosted on them -> every port
reaching any of those.
"""

from __future__ import annotations

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


def _base(action: str) -> tuple[Effect, bool, str]:
    from .actions import all_actions

    a = all_actions().get(action)
    return (a.effect, a.disturbs, a.reason) if a else (Effect.DANGEROUS, True, "unknown action")


def classify(bench: Bench, action: str, port_id: str, args: dict | None = None, port_open: bool = False) -> Impact:
    """What a call does to the device, and why. The effect is the strongest of the call's steps (design.md section 3,
    runtime contracts): opening the port (unless it is held open already at these settings: `port_open`), then
    what it sends, judged command by command through the protocol the action declares. Nothing here names a
    protocol, a port kind or an action."""
    from . import protocols
    from .actions import all_actions

    if port_id not in bench.ports:
        raise KeyError(f"unknown port {port_id}")
    port = bench.ports[port_id]
    spec = all_actions().get(action)
    subjects: list[str] | None = None
    effect, disturbs_targets, reason = _base(action)
    code = f"action:{action}" if reason != "unknown action" else "unknown_action"

    proto = protocols.get(spec.protocol) if spec and spec.protocol else None
    text = (args or {}).get(spec.text) if spec and spec.text else None
    if proto and text is not None:
        # what is sent decides, command by command, under one rule table for every protocol (nothing in it to read as a
        # command, such as an empty line: the action's own effect stands)
        judged, why, how, subjects = protocols.judge(port, str(text) + str((args or {}).get("eol") or ""), proto)
        subjects = subjects or [""]
        if why:
            effect, reason, code = judged, why, how
            disturbs_targets = effect > Effect.SAFE
    if spec and spec.opens:
        if port_open and effect == Effect.SAFE and spec.effect == Effect.SAFE:
            reason, code = "the port is already open (watched); reading it opens nothing", "watched"
        elif not port_open and port.may_reset_on_open and effect < Effect.DISRUPTIVE:
            # opening is a step of the call: what is safe once sent is not safe to send through a port that resets
            effect = Effect.DISRUPTIVE
            reason = ("opening this port can reset the device (DTR/RTS into an auto-reset circuit, or a native-USB chip "
                      "resetting on open)")
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
