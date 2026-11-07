"""What a protocol gives the gate, and how commands are judged (design.md section 3, 4; notes.md review 9).

A protocol says how text sent to a device is taken apart into commands (each with the names it may be known by and
its argument values), what a command nothing is remembered about is judged as, and how a remembered command is looked
up. The gate judges every protocol's commands the same way: by the rule remembered for the command (an effect, and
optionally limits on its values), else by the protocol's default; the strongest command decides. No module outside
`protocols/` names a protocol.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Callable, Protocol as _Interface

from ..policy_types import Effect

class Context(_Interface):
    """What a protocol's operations may use of the gate that runs them, and nothing else (tests/test_structure.py):
    the device's io, the bench, the watches held open, and the few things an operation does to the session."""

    io: Any
    watches: dict

    @property
    def bench(self) -> Any: ...

    @property
    def cancel_event(self) -> Any: ...  # set when the human or the agent stops the call

    def read_reopen_guard(self, port) -> Callable[[float], bool]: ...  # asked before a port that went away is reopened
    def new_artifact(self, stem: str, ext: str) -> Path: ...
    def save_artifact(self, name: str, content: str, preview: str) -> dict: ...
    def watch_port(self, port_id: str, baud: int) -> bool: ...
    def unwatch_port(self, port_id: str) -> None: ...
    def fold_output(self, port_id: str, text: str | None, error: str | None = None) -> None: ...
    def observe_open(self, port_id: str, text: str | None) -> None: ...  # what opening the port did (it stayed, it reset)
    def record_sent(self, port_id: str, text: str) -> None: ...  # what was sent: a device echoing it did not say it


Values = Callable[[str], list[str]]  # a command's argument values, under one of its names
Judged = tuple[Effect, str, str]  # (effect, reason, code)


class Protocol:
    name = ""
    port_kinds: tuple[str, ...] = ()
    # every name a command may be read by counts (the strictest wins), not only the most specific remembered one
    many_readings = False

    def check(self, text: str) -> None:
        """Raise ArgError for text this protocol cannot carry as asked (it would be read as something else)."""

    def commands(self, text: str) -> list[tuple[list[str], Values]]:
        raise NotImplementedError

    def default(self, name: str) -> Judged:
        """A command nothing is remembered about."""
        raise NotImplementedError

    def key(self, command: str) -> str:
        """How a remembered command is looked up ("" if it is not one command)."""
        raise NotImplementedError

    # the SAFE call recon makes to hear what a device is (an action and its arguments), and making it
    probe_action: tuple[str, dict] | None = None
    # the action that holds a port open and reads it continuously (None: this protocol's ports are not watched)
    watch_action: str | None = None

    def enumerate(self, io, bench, names: dict) -> None:
        """Add the devices and ports this protocol is reached through, that the USB and serial lists do not show."""

    def probe(self, io, port, seconds: float) -> dict | None:
        """{"probe": "output", "result" | "error"} (what it printed) or {"probe": "identity", "result", "maker", "model",
        "serial", "source"} (what it says it is)."""
        return None

    def resolve(self, port, args: dict) -> dict:
        """The call's arguments with what the remembered rule supplies (a line ending), resolved before it is asked
        for: the approval binds to what will actually be sent."""
        return args


def _number(text: str) -> float | None:
    """A plain number (3.3, -1, 2e-3). A value with a unit suffix or a keyword (MAX, DEF) is not one: it asks."""
    if not re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", text.strip()):
        return None
    value = float(text)
    return value if math.isfinite(value) else None


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


def values_to_take(text: str, proto: Protocol) -> dict[str, int]:
    """For each name the commands in `text` may be known by, how many values a rule remembered for it must take (one
    item each in its limits, None for any value) to cover `text`: `judge` below reads the words after a remembered
    command as its values only up to its limits where a protocol has one reading; with many readings (a header and its
    parameters) a rule takes any values already."""
    if proto.many_readings:
        return {}
    counts: dict[str, int] = {}
    for names, values in proto.commands(text):
        for n in names:
            counts[n] = max(counts.get(n, 0), len(values(n)))
    return counts


def judge_rule(rule: dict, name: str, args: list[str]) -> Judged:
    """One command under the rule remembered for it."""
    effect = Effect[rule["effect"].upper()]
    why = outside(rule.get("limits") or [], args)
    if why:
        return (Effect.DANGEROUS, f"`{name}`: {why}, the limits remembered for it; outside them it asks every time",
                "outside_limits")
    reason = (f"`{name}` is remembered as {rule['effect'].upper()}" + (" (confirmed by the human)" if rule.get("loosens") else "")
              + (f": {rule['text']}" if rule.get("text") else "") + (" (within its value limits)" if rule.get("limits") else ""))
    return effect, reason, f"command_{rule['effect']}"


def judge(port, text: str, proto: Protocol) -> tuple[Effect, str, str, list[str]]:
    """The strongest of the commands in `text`, and the names a standing grant for it is bound to."""

    def rule(name: str) -> dict | None:
        r = port.commands.get(name)
        return r if r and not r.get("pending") else None

    worst: Judged = (Effect.SAFE, "", "")
    subjects: set[str] = set()
    for names, values in proto.commands(text):
        if proto.many_readings:  # the device may read it any of these ways: the strictest counts
            judged = max((judge_rule(r, n, values(n)) if (r := rule(n)) else proto.default(n) for n in names),
                         key=lambda j: j[0])
            subjects |= set(names)
        else:  # the most specific remembered form is the command meant; else the command word alone
            name = next((n for n in names if rule(n)), None)
            if name is None:
                judged = proto.default(names[-1])
                subjects.add(names[-1])
            else:
                r = rule(name)
                assert r is not None
                judged = judge_rule(r, name, values(name))
                if len(values(name)) > len(r.get("limits") or []):
                    # words after it that its rule does not take as values: what the human confirmed was the command,
                    # not those words, so it asks no less than an unknown command would (and a stronger rule stays)
                    judged = max(judged, proto.default(names[-1]), key=lambda j: j[0])
                subjects.add(name)
        if judged[0] > worst[0] or not worst[1]:
            worst = judged
    return (*worst, sorted(subjects))
