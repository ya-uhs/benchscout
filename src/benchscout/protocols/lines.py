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
    watch_action = "serial.watch"

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


# ---- operations over a serial port (the gate finds them by name; `ctx` is the broker: watches, bench, artifacts) ----

def _rewatch_at(ctx, port, baud: int):
    """A call at another rate than the watch's: the watch reopens at it (the gate judged that reopen as an open)."""
    w = ctx.watches.get(port.id)
    if w and w.alive and baud != w.baud:
        ctx._unwatch(port.id)
        w = ctx.watches.get(port.id) if ctx._watch(port.id, baud) else None
    return w


def op_watch(ctx, port, args: dict):
    w = ctx.watches.get(port.id)
    if w and w.alive:
        return {"watching": port.id, "log": str(w.log.resolve()), "already": True}
    ctx._unwatch(port.id)
    if not ctx._watch(port.id, args["baud"]):
        return {"error": "could not open the port to watch it"}
    return {"watching": port.id, "log": str(ctx.watches[port.id].log.resolve())}


def op_listen(ctx, port, args: dict):
    from .. import recon as R

    w = _rewatch_at(ctx, port, args["baud"])
    learned = ctx.bench.devices[port.device_id].line_rules
    if w and w.alive:  # held open: read what the watch hears
        text = w.read_window(args["seconds"], ctx.cancel_event)
        if ctx.cancel_event is not None and ctx.cancel_event.is_set():
            return {"cancelled": True, **ctx._save_artifact(f"{port.id}-listen-cancelled.txt", text or "",
                                                           preview=(text or "")[-2000:])}
        R.apply_log(ctx.bench.targets_via(port.id), text, learned=learned)
        ctx.recon.proposals = R.propose(ctx.bench)
        ctx._remember(port.id, text)
        return dict(ctx._save_artifact(f"{port.id}-listen-{args['baud']}.txt", text, preview=text[-2000:]),
                    watched_log=str(w.log.resolve()))
    text = R.listen_into(ctx.io, port.path, ctx.bench.targets_via(port.id), args["seconds"],
                         exclusive=True, baud=args["baud"], lines=port.open_lines, cancel=ctx.cancel_event)
    if ctx.cancel_event is not None and ctx.cancel_event.is_set():
        return {"cancelled": True, **ctx._save_artifact(f"{port.id}-listen-cancelled.txt", text or "",
                                                           preview=(text or "")[-2000:])}
    ctx.recon.proposals = R.propose(ctx.bench)  # what was heard may change the next steps
    ctx._remember(port.id, text)
    ctx._observe_open(port.id, text)
    if text is None:
        detail = next((f for t in ctx.bench.targets_via(port.id) for f in reversed(t.facts) if "port disappeared" in f), "")
        return {"error": "port disappeared while listening; the device likely reset or re-enumerated", "detail": detail}
    return ctx._save_artifact(f"{port.id}-listen-{args['baud']}.txt", text, preview=text[-2000:])


def op_send(ctx, port, args: dict):
    from .. import recon as R
    from ..codec import split, until_pattern

    w = _rewatch_at(ctx, port, args["baud"])
    until = until_pattern(args.get("until"))
    data = (args["data"] + (args["eol"] if args.get("eol") is not None else "\n")).encode()
    if w and w.alive:
        text, before = w.send(data, args["seconds"], until, ctx.cancel_event)
    else:  # opened for this call: nothing it printed before to tell its own lines by
        text, before = ctx.io.serial_listen(port.path, args["seconds"], args["baud"], True, port.open_lines,
                                            send=data, until=until, cancel=ctx.cancel_event), None
    R.apply_log(ctx.bench.targets_via(port.id), text, learned=ctx.bench.devices[port.device_id].line_rules)
    ctx.recon.proposals = R.propose(ctx.bench)
    ctx._remember(port.id, text)
    safe_name = "".join(c if c.isalnum() else "_" for c in args["data"])[:24]
    saved = ctx._save_artifact(f"{port.id}-send-{safe_name}.txt", text, preview="")
    saved.pop("preview")
    parts = split(text, args["data"], before, until)
    for k in ("reply", "unsolicited"):
        if len(parts.get(k, "")) > 2000:
            parts[k] = "…" + parts[k][-2000:]
    return {**saved, **parts}


OPERATIONS = {"serial.watch": op_watch, "serial.listen": op_listen, "serial.send": op_send}
