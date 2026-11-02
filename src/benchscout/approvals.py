"""Approvals, held only in the core's memory (design.md section 15 R3 and R8).

- Only the control channel (the human's panel) calls `decide`. The agent's channel has
  no method that reaches it.
- DANGEROUS: granted once, bound to (action, port, args).
- DISRUPTIVE: may instead be granted for (action, port) for the session or N minutes.
- The audit log is append-only output. Nothing reads it back to decide anything, so
  editing it grants nothing.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .policy import Effect, Impact

Scope = str | int  # "once" | "session" | minutes


@dataclass
class Approval:
    id: str
    action: str
    port: str
    args: str  # canonical JSON of the action's arguments (actions.canonical)
    effect: str
    impact: dict
    status: str = "pending"  # pending | granted | denied | used | withdrawn
    scope: Scope = "once"
    created: float = field(default_factory=time.time)
    by: str = "agent"  # who asked: the agent, or benchscout itself (e.g. offering to watch a selected device)
    subject: str = ""  # the device's name, for the card
    purpose: str = ""  # what it is for, when benchscout asks itself
    fetch: str = ""  # a tool that will be downloaded first (official release, pinned), if any


@dataclass
class Grant:
    action: str
    port: str
    expires: float | None  # monotonic deadline; None = until the session ends


class ApprovalBook:
    def __init__(self, audit: Path | None = None, on_event: Callable[[dict], None] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self._audit = audit
        self._emit = on_event or (lambda e: None)
        self._clock = clock
        self._cond = threading.Condition()
        self._items: dict[str, Approval] = {}
        self._grants: list[Grant] = []

    # ---- agent side (via the broker) ------------------------------------------
    def open(self, impact: Impact, args: str, by: str = "agent", subject: str = "", purpose: str = "", fetch: str = "") -> Approval:
        a = Approval(uuid.uuid4().hex[:6], impact.action, impact.port_id, args, impact.effect.name.lower(), impact.to_dict(),
                     by=by, subject=subject, purpose=purpose, fetch=fetch)
        with self._cond:
            self._items[a.id] = a
        self._log("requested", a)
        self._emit({"event": "approval_requested", "approval": asdict(a)})
        return a

    def wait(self, approval_id: str, timeout: float) -> str:
        """Block until the human decides or `timeout` seconds pass. Returns the status."""
        deadline = self._clock() + max(timeout, 0)
        with self._cond:
            while True:
                a = self._items.get(approval_id)
                if a is None:
                    return "unknown_approval"
                if a.status != "pending":
                    return a.status
                left = deadline - self._clock()
                if left <= 0:
                    return "pending"
                self._cond.wait(min(left, 1.0))

    def consume(self, approval_id: str, action: str, port: str, args: str) -> str:
        """Use a granted approval for exactly this call. Returns "ok" or why not."""
        with self._cond:
            a = self._items.get(approval_id)
            if a is None:
                return "unknown_approval"
            if a.status != "granted":
                return a.status
            if (a.action, a.port, a.args) != (action, port, args):
                return "approval_mismatch"
            a.status = "used"
        self._log("used", a)
        return "ok"

    def asked_for(self, approval_id: str) -> tuple[str, str, str] | None:
        """(action, port, args) the approval was asked for, or None if there is no such approval."""
        with self._cond:
            a = self._items.get(approval_id)
            return (a.action, a.port, a.args) if a else None

    def covered(self, action: str, port: str, effect: Effect) -> bool:
        """A standing grant (R8) covers this DISRUPTIVE call."""
        if effect != Effect.DISRUPTIVE:
            return False
        now = self._clock()
        with self._cond:
            self._grants = [g for g in self._grants if g.expires is None or g.expires > now]
            return any(g.action == action and g.port == port for g in self._grants)

    # ---- human side (control channel only) --------------------------------------
    def decide(self, approval_id: str, granted: bool, scope: Scope = "once") -> Approval:
        with self._cond:
            a = self._items.get(approval_id)
            if a is None:
                waiting = [x.id for x in self._items.values() if x.status == "pending"]
                raise KeyError(f"no approval {approval_id!r}. " + (
                    f"Waiting: {', '.join(waiting)}" if waiting else
                    "Nothing is waiting; approvals appear here when the agent asks for something that is not SAFE."))
            if a.status != "pending":
                raise ValueError(f"{approval_id} is already {a.status}")
            if granted and scope != "once":
                if a.effect != Effect.DISRUPTIVE.name.lower():
                    raise ValueError(f"only DISRUPTIVE actions can be granted beyond one use; this one is {a.effect.upper()}")
                expires = None if scope == "session" else self._clock() + 60 * int(scope)
                self._grants.append(Grant(a.action, a.port, expires))
            a.status, a.scope = ("granted" if granted else "denied"), scope
            self._cond.notify_all()
        self._log(a.status, a)
        self._emit({"event": "approval_decided", "approval": asdict(a)})
        return a

    def pending(self) -> list[Approval]:
        with self._cond:
            return [a for a in self._items.values() if a.status == "pending"]

    def withdraw_ports(self, ports: set[str]) -> None:
        """Devices left the session: drop their standing grants and pending requests."""
        withdrawn = []
        with self._cond:
            self._grants = [g for g in self._grants if g.port not in ports]
            for a in self._items.values():
                if a.port in ports and a.status in ("pending", "granted"):
                    a.status = "withdrawn"
                    withdrawn.append(a)
            self._cond.notify_all()
        for a in withdrawn:  # so the panel closes their cards
            self._log("withdrawn", a)
            self._emit({"event": "approval_decided", "approval": asdict(a)})

    def end_session(self) -> None:
        with self._cond:
            self._grants.clear()

    def _log(self, what: str, a: Approval) -> None:
        if self._audit:
            rec = {"t": time.time(), "what": what, "id": a.id, "action": a.action, "port": a.port,
                   "args": a.args, "effect": a.effect, "scope": a.scope}
            with open(self._audit, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
