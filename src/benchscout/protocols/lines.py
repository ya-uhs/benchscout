"""A firmware's line shell over a serial port: one command per line, its words after the command as values."""

from __future__ import annotations

import re

from ..model import DEFAULT_BAUD
from ..policy_types import Effect
from .base import Context, Judged, Protocol, Values


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
            return {"probe": "output",
                    "result": io.serial_listen(port.path, seconds, port.baud or DEFAULT_BAUD, False, port.open_lines)}
        except Exception as e:  # serial.SerialException / OSError: the device went away
            return {"probe": "output", "error": str(e)}

    def resolve(self, port, args: dict) -> dict:
        """A remembered command is sent with its own line ending and end line unless the call says otherwise."""
        commands = self.commands(str(args.get("data", "")))
        rule = next((port.commands[n] for n in commands[0][0] if n in port.commands), None) if len(commands) == 1 else None
        out = dict(args)
        if out.get("eol") is None:
            out["eol"] = (rule or {}).get("eol", "\n")
        if out.get("until") is None and rule and rule.get("until"):
            out["until"] = rule["until"]
        return out


LINES = Lines()


# ---- operations over a serial port (the gate finds them by name; `ctx` is the broker, used only through what
# protocols.base.Context lists: watches, the bench, artifacts, what was printed and sent) ----------------------------

def _rewatch_at(ctx: Context, port, baud: int):
    """A call at another rate than the watch's: the watch reopens at it (the gate judged that reopen as an open)."""
    w = ctx.watches.get(port.id)
    if w and w.alive and baud != w.baud:
        ctx.unwatch_port(port.id)
        w = ctx.watches.get(port.id) if ctx.watch_port(port.id, baud) else None
    return w


def op_watch(ctx: Context, port, args: dict):
    w = ctx.watches.get(port.id)
    if w and w.alive:
        return {"watching": port.id, "log": str(w.log.resolve()), "already": True}
    ctx.unwatch_port(port.id)
    if not ctx.watch_port(port.id, args["baud"]):
        return {"error": "could not open the port to watch it"}
    return {"watching": port.id, "log": str(ctx.watches[port.id].log.resolve())}


def _gone(ctx: Context, port, error: str, doing: str) -> dict:
    """The port went away while this call had it open: said, folded in, and judged as what opening it did."""
    ctx.fold_output(port.id, None, error)
    ctx.observe_open(port.id, None)
    return {"error": f"port disappeared while {doing}; the device likely reset or re-enumerated", "detail": error}


def op_listen(ctx: Context, port, args: dict):
    w = _rewatch_at(ctx, port, args["baud"])
    if w and w.alive:  # held open: read what the watch hears
        text = w.read_window(args["seconds"], ctx.cancel_event)
        if ctx.cancel_event is not None and ctx.cancel_event.is_set():
            return {"cancelled": True, **ctx.save_artifact(f"{port.id}-listen-cancelled.txt", text or "",
                                                          preview=(text or "")[-2000:])}
        ctx.fold_output(port.id, text)
        return dict(ctx.save_artifact(f"{port.id}-listen-{args['baud']}.txt", text, preview=text[-2000:]),
                    watched_log=str(w.log.resolve()))
    try:
        text = ctx.io.serial_listen(port.path, args["seconds"], args["baud"], True, port.open_lines,
                                    cancel=ctx.cancel_event, may_reopen=ctx.read_reopen_guard(port))
    except Exception as e:  # serial.SerialException / OSError: it went away
        return _gone(ctx, port, str(e), "listening")
    if ctx.cancel_event is not None and ctx.cancel_event.is_set():
        return {"cancelled": True, **ctx.save_artifact(f"{port.id}-listen-cancelled.txt", text or "",
                                                      preview=(text or "")[-2000:])}
    ctx.fold_output(port.id, text)
    ctx.observe_open(port.id, text)
    return ctx.save_artifact(f"{port.id}-listen-{args['baud']}.txt", text, preview=text[-2000:])


def op_send(ctx: Context, port, args: dict):
    from ..codec import split, until_pattern

    w = _rewatch_at(ctx, port, args["baud"])
    until = until_pattern(args.get("until"))
    sent = args["data"] + (args["eol"] if args.get("eol") is not None else "\n")
    ctx.record_sent(port.id, args["data"])
    data = sent.encode()
    if w and w.alive:
        text, before = w.send(data, args["seconds"], until, ctx.cancel_event)
    else:  # opened for this call: nothing it printed before to tell its own lines by
        try:
            text, before = ctx.io.serial_listen(port.path, args["seconds"], args["baud"], True, port.open_lines,
                                                send=data, until=until, cancel=ctx.cancel_event,
                                                may_reopen=ctx.read_reopen_guard(port)), None
        except Exception as e:  # it went away: the open (or the command) reset it
            return _gone(ctx, port, str(e), "sending")
        ctx.observe_open(port.id, text)
    ctx.fold_output(port.id, text)
    safe_name = "".join(c if c.isalnum() else "_" for c in args["data"])[:24]
    saved = ctx.save_artifact(f"{port.id}-send-{safe_name}.txt", text, preview="")
    saved.pop("preview")
    parts = split(text, args["data"], before, until)
    for k in ("reply", "unsolicited"):
        if len(parts.get(k, "")) > 2000:
            parts[k] = "…" + parts[k][-2000:]
    return {**saved, **parts}


OPERATIONS = {"serial.watch": op_watch, "serial.listen": op_listen, "serial.send": op_send}
