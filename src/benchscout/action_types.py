"""What an action is: its typed arguments and effect, and how one defined as data (tools.toml, a person's own file)
becomes one. Below the knowledge loader, which checks each tool entry with `from_data` when it reads a file; the registry
of the actions that exist (actions.py) is above it, built from the built-ins and what was loaded (audit 2026-11-07,
Codex's finding 8: the two imported each other).
"""

from __future__ import annotations

import math
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
        if isinstance(v, float) and not math.isfinite(v):
            raise ArgError(f"{self.name} must be a finite number")
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
    # defined as data (design.md section 4): an external tool run with these arguments, no shell
    argv: tuple[str, ...] = ()
    port_kinds: tuple[str, ...] = ()  # which kinds of port it applies to ("" = any)
    releases_device: bool = False  # the tool opens the device itself: stop watching it meanwhile
    timeout: float = 60.0
    version: dict = field(default_factory=dict)  # {"argv": [...], "min": "5.0", "why": "..."}
    # what it sends, judged by the protocol it declares (protocols/): the protocol's name and the argument with the text
    protocol: str = ""
    text: str = ""
    opens: bool = False  # it opens the port unless the port is held open already: opening is a step of the call
    probe: dict = field(default_factory=dict)  # recon runs it by itself (SAFE), reading its output this way (tools.toml)
    # a DANGEROUS write the human may allow for the session on one device (rewriting a dev board while iterating), as
    # the data says; without it, once only (design.md section 12, 2026-11-08)
    allow_session: bool = False

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


def from_data(e: dict) -> Action:
    """An external-tool action as written in packs/bench/data/tools.toml (or a user's own file)."""
    params = tuple(Param(n, _TYPES[p.get("type", "str")], p.get("doc", ""), p.get("default"), bool(p.get("required", False)),
                         p.get("min"), p.get("max"), tuple(p.get("ext", ()))) for n, p in e.get("args", {}).items())
    effect = Effect[e.get("effect", "dangerous").upper()]  # no declared effect -> DANGEROUS
    return Action(e["name"], effect, bool(e.get("disturbs", effect > Effect.SAFE)), e.get("reason", "external tool"),
                  e.get("doc", ""), params, tuple(e["argv"]), tuple(e.get("port_kinds", ())),
                  bool(e.get("releases_device", False)), float(e.get("timeout", 60)), dict(e.get("version", {})), probe=dict(e.get("probe", {})),
                  allow_session=bool(e.get("allow_session", False)))
