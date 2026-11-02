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


class FilePath(str):
    """A file argument (checked to exist, relative to the workspace, when the action runs)."""


class FlashMap(list):
    """Pairs of flash offset and image file: "0x0:boot.bin,0x10000:app.bin" or {"0x0": "boot.bin"}."""


_TYPES = {"str": str, "int": int, "float": float, "bool": bool, "file": FilePath, "flash_map": FlashMap}


@dataclass(frozen=True)
class Param:
    name: str
    type: type
    doc: str
    default: object = None
    required: bool = False
    min: float | None = None
    max: float | None = None
    ext: tuple[str, ...] = ()  # for files: accepted extensions

    def coerce(self, value):
        if self.type is FilePath:
            v = FilePath(str(value))
            if self.ext and not v.lower().endswith(tuple(e.lower() for e in self.ext)):
                raise ArgError(f"{self.name} must be a {'/'.join(self.ext)} file, got {value!r}")
            return v
        if self.type is FlashMap:
            items = value.items() if isinstance(value, dict) else [x.split(":", 1) for x in str(value).split(",") if x.strip()]
            out = FlashMap()
            for addr, path in items:
                try:
                    out.append([int(str(addr).strip(), 0), FilePath(str(path).strip())])
                except ValueError:
                    raise ArgError(f"{self.name}: {addr!r} is not a flash offset (like 0x10000)") from None
            if not out:
                raise ArgError(f"{self.name} needs at least one offset:file pair")
            return out
        if self.type is int and isinstance(value, str):
            try:
                return self._range(int(value, 0))
            except ValueError:
                raise ArgError(f"{self.name} must be int, got {value!r}") from None
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
        return self._range(v)

    def _range(self, v):
        if self.min is not None and v < self.min or self.max is not None and v > self.max:
            raise ArgError(f"{self.name}={v} is out of range [{self.min}, {self.max}]")
        return v

    def describe(self) -> str:
        names: dict[type, str] = {FilePath: "file", FlashMap: "offset:file,..."}
        bits = [names.get(self.type, self.type.__name__)]
        if self.ext:
            bits.append("/".join(self.ext))
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
    # defined as data (design.md section 4): an external tool run with these arguments, no shell
    argv: tuple[str, ...] = ()
    port_kinds: tuple[str, ...] = ()  # which kinds of port it applies to ("" = any)
    releases_device: bool = False  # the tool opens the device itself: stop watching it meanwhile
    timeout: float = 60.0
    version: dict = field(default_factory=dict)  # {"argv": [...], "min": "5.0", "why": "..."}

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
                out[p.name] = p.coerce(p.default)  # defaults go through the same checks
        return out

    def describe_params(self) -> str:
        return "; ".join(p.describe() for p in self.params)


_LISTEN = (
    Param("seconds", float, "how long to listen", 3.0, min=0.05, max=60),
    Param("baud", int, "baud rate", 115200, min=50, max=12_000_000),
)
_COMMAND = (Param("command", str, "SCPI command line", required=True),)
SAVE_KINDS = ("png", "bmp", "jpg", "csv", "txt", "bin")
# a screen capture or waveform: the answer goes to a file in the workspace (an IEEE 488.2 block header is taken off).
# Which command gives it is the instrument's (its programming manual); benchscout keeps no list of them.
_QUERY = _COMMAND + (Param("save", str, f"save the answer to a file instead of returning it: one of {', '.join(SAVE_KINDS)} "
                                        "(e.g. a scope's screen as png, its waveform as csv)"),)

