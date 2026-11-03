"""The agent proposes how to use a tool benchscout ships nothing for; the human confirms it (design.md section 4).

benchscout ships actions for a few open tools (openFPGALoader, esptool). An FPGA or MCU they do not support is
programmed with its vendor's own tool, which the agent finds (it reads the vendor's documentation; benchscout keeps
no list of vendors). The agent proposes the action: how the tool is started, what it does to the device (its
effect), the documentation that says so, and, for a programmer cable benchscout does not know, its USB id. The
proposal waits in the inbox showing the exact command; only the human's OK writes it into the folder's knowledge
(.benchscout/knowledge/agent-tools.toml), where it is an action like any other, under the same policy. Nothing is
downloaded for it: the person installs vendor tools.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .model import PORT_KINDS, PROBE_KINDS

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*\.[a-z0-9][a-z0-9_.-]*$")
FIXED = {"{port.path}", "{device.serial}", "{port.select*}"}
ARG_TYPES = ("str", "int", "float", "bool", "file", "flash_map")
FILE = "agent-tools.toml"


class ProposalError(ValueError):
    pass


@dataclass
class Proposal:
    id: str
    tool: dict
    probes: list[dict]
    evidence: str
    status: str = "pending"  # pending | accepted | rejected
    created: float = field(default_factory=time.time)


def check(tool: dict, probes: list[dict] | None, evidence: str, existing: dict[str, dict], builtin: set[str]) -> tuple[dict, list[dict]]:
    """A cleaned definition, or ProposalError saying what is wrong (the agent reads it and fixes the proposal)."""
    if not isinstance(tool, dict):
        raise ProposalError("tool must be an object: {name, argv, effect, reason, doc, args, port_kinds, ...}")
    allowed = {"name", "doc", "effect", "reason", "argv", "args", "port_kinds", "releases_device", "timeout"}
    if set(tool) - allowed:
        raise ProposalError(f"unknown fields {sorted(set(tool) - allowed)} (allowed: {sorted(allowed)}); a download "
                            "([get]) cannot be proposed: the person installs vendor tools")
    name = str(tool.get("name") or "")
    if not NAME.match(name):
        raise ProposalError("name: lowercase '<tool>.<what it does>', e.g. 'vendortool.program'")
    if name in builtin or name in existing and existing[name].get("defined_by") != "agent":
        raise ProposalError(f"{name} is already an action benchscout has; choose another name")
    effect = str(tool.get("effect") or "").lower()
    if effect not in ("safe", "disruptive", "dangerous"):
        raise ProposalError("effect: safe | disruptive | dangerous, judged by what it does to the device "
                            "(reads without touching: safe; resets or halts: disruptive; writes flash or SRAM: dangerous)")
    reason = str(tool.get("reason") or "").strip()
    if not reason:
        raise ProposalError("reason: what it does to the device, in one line (shown on the card and to the agent)")
    if not evidence or not str(evidence).strip():
        raise ProposalError("evidence: where the vendor documents this command and its effect (a link and a quote)")
    argv = tool.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
        raise ProposalError("argv: the command as a list of strings, no shell, e.g. [\"tool\", \"--cable\", \"{device.serial}\"]")
    if "{" in argv[0] or any(c in argv[0] for c in "|&;<>`$"):
        raise ProposalError("argv[0] is the program itself (a name on PATH or an absolute path), with no placeholders")
    args = tool.get("args") or {}
    if not isinstance(args, dict):
        raise ProposalError("args: an object {name: {type, required, doc, default, min, max, ext}}")
    for n, p in args.items():
        if not re.match(r"^[a-z][a-z0-9_]*$", n) or not isinstance(p, dict) or p.get("type", "str") not in ARG_TYPES:
            raise ProposalError(f"args.{n}: a lowercase name and an object with type one of {ARG_TYPES}")
    names = set(args)
    for a in argv[1:]:
        for ph in re.findall(r"\{[^}]*\}", a):
            if ph in FIXED or ph[1:-1].rstrip("*") in names:
                continue
            raise ProposalError(f"argv uses {ph}: only {sorted(FIXED)} and the declared args ({sorted(names)}) can be filled in")
    kinds = tool.get("port_kinds") or []
    if not isinstance(kinds, list) or not set(kinds) <= set(PORT_KINDS):
        raise ProposalError(f"port_kinds: a list of {PORT_KINDS}")
    timeout = tool.get("timeout", 60)
    if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 1800:
        raise ProposalError("timeout: seconds, 1 to 1800")
    clean = {k: v for k, v in tool.items() if v not in (None, "", [], {})}
    clean.update(name=name, effect=effect, reason=reason, defined_by="agent")
    out_probes = []
    for pr in probes or []:
        try:
            vid, pid = int(str(pr["vid"]), 0), int(str(pr["pid"]), 0)
        except (KeyError, TypeError, ValueError):
            raise ProposalError("probes: [{vid: \"0x1234\", pid: \"0x5678\", probe: \"jtag\" | \"swd\"}]") from None
        if not (0 < vid <= 0xFFFF and 0 < pid <= 0xFFFF) or pr.get("probe", PROBE_KINDS[0]) not in PROBE_KINDS:
            raise ProposalError("probes: vid and pid are 16-bit USB ids; probe is jtag or swd")
        out_probes.append({"vid": vid, "pid": pid, "probe": pr.get("probe", PROBE_KINDS[0])})
    return clean, out_probes


def _toml(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # a JSON string is a TOML basic string
    if isinstance(v, list):
        return "[" + ", ".join(_toml(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {_toml(x)}" for k, x in v.items()) + " }"
    raise TypeError(type(v))


def write(knowledge_dir: Path, p: Proposal, when: str) -> Path:
    """Append the confirmed proposal to agent-tools.toml (an earlier entry of the same name is replaced)."""
    import tomllib

    knowledge_dir.mkdir(parents=True, exist_ok=True)
    f = knowledge_dir / FILE
    old = tomllib.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
    tools = [t for t in old.get("tool", []) if t.get("name") != p.tool["name"]] + [
        dict(p.tool, source=f"proposed by the agent, confirmed by the human {when}: {p.evidence}")]
    usb = old.get("usb", []) + [{"vid": x["vid"], "pid": x["pid"], "role": "probe", "probe": x["probe"],
                                  "source": f"programmer for {p.tool['name']}, confirmed by the human {when}"}
                                 for x in p.probes if not any((u["vid"], u["pid"]) == (x["vid"], x["pid"]) for u in old.get("usb", []))]
    lines = ["# Actions the agent proposed and the human confirmed in the benchscout inbox. Edit or delete freely;",
             "# an action here is used under the same policy as any other (design.md section 4).", ""]
    for t in tools:
        lines.append("[[tool]]")
        lines += [f"{k} = {_toml(v)}" for k, v in t.items() if k != "args"]
        lines += [f"args.{n} = {_toml(a)}" for n, a in (t.get("args") or {}).items()]
        lines.append("")
    for u in usb:
        lines.append("[[usb]]")
        lines += [f"{k} = {_toml(v)}" for k, v in u.items()]
        lines.append("")
    f.write_text("\n".join(lines), encoding="utf-8")
    return f


class ProposalBook:
    """Proposals waiting for the human, like approvals: the agent's call waits a while, then can call again."""

    def __init__(self, emit):
        self._emit = emit
        self._cond = threading.Condition()
        self._items: dict[str, Proposal] = {}

    def open(self, tool: dict, probes: list[dict], evidence: str) -> Proposal:
        p = Proposal(uuid.uuid4().hex[:6], tool, probes, evidence)
        with self._cond:
            self._items[p.id] = p
        self._emit({"event": "tool_proposed", "proposal": asdict(p)})
        return p

    def get(self, pid: str) -> Proposal | None:
        with self._cond:
            return self._items.get(pid)

    def wait(self, pid: str, timeout: float) -> str:
        end = time.monotonic() + max(timeout, 0)
        with self._cond:
            while True:
                p = self._items.get(pid)
                if p is None:
                    return "unknown_proposal"
                if p.status != "pending" or time.monotonic() >= end:
                    return p.status
                self._cond.wait(min(end - time.monotonic(), 1.0))

    def decide(self, pid: str, accept: bool) -> Proposal:
        with self._cond:
            p = self._items.get(pid)
            if p is None:
                raise KeyError(f"no tool proposal {pid!r}")
            if p.status != "pending":
                raise ValueError(f"{pid} is already {p.status}")
            p.status = "accepted" if accept else "rejected"
            self._cond.notify_all()
        self._emit({"event": "tool_decided", "proposal": asdict(p)})
        return p
