"""Action registry: every action the agent can ask for, with its effect and typed arguments.

One place says what an action is, what it does to device state, and what each argument
means, so the policy, the broker and the MCP tool descriptions cannot drift apart
(design.md section 5 and 10: `serial.listen arg=9600` was read as 9600 seconds).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .policy_types import Effect


class ArgError(ValueError):
    pass


@dataclass(frozen=True)
class Param:
    name: str
    type: type
    doc: str
    default: object = None
    required: bool = False
    min: float | None = None
    max: float | None = None

    def coerce(self, value):
        if self.type is bool:
            if isinstance(value, bool):
                return value
            raise ArgError(f"{self.name} must be true or false")
        try:
            v = self.type(value)
        except (TypeError, ValueError):
            raise ArgError(f"{self.name} must be {self.type.__name__}, got {value!r}") from None
        if self.type is float and isinstance(value, str) and not value.strip():
            raise ArgError(f"{self.name} must be a number")
        if self.min is not None and v < self.min or self.max is not None and v > self.max:
            raise ArgError(f"{self.name}={v} is out of range [{self.min}, {self.max}]")
        return v

    def describe(self) -> str:
        bits = [self.type.__name__]
        if self.required:
            bits.append("required")
        elif self.default is not None:
            bits.append(f"default {self.default}")
        if self.min is not None or self.max is not None:
            bits.append(f"{self.min}..{self.max}")
        return f"{self.name} ({', '.join(bits)}): {self.doc}"


@dataclass(frozen=True)
class Action:
    name: str
    effect: Effect  # effect before port-specific rules (policy.classify may raise it)
    disturbs: bool  # does it disturb the targets behind the port?
    reason: str
    doc: str
    params: tuple[Param, ...] = field(default_factory=tuple)
    implemented: bool = True

    def normalize(self, args: dict | None) -> dict:
        if args is not None and not isinstance(args, dict):
            raise ArgError(f"args must be an object like {{\"seconds\": 3}}, not {args!r}; "
                           f"{self.name} arguments: {self.describe_params() or 'none'}")
        args = dict(args or {})
        unknown = set(args) - {p.name for p in self.params}
        if unknown:
            raise ArgError(f"{self.name} has no argument {', '.join(sorted(unknown))}; "
                           f"arguments: {self.describe_params() or 'none'}")
        out = {}
        for p in self.params:
            if p.name in args and args[p.name] is not None:
                out[p.name] = p.coerce(args[p.name])
            elif p.required:
                raise ArgError(f"{self.name} needs {p.describe()}")
            elif p.default is not None:
                out[p.name] = p.default
        return out

    def describe_params(self) -> str:
        return "; ".join(p.describe() for p in self.params)


_LISTEN = (
    Param("seconds", float, "how long to listen", 3.0, min=0.05, max=60),
    Param("baud", int, "baud rate", 115200, min=50, max=12_000_000),
)
_COMMAND = (Param("command", str, "SCPI command line", required=True),)

ACTIONS: dict[str, Action] = {a.name: a for a in (
    Action("serial.listen", Effect.SAFE, False, "passively reads console output",
           "listen to a serial port and save what it prints", _LISTEN),
    Action("serial.send", Effect.DISRUPTIVE, True, "sends bytes the firmware will act on",
           "send text to a serial port and read the answer",
           (Param("data", str, "text to send", required=True),
            Param("eol", str, "appended to data: \"\\n\" (default), \"\\r\\n\", or \"\" for single-byte commands", "\n"),
            Param("seconds", float, "how long to read the answer", 1.0, min=0.05, max=30),
            Param("baud", int, "baud rate", 115200, min=50, max=12_000_000))),
    Action("esptool.chip_id", Effect.DISRUPTIVE, True,
           "reads chip ID but resets the chip into the ROM bootloader via DTR/RTS", "read the exact ESP chip with esptool"),
    Action("esptool.write_flash", Effect.DANGEROUS, True, "overwrites flash", "write an image to flash",
           (Param("address", int, "flash offset", required=True), Param("image", str, "image file", required=True)),
           implemented=False),
    Action("jtag.scan", Effect.SAFE, False, "reads IDCODEs; needs exclusive use of the probe channel",
           "read the IDCODEs on a JTAG chain"),
    Action("jtag.halt", Effect.DISRUPTIVE, True, "stops the CPU", "halt the CPU over JTAG", implemented=False),
    Action("fpga.program", Effect.DANGEROUS, True, "reconfigures the FPGA; everything inside the fabric restarts",
           "load a bitstream into the FPGA (SRAM only)", (Param("bitstream", str, "bitstream file", required=True),),
           implemented=False),
    Action("scpi.query", Effect.SAFE, False, "query only (ends with '?')", "send a SCPI query and read the answer", _COMMAND),
    Action("scpi.write", Effect.DISRUPTIVE, False, "changes instrument settings the user may be looking at",
           "send a SCPI command that changes settings", _COMMAND),
)}


def get(name: str) -> Action:
    if name not in ACTIONS:
        raise ArgError(f"unknown action {name!r}; actions: {', '.join(ACTIONS)}")
    return ACTIONS[name]


def canonical(args: dict) -> str:
    """The arguments an approval is bound to, in one comparable form."""
    return json.dumps(args, sort_keys=True)


def catalog() -> str:
    """For tool descriptions: every action with its arguments."""
    lines = ["(The effect in brackets is the base one. The port can raise it, e.g. opening a port that "
             "resets the device; bench_impact gives the actual effect.)"]
    for a in ACTIONS.values():
        state = "" if a.implemented else " (not implemented yet)"
        params = f" args: {a.describe_params()}" if a.params else ""
        lines.append(f"- {a.name} [{a.effect.name}]{state}: {a.doc}.{params}")
    return "\n".join(lines)
