"""A line shell's answer, told apart from what the device prints by itself (design.md section 4, review R7).

An MCU's shell keeps logging while it answers, so the text that arrives after a command is the answer mixed with
log lines. Nothing here knows any firmware; it works from what the device itself printed:

- the line the device echoed back (the sent text, maybe after a prompt) is not part of the answer;
- `until`, a pattern for the line that ends an answer (a prompt, "ok", "OK|ERROR"), stops the read as soon as it
  arrives instead of after a fixed time. Which line that is, the agent reads from the device's output or its help
  and remembers with the command;
- a line shaped like the lines the device printed by itself before the command (numbers ignored: "hb n=28
  uptime_ms=491535" is "hb n=# uptime_ms=#") is listed apart as unsolicited. That is a guess from the device's own
  output, so nothing is dropped: both lists are returned and the whole text is saved.
"""

from __future__ import annotations

import re

_NUMBER = re.compile(r"\b(?:0x)?[0-9A-Fa-f]*\d[0-9A-Fa-f]*\b")
MAX_UNTIL = 200


def shape(line: str) -> str:
    """The line with its numbers (decimal or hex, with or without 0x) replaced by '#'."""
    return _NUMBER.sub("#", line.strip())


def until_pattern(text: str | None) -> re.Pattern | None:
    """The agent's pattern for the line that ends an answer; ValueError says what is wrong with it."""
    if not text:
        return None
    if len(text) > MAX_UNTIL:
        raise ValueError(f"until: at most {MAX_UNTIL} characters")
    try:
        return re.compile(text)
    except re.error as e:
        raise ValueError(f"until is not a regular expression: {e}") from None


QUIET_S = 0.15  # a shell prompt has no newline after it: the last line is judged once nothing more has come this long


def ended(data: bytes, until: re.Pattern | None, settled: bool = False) -> bool:
    """A whole line of `data` matches `until`; the line still arriving too once the output has `settled` (a prompt
    is printed without a newline and then the shell waits)."""
    if until is None:
        return False
    lines = data.decode("utf-8", errors="replace").replace("\r", "").split("\n")
    return any(until.search(line) for line in (lines if settled else lines[:-1]) if line.strip())


def split(text: str, sent: str, before: str | None, until: re.Pattern | None) -> dict:
    """The answer to `sent`, the lines the device printed by itself meanwhile, and how the read ended.
    `before`: what the device printed by itself before the command (None when it was not watched: nothing to compare)."""
    lines = [l for l in text.replace("\r", "").split("\n") if l.strip()]
    out: dict = {}
    cmd = sent.strip()
    if cmd and lines and lines[0].strip().endswith(cmd) and len(lines[0].strip()) <= len(cmd) + 40:
        out["echo"] = lines.pop(0).strip()  # the shell echoed the command (maybe after its prompt)
    end = None
    if until is not None:
        end = next((i for i, l in enumerate(lines) if until.search(l)), None)
        out["ended"] = f"at the line matching until: {lines[end].strip()!r}" if end is not None else \
            "after the time given: no line matched until (the answer may be cut short, or until is wrong)"
    after = []
    if until is not None and end is not None:  # what came after the answer ended is not the answer
        lines, after = lines[:end + 1], lines[end + 1:]
    known = {shape(l) for l in (before or "").replace("\r", "").split("\n") if l.strip()}
    reply = [l for l in lines if shape(l) not in known]
    other = [l for l in lines if shape(l) in known] + after
    out["reply"] = "\n".join(reply)
    if other:
        out["unsolicited"] = "\n".join(other)
        out["how"] = ("lines shaped like what the device printed by itself before the command (numbers ignored) are "
                      "listed as unsolicited; the saved file has everything in order")
    elif before is None:
        out["how"] = "the port was not watched before the command, so its own lines could not be told apart"
    return out