BUILTIN: dict[str, Action] = {a.name: a for a in (
    Action("serial.listen", Effect.SAFE, False, "passively reads console output",
           "listen to a serial port and save what it prints", _LISTEN),
    Action("serial.watch", Effect.SAFE, False, "keeps the port open and reads it continuously",
           "keep listening to a serial port in the background; then serial.listen reads from it without reopening, "
           "and alerts (crashes, resets) are noticed", (Param("baud", int, "baud rate", 115200, min=50, max=12_000_000),)),
    Action("serial.send", Effect.DISRUPTIVE, True, "sends bytes the firmware will act on",
           "send text to a serial port and read the answer",
           (Param("data", str, "text to send", required=True),
            Param("eol", str, "appended to data: \"\\n\" (default), \"\\r\\n\", or \"\" for single-byte commands", "\n"),
            Param("seconds", float, "how long to read the answer", 1.0, min=0.05, max=30),
            Param("baud", int, "baud rate", 115200, min=50, max=12_000_000))),
    Action("serial.command", Effect.DISRUPTIVE, True, "runs a remembered firmware command; its effect is what memory says",
           "run a firmware command remembered for this port (bench_remember kind command) by its name and read the "
           "answer; a command remembered as safe runs without asking once the human confirmed it",
           (Param("name", str, "the command as remembered, e.g. \"status\"", required=True),
            Param("seconds", float, "how long to read the answer", 1.0, min=0.05, max=30),
            Param("baud", int, "baud rate", 115200, min=50, max=12_000_000)), port_kinds=("serial",)),
    Action("jtag.scan", Effect.SAFE, False, "reads IDCODEs; needs exclusive use of the probe channel",
           "read the IDCODEs on a JTAG chain"),
    Action("jtag.halt", Effect.DISRUPTIVE, True, "stops the CPU", "halt the CPU over JTAG", implemented=False),
    # the command line decides the effect (policy.py): the standard's status and identity queries are SAFE, an output
    # setting asks every time, anything else asks and the human may allow that command for the session or always;
    # these base effects apply only with no command at all
    Action("scpi.query", Effect.DISRUPTIVE, False, "a SCPI query; what it does depends on the command and the instrument",
           "send a SCPI query and read the answer (one message, printable ASCII; *IDN?, SYST:ERR? and the other "
           "status queries run without asking; another query asks, and the human may allow it for the session or "
           "always on this instrument; `save` puts a screen capture or waveform in a file)", _QUERY),
    Action("scpi.write", Effect.DANGEROUS, False, "a SCPI command; what it does depends on the instrument",
           "send a SCPI command (one message, printable ASCII); it asks the human, who may allow that command for the "
           "session or always on this instrument; one that sets an output (voltage, current, output on) asks every "
           "time", _COMMAND),
)}


def from_data(e: dict) -> Action:
    """An external-tool action as written in packs/bench/data/tools.toml (or a user's own file)."""
    params = tuple(Param(n, _TYPES[p.get("type", "str")], p.get("doc", ""), p.get("default"), bool(p.get("required", False)),
                         p.get("min"), p.get("max"), tuple(p.get("ext", ()))) for n, p in e.get("args", {}).items())
    effect = Effect[e.get("effect", "dangerous").upper()]  # no declared effect -> DANGEROUS
    return Action(e["name"], effect, bool(e.get("disturbs", effect > Effect.SAFE)), e.get("reason", "external tool"),
                  e.get("doc", ""), params, True, tuple(e["argv"]), tuple(e.get("port_kinds", ())),
                  bool(e.get("releases_device", False)), float(e.get("timeout", 60)), dict(e.get("version", {})))


def all_actions() -> dict[str, Action]:
    from . import knowledge as K

    out = dict(BUILTIN)
    for e in K.TOOLS.values():
        out[e["name"]] = from_data(e)
    return out


def __getattr__(name: str):  # ACTIONS stays importable and always reflects the loaded data
    if name == "ACTIONS":
        return all_actions()
    raise AttributeError(name)


def get(name: str) -> Action:
    acts = all_actions()
    if name not in acts:
        raise ArgError(f"unknown action {name!r}; actions: {', '.join(acts)}")
    return acts[name]


def canonical(args: dict) -> str:
    """The arguments an approval is bound to, in one comparable form."""
    return json.dumps(args, sort_keys=True)


def catalog() -> str:
    """For tool descriptions: every action with its arguments."""
    lines = ["(The effect in brackets is the base one. The port can raise it, e.g. opening a port that "
             "resets the device; bench_impact gives the actual effect.)"]
    for a in all_actions().values():
        state = "" if a.implemented else " (not implemented yet)"
        params = f" args: {a.describe_params()}" if a.params else ""
        lines.append(f"- {a.name} [{a.effect.name}]{state}: {a.doc}.{params}")
    return "\n".join(lines)
