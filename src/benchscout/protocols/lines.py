"""A firmware's line shell over a serial port: one command per line, its words after the command as values."""

from __future__ import annotations

import re

from ..policy_types import Effect
from .base import Judged, Protocol, Values


def sent_lines(text: str) -> list[str]:
    """The lines a firmware will read from what is sent (data and its line ending together)."""
    return [line.strip() for line in re.split(r"[\r\n]+", text) if line.strip()]


def _values_after(words: list[str]) -> Values:
    return lambda name: words[len(name.split()):]


class Lines(Protocol):
    name = "lines"
    port_kinds = ("serial",)
    many_readings = False

    def commands(self, text: str) -> list[tuple[list[str], Values]]:
        out: list[tuple[list[str], Values]] = []
        for line in sent_lines(text):
            words = line.split()
            # "set 42" may be the remembered "set 42", or "set" with a value: the most specific remembered one is meant
            out.append(([" ".join(words[:n]) for n in range(len(words), 0, -1)], _values_after(words)))
        return out

    def default(self, name: str) -> Judged:
        return Effect.DISRUPTIVE, "sends bytes the firmware will act on", "action:serial.send"

    def key(self, command: str) -> str:
        return command.strip()

    probe_action = ("serial.listen", {})

    def probe(self, io, port, seconds: float) -> dict | None:
        # opened the way it was judged safe to open (the port's lines): real bench 2026-11-03, a probe that dropped
        # DTR/RTS reset an ESP32-S3 its memory said was safe to open with them left asserted
        try:
            return {"probe": "output", "result": io.serial_listen(port.path, seconds, 115200, False, port.open_lines)}
        except Exception as e:  # serial.SerialException / OSError: the device went away
            return {"probe": "output", "error": str(e)}

    def resolve(self, port, args: dict) -> dict:
        """A remembered command is sent with its own line ending and end line unless the call says otherwise."""
        lines = sent_lines(str(args.get("data", "")))
        rule = port.commands.get(lines[0]) if len(lines) == 1 else None
        out = dict(args)
        if out.get("eol") is None:
            out["eol"] = (rule or {}).get("eol", "\n")
        if out.get("until") is None and rule and rule.get("until"):
            out["until"] = rule["until"]
        return out


LINES = Lines()
