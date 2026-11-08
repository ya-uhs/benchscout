"""Action registry: every action the agent can ask for, with its effect and typed arguments.

One place says what an action is, what it does to device state, and what each argument
means, so the policy, the broker and the MCP tool descriptions cannot drift apart
(design.md section 5 and 10: `serial.listen arg=9600` was read as 9600 seconds).
"""

from __future__ import annotations

import json

from .action_types import _TYPES, Action, ArgError, FilePath, FlashMap, Param, from_data  # noqa: F401 (their home; re-exported)
from .model import DEFAULT_BAUD
from .policy_types import Effect


_LISTEN = (
    Param("seconds", float, "how long to listen", 3.0, min=0.05, max=60),
    Param("baud", int, "baud rate", DEFAULT_BAUD, min=50, max=12_000_000),
)
_COMMAND = (Param("command", str, "SCPI command line", required=True),)
SAVE_KINDS = ("png", "bmp", "jpg", "csv", "txt", "bin")
# a screen capture or waveform: the answer goes to a file in the workspace (an IEEE 488.2 block header is taken off).
# Which command gives it is the instrument's (its programming manual); benchscout keeps no list of them.
_QUERY = _COMMAND + (Param("save", str, f"save the answer to a file instead of returning it: one of {', '.join(SAVE_KINDS)} "
                                        "(e.g. a scope's screen as png, its waveform as csv)"),)

BUILTIN: dict[str, Action] = {a.name: a for a in (
    Action("serial.listen", Effect.SAFE, False, "passively reads console output",
           "listen to a serial port and save what it prints", _LISTEN, opens=True),
    Action("serial.watch", Effect.SAFE, False, "keeps the port open and reads it continuously",
           "keep listening to a serial port in the background; then serial.listen reads from it without reopening, "
           "and alerts (crashes, resets) are noticed", (Param("baud", int, "baud rate", DEFAULT_BAUD, min=50, max=12_000_000),),
           opens=True),
    Action("serial.send", Effect.DISRUPTIVE, True, "sends bytes the firmware will act on",
           "send text to a serial port and read the answer; a command remembered for the port is judged by its rule "
           "(effect, value limits) and sent with its own line ending and end line",
           (Param("data", str, "text to send", required=True),
            Param("eol", str, "appended to data: \"\\n\", \"\\r\\n\", or \"\" for single-byte commands (default: the "
                  "remembered command's, else \"\\n\")"),
            Param("seconds", float, "how long to read the answer (at most, with until)", 1.0, min=0.05, max=30),
            Param("baud", int, "baud rate", DEFAULT_BAUD, min=50, max=12_000_000),
            Param("until", str, "regular expression for the line that ends the answer (the shell's prompt, \"^(ok|err)\", "
                  "\"^(OK|ERROR)$\"): reading stops there instead of after seconds; read it from the device's output")),
           protocol="lines", text="data", opens=True),
    # the command line decides the effect (protocols/scpi.py): the standard's status and identity queries that change
    # nothing are SAFE (not the error queue or the event register: reading them clears them), an output setting asks (the
    # human may allow it for the session up to the value on the card), anything else asks and the human may allow that
    # command for the session or always; these base effects apply only with no command at all
    Action("scpi.query", Effect.DISRUPTIVE, False, "a SCPI query; what it does depends on the command and the instrument",
           "send a SCPI query and read the answer (one message, printable ASCII; *IDN?, *STB?, SYST:ERR:COUN? and the "
           "other status queries that change nothing run without asking; SYST:ERR? and *ESR? ask, since reading them "
           "clears them; another query asks, and the human may allow it for the session or always on this "
           "instrument; `save` puts a screen capture or waveform in a file)", _QUERY,
           protocol="scpi", text="command"),
    Action("scpi.write", Effect.DANGEROUS, False, "a SCPI command; what it does depends on the instrument",
           "send a SCPI command (one message, printable ASCII); it asks the human, who may allow that command for the "
           "session or always on this instrument; one that sets an output (voltage, current, output on) asks, and the "
           "human may allow it for the session up to the value asked (ask for the largest value of a sweep first)",
           _COMMAND, protocol="scpi", text="command"),
)}


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
        params = f" args: {a.describe_params()}" if a.params else ""
        lines.append(f"- {a.name} [{a.effect.name}]: {a.doc}.{params}")
    return "\n".join(lines)
