"""Approvals, held only in the core's memory (docs/notes.md review R3 and R8).

- Only the control channel (the human's panel) calls `decide`. The agent's channel has
  no method that reaches it.
- DANGEROUS: granted once, bound to (action, port, args); or, where the broker offers it on the card, for the session
  within what it says (design.md section 12, 2026-11-08): a program while its content is unchanged, a write with the
  same tool on the same device, a command up to the values the human approved.
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
from .protocols import outside

Scope = str | int  # "once" | "session" | "all" | minutes ("always" is the broker's: once, plus memory)


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
    # "always on this device" can be kept: the commands asked about can be remembered as this unit's (the broker says)
    always: bool = False
    model: bool = False  # ... and it would be kept for the model: every unit carries the same serial, this one is alone
    # a DANGEROUS call the human may allow for the session, and what that covers: "program" (this content), "tool" (this
    # tool on this device, any file), "bounded" (these commands up to these values); "" = once only
    again: str = ""


@dataclass
class Grant:
    action: str
    port: str
    expires: float | None  # monotonic deadline; None = until the session ends
    subjects: frozenset[str] | None = None  # only calls about these (SCPI headers); None = any call of the action
    effect: Effect = Effect.DISRUPTIVE  # the strongest call it covers
    content: str | None = None  # only calls with this content (a program's plan, a tool's identity); None = any
    bounds: dict | None = None  # {subject: limits (protocols.base.outside)}: only values within them


class ApprovalBook:
    def __init__(self, audit: Path | None = None, on_event: Callable[[dict], None] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self._audit = audit
        self._emit = on_event or (lambda e: None)
        self._clock = clock
        self._cond = threading.Condition()
        self._items: dict[str, Approval] = {}
        self._grants: list[Grant] = []
        self._again: dict[str, dict] = {}  # approval id -> what "for the session" would grant (the broker's offer)
        # "allow everything for this session": every DISRUPTIVE call runs without asking until the human turns it
        # off or benchscout stops. DANGEROUS calls still ask each time (R8).
        self.allow_all = False

    # ---- agent side (via the broker) ------------------------------------------
    def open(self, impact: Impact, args: str, by: str = "agent", subject: str = "", purpose: str = "", fetch: str = "",
             always: bool = False, again: tuple[str, dict] | None = None, model: bool = False) -> Approval:
        a = Approval(uuid.uuid4().hex[:6], impact.action, impact.port_id, args, impact.effect.name.lower(), impact.to_dict(),
                     by=by, subject=subject, purpose=purpose, fetch=fetch, always=always, model=always and model,
                     again=again[0] if again and impact.effect == Effect.DANGEROUS else "")
        with self._cond:
            self._items[a.id] = a
            if a.again and again:
                self._again[a.id] = again[1]
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

    def consume(self, approval_id: str, action: str, port: str, args: str, effect: str | None = None) -> str:
        """Use a granted approval for exactly this call: the same action, port and resolved arguments, judged as it was
        when the human decided (`effect`: what it is judged now; a memory changed meanwhile asks again). Returns "ok"
        or why not."""
        with self._cond:
            a = self._items.get(approval_id)
            if a is None:
                return "unknown_approval"
            if a.status != "granted":
                return a.status
            if (a.action, a.port, a.args) != (action, port, args) or effect is not None and effect != a.effect:
                return "approval_mismatch"
            a.status = "used"
        self._log("used", a)
        return "ok"

    def get(self, approval_id: str) -> Approval | None:
        with self._cond:
            return self._items.get(approval_id)

    def asked_for(self, approval_id: str) -> tuple[str, str, str] | None:
        """(action, port, args) the approval was asked for, or None if there is no such approval."""
        with self._cond:
            a = self._items.get(approval_id)
            return (a.action, a.port, a.args) if a else None

    def covered(self, action: str, port: str, effect: Effect, subjects: list[str] | None = None, content: str | None = None,
                values: dict[str, list[list[str]]] | None = None) -> bool:
        """A standing grant (R8) covers this call: same action and port, no stronger than it was given for, and within
        what it was given for: the subjects (SCPI headers), the content (a program, a tool), the values (each time a
        subject comes in the call). "Allow all" covers DISRUPTIVE calls only."""
        if effect not in (Effect.DISRUPTIVE, Effect.DANGEROUS):
            return False
        now = self._clock()
        with self._cond:
            if self.allow_all and effect == Effect.DISRUPTIVE:
                return True
            self._grants = [g for g in self._grants if g.expires is None or g.expires > now]
            return any(self._covers(g, action, port, effect, subjects, content, values) for g in self._grants)

    @staticmethod
    def _covers(g: Grant, action: str, port: str, effect: Effect, subjects, content, values) -> bool:
        if g.action != action or g.port != port or effect > g.effect:
            return False
        if g.subjects is not None and (subjects is None or not set(subjects) <= g.subjects):
            return False
        if g.content is not None and content != g.content:
            return False
        if g.bounds is not None:
            for s in subjects or []:
                limits = g.bounds.get(s)
                for vals in (values or {}).get(s) or [None]:
                    if limits is None or vals is None or len(vals) > len(limits) or outside(limits, vals):
                        return False
        return True

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
            dangerous = a.effect == Effect.DANGEROUS.name.lower()
            if granted and scope != "once":
                if a.effect != Effect.DISRUPTIVE.name.lower() and not (dangerous and scope == "session" and a.again):
                    raise ValueError(f"{a.action} is {a.effect.upper()}: it can be allowed once"
                                     + (" or, as its card offers, for the session" if a.again else ""))
            if granted and scope == "all":
                pass  # this call, and set_allow_all below
            elif granted and scope != "once" and dangerous:
                offer = self._again.get(a.id, {})  # what the card said "for the session" covers, and nothing more
                self._grants.append(Grant(a.action, a.port, None,
                                          frozenset(offer["subjects"]) if offer.get("subjects") is not None else None,
                                          Effect.DANGEROUS, offer.get("content"), offer.get("bounds")))
            elif granted and scope != "once":
                expires = None if scope == "session" else self._clock() + 60 * int(scope)
                subjects = a.impact.get("subjects")
                self._grants.append(Grant(a.action, a.port, expires, frozenset(subjects) if subjects is not None else None))
            a.status, a.scope = ("granted" if granted else "denied"), scope
            self._cond.notify_all()
        self._log(a.status, a)
        self._emit({"event": "approval_decided", "approval": asdict(a)})
        if granted and scope == "all":
            self.set_allow_all(True)
        return a

    def set_allow_all(self, on: bool) -> None:
        """Human side: allow every DISRUPTIVE call for the rest of the session, or stop doing so. Turning it on
        also lets the agent's DISRUPTIVE calls already waiting on a card go ahead."""
        with self._cond:
            changed = self.allow_all != on
            self.allow_all = on
            waiting = [a for a in self._items.values()
                       if on and a.status == "pending" and a.by == "agent" and a.effect == Effect.DISRUPTIVE.name.lower()]
            for a in waiting:
                a.status, a.scope = "granted", "all"
            self._cond.notify_all()
        for a in waiting:
            self._log("granted", a)
            self._emit({"event": "approval_decided", "approval": asdict(a)})
        if changed:
            if self._audit:  # the audit log says when the human allowed everything, and when they stopped
                with open(self._audit, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"t": time.time(), "what": "allow_all", "on": on}) + "\n")
            self._emit({"event": "allow_all", "on": on})

    def pending(self) -> list[Approval]:
        with self._cond:
            return [a for a in self._items.values() if a.status == "pending"]

    def withdraw_ports(self, ports: set[str]) -> None:
        """Devices left the session: drop their standing grants and pending requests."""
        withdrawn = []
        with self._cond:
            self._grants = [g for g in self._grants if g.port not in ports]
            for a in self._items.values():
                if (a.port in ports or ports.intersection(a.impact.get("affected_ports", []))) and a.status in ("pending", "granted"):
                    a.status = "withdrawn"
                    withdrawn.append(a)
            self._cond.notify_all()
        for a in withdrawn:  # so the panel closes their cards
            self._log("withdrawn", a)
            self._emit({"event": "approval_decided", "approval": asdict(a)})

    def end_session(self) -> None:
        with self._cond:
            self._grants.clear()
            self._again.clear()

    def _log(self, what: str, a: Approval) -> None:
        if self._audit:
            rec = {"t": time.time(), "what": what, "id": a.id, "action": a.action, "port": a.port,
                   "args": a.args, "effect": a.effect, "scope": a.scope}
            with open(self._audit, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
