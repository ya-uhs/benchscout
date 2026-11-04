"""L2 broker: owns ports, enforces policy, holds approvals and the session's selection.

Two kinds of caller, two sets of methods (design.md section 3 and 15 R3):

- the agent (through the MCP relay): `scan`, `impact`, `request`
- the human (through the control channel only): `select`, `decide`

Approvals live in memory (`ApprovalBook`); nothing on disk grants anything.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Callable

from . import actions as A
from . import recon as R
from .approvals import ApprovalBook, Scope
from .io import Io, run_tool
from .locks import DeviceLocks
from . import protocols
from .policy import Effect, Impact, classify

DEFAULT_BAUD = 115200  # what a watch opens a port at when no rate was learned for it
LINE_ENDINGS = ("\n", "\r\n", "\r", "")
OPEN_RESET_S = 3.0  # a port that vanishes this soon after being opened was reset (or re-enumerated) by the open
RECENT_FOR_QUOTES = 2 * 1024 * 1024  # how much recent output a quote is checked against


def _tail(path: Path, nbytes: int) -> str:
    """The last `nbytes` of a file, without reading the rest (a watched log grows for as long as it is watched)."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - nbytes))
        return f.read().decode("utf-8", errors="replace")


class _Job:
    """An action running on a device, outside the broker's lock."""

    def __init__(self, action: str, port: str, extra: dict):
        import uuid

        self.id, self.action, self.port, self.extra = uuid.uuid4().hex[:6], action, port, extra
        self.t0 = time.monotonic()
        self.done = threading.Event()
        self.result: dict | None = None
        self.error: Exception | None = None


class _Download:
    """One tool's first-use download, in the background from the human's OK on, reported to the inbox as it goes
    (design.md section 12, 2026-11-02). A call waits a while for it, then says it is still downloading."""

    def __init__(self, exe: str, emit):
        from . import fetch

        p = fetch.plan(exe)
        self.exe, self.version, self.emit = exe, p["version"] if p else "", emit
        self.done, self.error, self.got, self._last = threading.Event(), "", 0, 0.0
        self.total: int | None = None
        threading.Thread(target=self._run, daemon=True).start()

    def _event(self, state: str) -> None:
        self.emit({"event": "download", "tool": self.exe, "version": self.version, "got": self.got, "total": self.total,
                   "state": state, **({"error": self.error} if self.error else {})})

    def _progress(self, got: int, total: int | None) -> None:
        self.got, self.total = got, total
        now = time.monotonic()
        if now - self._last >= 0.5:
            self._last = now
            self._event("running")

    def _run(self) -> None:
        from . import fetch

        self._event("running")
        try:
            fetch.get(self.exe, progress=self._progress)
        except Exception as e:  # anything: the call that waits must hear why
            self.error = str(e) or type(e).__name__
            self._event("failed")
        else:
            self._event("done")
        finally:
            self.done.set()

    def summary(self) -> str:
        mb = lambda n: f"{n / 1e6:.0f}"  # noqa: E731
        return f"{self.exe} {self.version}: {mb(self.got)}" + (f" of {mb(self.total)} MB" if self.total else " MB so far")


class Broker:
    def __init__(self, io: Io, state_dir: str | Path = ".benchscout", *, locks: DeviceLocks | None = None,
                 on_event: Callable[[dict], None] | None = None, approval_wait: float = 0.0,
                 workspace_knowledge: bool = True, download_wait: float = 45.0):
        self.io = io
        # how long a call waits for a tool's first-use download before saying it is still downloading; with the
        # approval wait it stays inside the relay's time for one call
        self.download_wait = download_wait
        self._downloads: dict[str, _Download] = {}
        self.state = Path(state_dir)
        self.state.mkdir(parents=True, exist_ok=True)
        from . import knowledge as K

        # shipped + user + this workspace's .benchscout/knowledge/ (not for a folder VS Code does not trust:
        # a project from elsewhere could declare its own actions safe)
        K.load(self.state.parent if workspace_knowledge else None)
        self.artifacts = self.state / "artifacts"
        self.artifacts.mkdir(exist_ok=True)
        self.locks = locks
        self.on_event = on_event  # set by the core to forward events to the control channel
        self.approval_wait = approval_wait  # seconds a request waits for the human (S3)
        self.approvals = ApprovalBook(self.state / "audit.jsonl", on_event=self._emit)
        from .questions import QuestionBook

        self.questions = QuestionBook(on_event=self._emit)
        from .proposals import ProposalBook

        self.proposals = ProposalBook(self._emit)  # tools the agent proposes, waiting for the human (proposals.py)
        self.workspace_knowledge = workspace_knowledge
        self.questions.on_answer = self._on_answer
        self.selected: set[str] = set()  # device ids in this session
        self.watches: dict = {}  # port id -> PortWatch (selected devices' ports held open)
        self.watch_offers: dict[str, str] = {}  # port id -> approval id: benchscout asked the human to watch it
        self.watch_declined: set[str] = set()  # ports the human said not to watch, this session
        self.watch_approved: set[str] = set()  # ports the human allowed to be watched although opening may reset them
        self._pending_shown: set[str] = set()  # memory entries waiting for the human that the panel was given
        from .memory import Memory

        self.memory = Memory(self.state.parent)  # design.md section 6
        # Output heard from each target, kept across re-scans so what was learned is not lost
        # when the graph is rebuilt (docs/notes.md, 実機で学んだこと). Per target: the latest output, plus
        # the latest one that named the part and the latest one that raised an alert, so a later
        # quiet answer does not wipe them (real bench: `hang` after `panic` lost the ESP32-S3).
        self.observed: dict[str, dict[str, tuple[str, str]]] = {}  # target id -> kind -> (time, text)
        self._lock = threading.RLock()
        self.last: R.Recon | None = None
        # what benchscout itself did on the bench this session (action, port, args, who allowed it): a raw clue the agent
        # otherwise has to ask the human for (real bench, 2026-11-03: "did anyone send something just before the crash?")
        from collections import deque

        self.done: deque[dict] = deque(maxlen=500)
        self._jobs: dict[str, _Job] = {}  # runs that outlasted the call that started them
        self._turns: dict[str, threading.Lock] = {}  # one action at a time on each device
        from .resources import Resources
        from .execution import Run

        self.resources = Resources()
        self.runs: dict[str, Run] = {}
        # how long one agent call may wait in all (the human, a download, the run): under the relay's limit, so the
        # agent gets "running" with a job id rather than a call that timed out (review 7, 21)
        self.call_budget = max(30.0, approval_wait + 50.0)

    def _emit(self, event: dict) -> None:
        if self.on_event:
            self.on_event(event)

    # ---- recon ---------------------------------------------------------------
    def scan(self, only_new: bool = False) -> R.Recon:
        """Look at the bench again. A device in the session is never probed again (its watch and the agent's
        actions are using it; what its probes got before is kept). `only_new`: probe only devices that were not
        there at the last scan (a plug noticed by the core; design.md section 17)."""
        from . import knowledge as K

        with self._lock:
            K.load(self.state.parent if self.workspace_knowledge else None)  # a fixed knowledge file counts at the next scan
            busy = self.locks.holder if self.locks else None
            before = self.last.bench if self.last else None
            keep = set(before.devices) if only_new and before else self.selected & set(before.devices if before else ())
            keys: dict[str, str] = {}

            def prepare(bench) -> None:
                self.resources.apply(bench)
                self._apply_memory(bench)
                keys.update({d.id: self._key(d) for d in bench.devices.values()})

            self.last = R.run(self.io, busy=busy, keep=keep, heard=self.last.heard if self.last else None, prepare=prepare)
            # a unit known only once it answered (a LAN instrument's *IDN?): its memory, now that it is known
            later = {d for d, dev in self.last.bench.devices.items() if self._key(dev) != keys.get(d)}
            if later:
                self._apply_memory(self.last.bench, only=later)
            for tid, kept in self.observed.items():
                if tid not in self.last.bench.targets:
                    continue
                seen_texts = []
                for kind in ("alert", "latest"):
                    if kind in kept and kept[kind][1] not in seen_texts:
                        seen_texts.append(kept[kind][1])
                        R.apply_log([self.last.bench.targets[tid]], kept[kind][1], seen=kept[kind][0],
                                    learned=self._learned_lines(self.last.bench, tid))
            self.last.proposals = R.propose(self.last.bench)
            (self.state / "inventory.json").write_text(json.dumps(self.last.to_dict(), indent=2, default=str), encoding="utf-8")
            # what still waits for the human (from an earlier session) is shown again, for connected devices only
            waiting = [(dev, e) for dev in self.last.bench.devices.values() for e in self.memory.entries(self._key(dev))
                       if e.status == "pending" and e.id not in self._pending_shown]
            changed = self._plugged(before, self.last.bench) if before else None
        self._emit({"event": "scanned", "devices": list(self.last.bench.devices)})
        self._bench_event()
        if changed:
            self._emit(changed)
        for dev, e in waiting:
            self._memory_event(dev, e)
        return self.last

    def _plugged(self, before, now) -> dict | None:
        """Devices that came or went since the last scan. One the human chose that went away stays chosen, so it is
        watched again when it comes back; but nothing granted for its ports carries over: an approval was for what
        was plugged in then, and another board can come back under the same name and port. The same for a port that
        went while its device stayed listed (handed to a VM: real bench, 2026-11-03)."""
        def moved(a, b, p):  # a port of `a` that `b` lacks, or that is another device's there
            return p not in b.ports or b.ports[p].device_id != a.ports[p].device_id

        went_ports = {p for p in before.ports if moved(before, now, p) and before.ports[p].device_id in self.selected}
        if went_ports:
            self._stop_runs({before.ports[p].device_id for p in went_ports})
            self.approvals.withdraw_ports(went_ports)
        for pid in [p for p, w in self.watches.items() if not w.alive]:  # it gave up waiting for the port
            self.watches.pop(pid)
        if any(moved(now, before, p) and now.ports[p].device_id in self.selected for p in now.ports):
            self._autowatch()  # back: watched again (or asked about again)
        came = [d for d in now.devices if d not in before.devices]
        went = [d for d in before.devices if d not in now.devices]
        if not came and not went:
            return None

        def view(b, d):
            dev = b.devices[d]
            return {"id": d, "name": dev.name, "other": dev.other, "selected": d in self.selected}

        return {"event": "devices_changed", "added": [view(now, d) for d in came], "removed": [view(before, d) for d in went]}

    @property
    def bench(self):
        return self.recon.bench

    @property
    def recon(self) -> R.Recon:
        """The last scan, scanning first if there was none."""
        return self.last or self.scan()

    def resolve_port(self, port: str) -> str:
        """Accept a port id (`serial:usbmodem1101`) or its path (`/dev/cu.usbmodem1101`)."""
        b = self.bench
        if port in b.ports:
            return port
        for p in b.ports.values():
            if p.path == port:
                return p.id
        known = ", ".join(f"{p.id} ({p.path})" if p.path else p.id for p in b.ports.values())
        raise KeyError(f"unknown port {port!r}; known ports: {known}")

    def impact(self, action: str, port: str, args: dict | None = None) -> Impact:
        with self._lock:
            pid = self.resolve_port(port)
            args = A.get(action).normalize(args)
            return classify(self.bench, action, pid, args, port_open=self._held_open(pid, args))

    def _held_open(self, port_id: str, args: dict) -> bool:
        """The port is open already at these settings (watched at this baud): the call opens nothing."""
        w = self.watches.get(port_id)
        return bool(w and w.alive and args.get("baud", w.baud) == w.baud)

    # ---- session (human, control channel) --------------------------------------
    def select(self, device_ids: list[str]) -> dict:
        """Set the session to exactly these devices. Each must be free in every other window."""
        with self._lock:
            b = self.bench
            wanted = [self._resolve_device(d) for d in device_ids]
            busy: dict[str, dict] = {}
            for d in wanted:
                if d in self.selected:
                    continue
                holder = self.locks.acquire(d) if self.locks else None
                if holder:
                    busy[d] = holder
            new = {d for d in wanted if d not in busy}
            dropped = self.selected - new
            self._stop_runs(dropped)
            for d in dropped:
                if self.locks:
                    self.locks.release(d)
            self.approvals.withdraw_ports({p.id for d in dropped for p in b.ports_of(d)})
            for p in [p for d in dropped for p in b.ports_of(d)]:
                self._unwatch(p.id)
            self.selected = new
            started = self._autowatch()
        out = {"selected": sorted(self.selected), "busy": busy, "watching": started}
        self._emit({"event": "selection_changed", **out})
        return out

    def _resolve_device(self, ref: str) -> str:
        b = self.bench
        devs = b.devices
        if ref in devs or ref in self.selected:  # (one the human chose may be unplugged for now)
            return ref
        if ref.isdigit() and 1 <= int(ref) <= len(devs):  # the number shown in the console's list
            return list(devs)[int(ref) - 1]
        # also accept what people see: a port id or path (usbserial-10), or part of the name
        names = {d: [d, devs[d].name.lower()] + [x for p in b.ports_of(d) for x in (p.id, p.path or "")] for d in devs}
        hits = [d for d, keys in names.items() if any(ref.lower() in k.lower() for k in keys if k)]
        if len(hits) == 1:
            return hits[0]
        raise KeyError(f"device {ref!r} matches {len(hits)} devices; known: {', '.join(devs)}")

    # ---- questions from the agent (answered by the human, control channel only) ---------
    def ask(self, text: str, options: list[str], question_id: str | None = None, wait: float | None = None) -> dict:
        """The agent asks the human. Waits like an approval; on time-out, call again with the id."""

        wait = self.approval_wait if wait is None else wait
        q = self.questions.wait(question_id, 0) if question_id else self.questions.ask(text, options)
        if q is None:
            return {"status": "unknown_question", "question_id": question_id}
        q = self.questions.wait(q.id, wait)
        if q is None:
            return {"status": "unknown_question", "question_id": question_id}
        if q.status == "pending":
            return {"status": "waiting", "question_id": q.id,
                    "tell_user": "The question is waiting in the benchscout inbox. Call again with this question_id to keep waiting."}
        return {"status": q.status, "question_id": q.id, "choice": q.choice, "reply": q.reply,
                "answered_by": "the human, in the benchscout inbox"}

    def request_devices(self, devices: list[str], reason: str, question_id: str | None = None,
                        wait: float | None = None) -> dict:
        """The agent asks to add devices to the session. Only the human's answer selects them."""
        wait = self.approval_wait if wait is None else wait
        if question_id:
            q = self.questions.wait(question_id, wait)
        else:
            with self._lock:
                try:
                    ids = [self._resolve_device(d) for d in devices]
                except KeyError as e:
                    return {"status": "unknown_device", "message": str(e.args[0])}
                names = {i: self.bench.devices[i].name for i in ids}
            q = self.questions.ask(f"The agent asks to use: {', '.join(names.values())}. Reason: {reason}",
                                   ["allow", "deny"], kind="devices", data={"devices": ids, "names": names, "reason": reason})
            q = self.questions.wait(q.id, wait)
        if q is None:
            return {"status": "unknown_question", "question_id": question_id}
        if q.status == "pending":
            return {"status": "waiting", "question_id": q.id,
                    "tell_user": "Waiting for the human in the benchscout inbox. Call again with this question_id."}
        if q.choice == "allow":
            return {"status": "selected", "selected": sorted(self.selected), "answered_by": "the human, in the benchscout inbox"}
        return {"status": "denied", "reply": q.reply, "answered_by": "the human, in the benchscout inbox"}

    def _on_answer(self, q) -> None:
        if q.kind == "devices" and q.choice == "allow":
            self.select(sorted(self.selected | set(q.data["devices"])))

    def answer(self, question_id: str, choice: str | None = None, reply: str | None = None) -> dict:
        from dataclasses import asdict

        return asdict(self.questions.answer(question_id, choice, reply))

    def decide(self, approval_id: str, granted: bool, scope: Scope = "once") -> dict:
        from dataclasses import asdict

        if scope == "always":
            return self._allow_always(approval_id, granted)
        if scope == "all":
            a0 = self.approvals.get(approval_id)
            if a0 is not None and a0.by != "agent":
                raise ValueError("allow all is for the agent's requests; benchscout's own offers are answered one by one")
        a = self.approvals.decide(approval_id, granted, scope)
        if a.by == "benchscout" and self.watch_offers.get(a.port) == a.id:
            if granted:  # benchscout asked for itself: nobody is waiting on the call, so run it here
                threading.Thread(target=self._start_offered_watch, args=(a.id, a.port), daemon=True).start()
            else:
                self.watch_declined.add(a.port)
        return asdict(a)

    def set_allow_all(self, on: bool) -> dict:
        """Control channel only: allow every DISRUPTIVE call for the rest of the session, or stop (R8: DANGEROUS
        calls still ask each time)."""
        self.approvals.set_allow_all(on)
        return {"allow_all": self.approvals.allow_all}

    def _allow_always(self, approval_id: str, granted: bool) -> dict:
        """Control channel: "always on this device" for a call that asks once and whose commands its protocol judged.
        The human saw the commands and decided, so each is remembered for this device as confirmed (they can forget it
        in the inbox), and this call runs. What is DANGEROUS cannot be allowed this way."""
        from dataclasses import asdict

        a = self.approvals.get(approval_id)
        if a is None or not granted:
            return self.decide(approval_id, granted, "once")
        if not (A.get(a.action).protocol and a.impact.get("subjects")) or a.effect != Effect.DISRUPTIVE.name.lower():
            raise ValueError(f"only a command that asks once can be allowed always; this is {a.action} ({a.effect.upper()})")
        with self._lock:
            dev = self.bench.devices[self.bench.ports[a.port].device_id]
            for h in a.impact.get("subjects") or []:
                self._remember_command(dev, a.port, "allowed always on this device, in the benchscout inbox", "",
                                       {"command": h, "effect": "safe"}, "human", confirmed=True)
        return asdict(self.approvals.decide(approval_id, True, "once"))

    # ---- tools the agent proposes (proposals.py, design.md section 4) ---------------------------
    def define_tool(self, tool: dict | None = None, evidence: str = "", probes: list[dict] | None = None,
                    proposal_id: str | None = None, wait: float | None = None) -> dict:
        """Agent channel: propose an action for a tool benchscout ships nothing for (a vendor's programmer). It waits
        for the human like an approval; once confirmed it is an action under the same policy as any other."""
        from . import knowledge as K
        from .proposals import ProposalError, check

        wait = self.approval_wait if wait is None else wait
        if not self.workspace_knowledge:
            return {"status": "rejected", "message": "this folder is not trusted in VS Code, so benchscout does not read "
                                                     "its knowledge folder and cannot add a tool here"}
        if proposal_id is None:
            try:
                clean, pr = check(tool or {}, probes, evidence, K.TOOLS, set(A.BUILTIN))
            except ProposalError as e:
                return {"status": "invalid", "message": str(e)}
            proposal_id = self.proposals.open(clean, pr, evidence).id
        status = self.proposals.wait(proposal_id, wait)
        p = self.proposals.get(proposal_id)
        if status == "pending":
            return {"status": "waiting", "proposal_id": proposal_id,
                    "tell_user": "The proposed tool is waiting for a decision in the benchscout inbox. Call again with "
                                 "this proposal_id to keep waiting."}
        if status == "accepted" and p:
            return {"status": "defined", "action": p.tool["name"], "effect": p.tool["effect"],
                    "next": f"run it with bench_do {p.tool['name']} on a port of kind {p.tool.get('port_kinds') or 'any'}; "
                            "it is not installed by benchscout: if bench_do says so, ask the user to install it"}
        return {"status": status, "proposal_id": proposal_id}

    def decide_tool(self, proposal_id: str, accept: bool) -> dict:
        """Control channel only: the human confirms or rejects a proposed tool. Confirmed, it is written into this
        folder's knowledge, loaded, and the bench is scanned again (a programmer it names becomes a port)."""
        from dataclasses import asdict
        from datetime import date

        from . import knowledge as K
        from .proposals import write

        p = self.proposals.get(proposal_id)
        if p is None or p.status != "pending":
            raise ValueError(f"no pending tool proposal {proposal_id!r}")
        if accept:  # the action exists before the agent's waiting call hears "defined" (real bench, 2026-11-02)
            write(self.state / "knowledge", p, date.today().isoformat())
            K.load(self.state.parent)
            self.scan()
        return asdict(self.proposals.decide(proposal_id, accept))

    # ---- memory (design.md section 6) ---------------------------------------------------------
    def _key(self, dev) -> str:
        from .memory import device_key

        if dev.id.startswith("resource:"):
            return dev.id.replace(":", "-")
        return device_key(dev.vid, dev.pid, dev.serial)

    @staticmethod
    def _one_unit(bench, dev) -> bool:
        """Memory is kept by model and serial number. It is about this very unit only if the serial is the unit's own: it
        has one, the model does not share one serial across units, and no other unit plugged in carries it. Memory that
        loosens safety applies only then (design.md section 3, runtime contracts; notes.md review 7: a second board
        without a serial took on the first's 'safe to open')."""
        from . import knowledge as K

        if not dev.serial or dev.serial in K.SHARED_SERIALS:
            return False
        return sum(1 for d in bench.devices.values() if (d.vid, d.pid, d.serial) == (dev.vid, dev.pid, dev.serial)) == 1

    def _header(self, dev) -> dict:
        from . import knowledge as K

        return {"device": dev.name, "vid": f"{dev.vid or 0:04x}", "pid": f"{dev.pid or 0:04x}", "serial": dev.serial or "",
                "shared_serial": dev.serial in K.SHARED_SERIALS}

    def _apply_memory(self, bench, only: set[str] | None = None) -> None:
        """What was learned before shows up in the first read, without an LLM. `only`: these devices (the rest is done)."""
        from .model import Confidence, Evidence

        for dev in bench.devices.values():
            if only is not None and dev.id not in only:
                continue
            shared = not self._one_unit(bench, dev)
            targets = [t for p in bench.ports_of(dev.id) for t in bench.targets_via(p.id)]
            for e in self.memory.entries(self._key(dev)):
                if e.status != "active" and not (e.kind == "command" and e.status == "pending"):
                    continue  # (a command waiting for the human is still usable, gated like serial.send)
                when = f"{e.time[:10]}, {e.source}"
                if e.kind == "identity" and e.target in bench.targets:
                    t = bench.targets[e.target]
                    if t.confidence != Confidence.IDENTIFIED:
                        t.name, t.confidence = e.text, Confidence.IDENTIFIED
                    t.evidence.append(Evidence("learned", f"{e.text}: \"{e.evidence}\" ({when})"))
                elif e.kind == "safety":
                    if e.target in bench.ports and e.data.get("listen_safe") is False:
                        bench.ports[e.target].may_reset_on_open = True  # seen to reset when opened: stricter, at once
                        continue
                    if shared:
                        continue  # memory of this model and serial may be another unit's: never loosen safety on it
                    if e.target in bench.ports and e.data.get("listen_safe"):
                        bench.ports[e.target].may_reset_on_open = False
                elif e.kind == "command" and e.data.get("command"):
                    self._load_command(bench, dev, e)
                    for t in bench.targets_via(e.target)[:1] if e.target in bench.ports else []:
                        t.facts.append(f"command `{e.data['command']}` on {e.target}: {e.text} "
                                       f"({e.data.get('effect', 'disruptive')}{', waiting for the human' if e.status == 'pending' else ''})")
                elif e.kind == "interpretation" and e.data.get("pattern"):
                    dev.line_rules.append(e.data)
                else:
                    baud = e.data.get("baud") if e.kind == "config" else None
                    if isinstance(baud, int) and not isinstance(baud, bool) and 50 <= baud <= 12_000_000:
                        for p in [bench.ports[e.target]] if e.target in bench.ports else bench.ports_reaching(e.target):
                            p.baud = baud  # the rate the agent found it talks at: watched at it from now on
                    inside = e.data.get("inside") if e.kind == "config" else None
                    if inside and e.target in bench.targets and inside in bench.targets and inside != e.target:
                        # learned wiring is structure, not only words: what disturbs the host disturbs this (review 7, 13)
                        t = bench.targets[e.target]
                        if t.hosted_on is None:
                            t.hosted_on = inside
                        elif t.hosted_on != inside:
                            t.facts.append(f"also said to be inside {inside} ({when}); kept inside {t.hosted_on}")
                    for t in ([bench.targets[e.target]] if e.target in bench.targets else targets[:1]):
                        t.facts.append(f"remembered: {e.text} ({when})")

    def _observe_open(self, port_id: str, text: str | None) -> None:
        """benchscout's own observation, written without asking: opening a port that may reset did
        not make it vanish. After two, propose (pending the human) treating listening as SAFE. A port opened as safe
        that vanished at once says the opposite (_reset_on_open)."""
        from .io import VANISHED_MARK
        from .memory import Entry

        port = self.bench.ports[port_id]
        if not port.may_reset_on_open:
            gone = re.search(re.escape(VANISHED_MARK) + r" at ([\d.]+) s", text or "")
            after = float(gone.group(1)) if gone else None
            if text is None:
                self._reset_on_open(port_id, "it vanished right after it was opened")
            elif after is not None and after < OPEN_RESET_S:
                self._reset_on_open(port_id, f"it vanished {after:.1f} s after it was opened")
            return
        if text is None or VANISHED_MARK in text:
            return
        dev = self.bench.devices[port.device_id]
        if not port.own_usb or not self._one_unit(self.bench, dev):
            # behind a bridge the port stays whether the MCU reset or not: its staying proves nothing (review 7, 19)
            return
        key = self._key(dev)
        self.memory.add(key, Entry(kind="observation", source="benchscout", target=port_id,
                                   text=f"opened {port.path} and the chip's own USB port stayed (did not drop off and come back)",
                                   data={"opened": "stayed"}, time=time.strftime("%Y-%m-%dT%H:%M:%S")), self._header(dev))
        entries = self.memory.entries(key)
        # what counts is what was seen since the device last said otherwise (it reset when opened)
        since = max((e.time for e in entries if e.kind == "safety" and e.target == port_id and e.data.get("listen_safe") is False),
                    default="")
        seen = sum(1 for e in entries if e.kind == "observation" and e.target == port_id and e.time > since
                   and (e.data.get("opened") == "stayed" or "did not vanish" in e.text or "own USB port stayed" in e.text))
        if seen >= 2 and not any(e.kind == "safety" and e.target == port_id and e.status in ("pending", "active", "rejected") for e in entries):
            e = self.memory.add(key, Entry(kind="safety", source="benchscout", target=port_id, status="pending",
                                           text=f"Opening {port.path} left the chip's own USB port in place ({seen} times): it did not "
                                                f"reset, as this chip shows a reset by dropping off. Treat listening as SAFE?",
                                           data={"listen_safe": True, "seen": seen}), self._header(dev))
            self._memory_event(dev, e)

    def _reset_on_open(self, port_id: str, what: str) -> None:
        """A port judged safe to open (by a confirmed memory or the shipped data) vanished as soon as it was opened: the
        device reset or re-enumerated. Memory is used as possibly stale (design.md section 6), and the newer
        observation wins: the entry that said listening is safe is marked contradicted (kept as history), and a stricter
        entry takes effect at once, so opening this port asks first from now on. The human can delete it."""
        from dataclasses import asdict

        from .memory import Entry

        with self._lock:
            port = self.bench.ports.get(port_id)
            if port is None or port.may_reset_on_open:
                return
            dev = self.bench.devices[port.device_id]
            key = self._key(dev)
            port.may_reset_on_open = True
            for e in self.memory.entries(key):
                if e.kind == "safety" and e.target == port_id and e.status in ("active", "pending") and e.data.get("listen_safe"):
                    old = self.memory.set_status(key, e.id, "contradicted")
                    self._emit({"event": "memory_decided", "key": key, "entry": asdict(old)})
            e = self.memory.add(key, Entry(kind="safety", source="benchscout", target=port_id, status="active",
                                           text=f"opened {port.path} as safe to open and {what} (a reset or re-enumeration): "
                                                "opening it asks first from now on",
                                           data={"listen_safe": False}), self._header(dev))
        self._memory_event(dev, e)

    def _memory_event(self, dev, entry) -> None:
        from dataclasses import asdict

        if entry.status == "pending":
            self._pending_shown.add(entry.id)
        self._emit({"event": "memory_added", "key": self._key(dev), "device": dev.id, "device_name": dev.name, "entry": asdict(entry)})

    def remember(self, ref: str, kind: str, text: str, evidence: str = "", data: dict | None = None, source: str = "agent") -> dict:
        """Written without asking (design.md 6). Safety-loosening entries wait for the human."""
        from .memory import Entry

        with self._lock:
            dev, target = self._resolve_ref(ref)
            if kind == "command":
                return self._remember_command(dev, ref, text, evidence, data or {}, source)
            if kind == "identity":
                return {"status": "rejected", "message": "a part is named only with bench_identify, quoting the device's own "
                                                         "output (benchscout checks the quote)"}
            inside = (data or {}).get("inside")
            if kind == "config" and inside is not None:
                if not target or inside not in self.bench.targets or inside == target:
                    known = ", ".join(t for t in self.bench.targets if t != target)
                    return {"status": "rejected", "message": "config data {\"inside\": \"<target id>\"} says the far end of this port "
                                                             f"is implemented inside that target; give one of: {known}"}
            pattern = (data or {}).get("pattern")
            if kind == "interpretation" and pattern is not None:
                try:
                    re.compile(str(pattern))  # checked now: a bad one would stop the next watch from starting
                except re.error as err:
                    return {"status": "rejected", "message": f"pattern is not a regular expression: {err}"}
            if kind == "safety":
                allowed = {"listen_safe"}
                if not data or set(data) - allowed:
                    return {"status": "rejected", "message": f"a safety entry needs data with only: {', '.join(sorted(allowed))}"}
                port = next((p.id for p in self.bench.ports_of(dev.id) if p.id == ref or p.path == ref), None)
                if not port:
                    return {"status": "rejected", "message": "a safety entry is about one port: give its id or path"}
                target = port
            e = Entry(kind=kind, text=text, source=source, evidence=evidence, target=target or "",
                      status="pending" if kind == "safety" else "active", data=data or {})
            e = self.memory.add(self._key(dev), e, self._header(dev))
            if kind == "interpretation":
                self._refresh_patterns(dev)  # the running watch looks for it from now on
            if kind == "config" and inside and self.bench.targets[target].hosted_on is None:
                self.bench.targets[target].hosted_on = inside  # in effect now, not at the next scan
        self._memory_event(dev, e)
        return {"status": "pending_human" if e.status == "pending" else "remembered", "id": e.id}

    @staticmethod
    def _learned_lines(bench, target_id: str) -> list[dict]:
        ports = bench.ports_reaching(target_id)
        return bench.devices[ports[0].device_id].line_rules if ports else []

    def _refresh_patterns(self, dev) -> None:
        """The lines learned to mean trouble on this device, given to its running watches."""
        pats = [e.data for e in self.memory.entries(self._key(dev))
                if e.kind == "interpretation" and e.status == "active" and e.data.get("pattern")]
        if dev.id in self.bench.devices:
            self.bench.devices[dev.id].line_rules = list(pats)
        for w in list(self.watches.values()):
            if w.device_id == dev.id:
                w.set_patterns(pats)

    def _remember_command(self, dev, ref: str, text: str, evidence: str, data: dict, source: str,
                          confirmed: bool = False) -> dict:
        """A firmware command for one serial port, or a SCPI command (by its header) for one instrument. Whatever
        makes it run with fewer questions than benchscout would ask by itself waits for the human."""
        from .memory import Entry

        cmd = str(data.get("command") or "").strip()
        effect = str(data.get("effect") or "disruptive").lower()
        if not cmd or "\n" in cmd or "\r" in cmd:
            return {"status": "rejected", "message": 'a command entry needs data {"command": "<what to send>", "effect": '
                                                     '"safe"|"disruptive"|"dangerous", "eol": "\\n" (optional), "until": '
                                                     '"<regex for the line that ends its answer>" (optional)}'}
        try:
            from .codec import until_pattern

            until_pattern(data.get("until"))
        except ValueError as err:
            return {"status": "rejected", "message": str(err)}
        if effect not in ("safe", "disruptive", "dangerous"):
            return {"status": "rejected", "message": "effect must be safe, disruptive or dangerous"}
        limits = data.get("limits")
        if limits is not None and (not isinstance(limits, list) or len(limits) > 16 or not all(
                x is None or isinstance(x, dict) and x and set(x) <= {"min", "max"}
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in x.values()) for x in limits)):
            return {"status": "rejected", "message": 'limits: a list, one item per value after the command: null (any value) '
                                                     'or {"min": number, "max": number} (either may be left out)'}
        if "eol" in data and data["eol"] not in LINE_ENDINGS:
            # a line ending that carries more ("\nerase\n") would send a second command the human never saw
            return {"status": "rejected", "message": "eol must be one of \"\\n\", \"\\r\\n\", \"\\r\" or \"\" (nothing)"}
        port = next((p.id for p in self.bench.ports_of(dev.id) if p.id == ref or p.path == ref), None)
        if not port:
            ports = [p.id for p in self.bench.ports_of(dev.id) if protocols.for_port(p)]
            if len(ports) != 1:
                return {"status": "rejected", "message": f"a command belongs to one port; give its id ({', '.join(ports) or 'none'})"}
            port = ports[0]
        proto = protocols.for_port(self.bench.ports[port])
        if proto is None:
            return {"status": "rejected", "message": f"{port} carries no commands (a tool reaches it)"}
        try:
            proto.check(cmd)
            one = len(proto.commands(cmd)) == 1
        except A.ArgError as err:
            return {"status": "rejected", "message": str(err)}
        if not one:
            return {"status": "rejected", "message": "remember one command per entry (one line, one header, no ';')"}
        key = self._key(dev)
        loosens = self._loosens(self.bench.ports[port], cmd, effect, self._in_force(key, self.bench.ports[port], cmd)) \
            or self._widens(key, self.bench.ports[port], cmd, effect, limits)
        status = "pending" if loosens and not confirmed else "active"
        if status == "active":  # the same command again: the newer entry replaces the older (a weaker one only once confirmed)
            self._supersede_command(key, self.bench.ports[port], cmd)
        e = Entry(kind="command", text=text, source=source, evidence=evidence, target=port, status=status,
                  data={"command": cmd, "effect": effect, **{k: data[k] for k in ("eol", "until", "limits") if data.get(k) is not None}})
        e = self.memory.add(key, e, self._header(dev))
        self._load_command(self.bench, dev, e)
        self._memory_event(dev, e)
        return {"status": "pending_human" if e.status == "pending" else "remembered", "id": e.id, "port": port,
                **({"message": "it runs with fewer questions only after the human confirms it in the panel; until then "
                               "it asks as before"} if e.status == "pending" else {})}

    @staticmethod
    def _command_key(port, cmd: str) -> str:
        """How a remembered command is looked up, as the port's protocol says."""
        proto = protocols.for_port(port)
        return proto.key(cmd) if proto else ""

    @staticmethod
    def _loosens(port, cmd: str, effect: str, in_force: Effect | None = None) -> bool:
        """Would this entry make the command ask less than it does now: than the rule in force for it (DANGEROUS
        remembered, then DISRUPTIVE: that is loosening too), or than the protocol's default when nothing is remembered?"""
        proto = protocols.for_port(port)
        key = proto.key(cmd) if proto else ""
        if not proto or not key:
            return True
        floor = proto.default(key)[0]
        return Effect[effect.upper()] < (in_force if in_force is not None else floor)

    def _widens(self, key: str, port, cmd: str, effect: str, limits: list | None) -> bool:
        """Would this entry let through values the rule in force (at this effect or stronger) keeps out? Removing or
        widening limits asks less too, so the human confirms it (design.md section 3, runtime contracts)."""
        same = self._command_key(port, cmd)
        for e in self.memory.entries(key):
            if not (e.kind == "command" and e.target == port.id and e.status == "active"
                    and self._command_key(port, e.data.get("command", "")) == same):
                continue
            old = e.data.get("limits")
            if not old or Effect[e.data.get("effect", "disruptive").upper()] < Effect[effect.upper()]:
                continue
            for i, o in enumerate(old):
                n = (limits or [])[i] if i < len(limits or []) else None
                if o is None:
                    continue
                if n is None or n.get("min", float("-inf")) < o.get("min", float("-inf")) \
                        or n.get("max", float("inf")) > o.get("max", float("inf")):
                    return True
        return False

    def _in_force(self, key: str, port, cmd: str) -> Effect | None:
        """The strongest effect remembered (and in force) for this command on this port."""
        same = self._command_key(port, cmd)
        effects = [Effect[e.data.get("effect", "disruptive").upper()] for e in self.memory.entries(key)
                   if e.kind == "command" and e.target == port.id and e.status == "active"
                   and self._command_key(port, e.data.get("command", "")) == same]
        return max(effects) if effects else None

    def _reload_command(self, key: str, port, cmd: str) -> None:
        """This command on this port, as memory has it now (entries in the order they were written)."""
        same = self._command_key(port, cmd)
        port.commands.pop(same, None)
        dev = self.bench.devices[port.device_id]
        for e in self.memory.entries(key):
            if e.kind == "command" and e.target == port.id and self._command_key(port, e.data.get("command", "")) == same:
                self._load_command(self.bench, dev, e)

    def _supersede_command(self, key: str, port, cmd: str, keep: str = "") -> None:
        same = self._command_key(port, cmd)
        for old in self.memory.entries(key):
            if (old.kind == "command" and old.target == port.id and old.status in ("active", "pending") and old.id != keep
                    and self._command_key(port, old.data.get("command", "")) == same):
                self.memory.set_status(key, old.id, "superseded")

    def _load_command(self, bench, dev, e) -> None:

        port = bench.ports.get(e.target)
        if not port or e.status not in ("active", "pending"):
            return
        effect = e.data.get("effect", "disruptive")
        key = self._command_key(port, e.data["command"])
        if not key:
            return
        loosens = self._loosens(port, e.data["command"], effect)
        shared = not self._one_unit(bench, dev)  # the memory may be another unit's: never loosen safety on it
        held = port.commands.get(key)
        if e.status == "pending" and held and not held.get("pending"):
            return  # waiting for the human: what is in force stays in force meanwhile
        port.commands[key] = {"effect": effect, "eol": e.data.get("eol", "\n"), "until": e.data.get("until"), "text": e.text,
                              "limits": e.data.get("limits"),
                              "loosens": loosens,
                              "pending": e.status == "pending" or (loosens and shared)}

    def forget(self, key: str, entry_id: str) -> dict:
        """Control channel: the human removes an entry. What it loosened stops at once, not at the next scan."""
        with self._lock:
            e = next((x for x in self.memory.entries(key) if x.id == entry_id), None)
            self.memory.delete(key, entry_id)
            self._emit({"event": "memory_forgotten", "key": key, "id": entry_id})
            if e and self.last and e.target in self.last.bench.ports:
                port = self.last.bench.ports[e.target]
                if e.kind == "command":
                    self._reload_command(key, port, e.data.get("command", ""))
                if e.kind == "safety" and e.data.get("listen_safe"):
                    port.may_reset_on_open = True  # back to asking before opening
            if e and e.kind == "interpretation" and self.last:
                for dev in self.last.bench.devices.values():
                    if self._key(dev) == key:
                        self._refresh_patterns(dev)
        return {"deleted": entry_id}

    def recall(self, ref: str | None = None) -> list[dict]:
        from dataclasses import asdict

        devs = [self._resolve_ref(ref)[0]] if ref else [self.bench.devices[d] for d in sorted(self.selected)] or list(self.bench.devices.values())
        return [{"device": d.name, "key": self._key(d), "entries": [asdict(e) for e in self.memory.entries(self._key(d))]} for d in devs]

    def memory_view(self) -> list[dict]:
        """Control channel: what is remembered about each device on the bench, with the file it lives in, for the
        inbox's list (design.md section 6: the person sees, fixes and deletes it). Entries no longer in force
        (superseded, rejected) are left out; the file keeps them."""
        from dataclasses import asdict

        out = []
        for d in self.bench.devices.values():
            if d.other:
                continue
            key = self._key(d)
            entries = [asdict(e) for e in self.memory.entries(key) if e.status in ("active", "pending")]
            if entries:
                out.append({"device": d.id, "name": d.name, "key": key, "file": str(self.memory.device_file(key).path),
                            "entries": entries})
        return out

    def decide_memory(self, key: str, entry_id: str, accept: bool) -> dict:
        """Control channel only: the human confirms (or rejects) a safety-loosening entry."""
        from dataclasses import asdict

        e = self.memory.set_status(key, entry_id, "active" if accept else "rejected")
        if accept and e.kind == "safety" and self.last and e.target in self.last.bench.ports and e.data.get("listen_safe"):
            port = self.last.bench.ports[e.target]
            if self._one_unit(self.last.bench, self.last.bench.devices[port.device_id]):  # as it is applied after a restart
                port.may_reset_on_open = False
        if e.kind == "command" and self.last and e.target in self.last.bench.ports:
            port = self.last.bench.ports[e.target]
            if accept:
                self._supersede_command(key, port, e.data.get("command", ""), keep=e.id)
            # what is in force for this command, from memory again: a rejected weaker entry leaves the stronger one
            self._reload_command(key, port, e.data.get("command", ""))
        self._emit({"event": "memory_decided", "key": key, "entry": asdict(e)})
        return asdict(e)

    def _resolve_ref(self, ref: str):
        """A device, port or target reference -> (device, target id or "")."""
        b = self.bench
        if ref in b.targets:
            port = b.ports_reaching(ref)[0]
            return b.devices[port.device_id], ref
        try:
            pid = self.resolve_port(ref)
            port = b.ports[pid]
            ts = b.targets_via(pid)
            return b.devices[port.device_id], ts[0].id if ts else ""
        except KeyError:
            dev = b.devices[self._resolve_device(ref)]
            ts = [t for p in b.ports_of(dev.id) for t in b.targets_via(p.id)]
            return dev, ts[0].id if len(ts) == 1 else ""

    def identify(self, ref: str, name: str, quote: str) -> dict:
        """The agent names a part, quoting the device's own output. benchscout checks the quote is
        really in what it captured from that device (design.md section 3, "名前の付け方")."""
        from .memory import Entry
        from .model import Confidence, Evidence

        q = (quote or "").strip()
        with self._lock:
            dev, tid = self._resolve_ref(ref)
            if not tid:
                return {"status": "rejected", "message": "say which target (a port of it, or the target id): the device has several"}
            t = self.bench.targets[tid]
            if len(q) < 6:
                return {"status": "rejected", "message": "the quote is too short to be evidence; quote the device's output line"}
            where = self._find_quote(tid, q)
            if not where:
                guess = f"guess (not verified): {name}"
                if guess not in t.facts:
                    t.facts.append(guess)
                return {"status": "unverified", "message": "the quote is not in anything benchscout captured from this device; "
                                                           "recorded as a guess, not as its name"}
            t.name, t.confidence = name, Confidence.IDENTIFIED
            t.evidence.append(Evidence("agent-read", f"\"{q}\" ({where})"))
            e = self.memory.add(self._key(dev), Entry(kind="identity", text=name, source="agent", evidence=q, target=tid), self._header(dev))
        self._memory_event(dev, e)
        self._bench_event()  # the name shows in the panel now, not at the next scan
        return {"status": "identified", "target": tid, "found_in": where}

    def _bench_event(self) -> None:
        """The bench as it is now, for the human's panel: a scan or a name from the agent's side shows at once (review 7, 18)."""
        from dataclasses import asdict

        from . import knowledge as K
        from . import memory as M

        r = self.last
        if r is not None:
            self._emit({"event": "bench", "bench": r.bench.to_dict(), "proposals": [asdict(p) for p in r.proposals],
                        "knowledge_errors": list(K.PROBLEMS) + [f"{p}: {e}" for p, e in M.PROBLEMS.items()]})

    def read_log(self, port: str, lines: int = 200, contains: str | None = None) -> dict:
        """The end of a watched port's log (SAFE: it reads a file benchscout wrote)."""
        pid = self.resolve_port(port)
        log = self.state / "logs" / (pid.replace(":", "_") + ".log")
        if not log.exists():
            watch = self._watch_action(self.bench.ports[pid])
            return {"status": "no_log", "message": f"{pid} has not been watched in this folder yet"
                                                   + (f"; use {watch}" if watch else "")}
        text = _tail(log, RECENT_FOR_QUOTES).splitlines()
        if contains:
            text = [l for l in text if contains in l]
        tail = text[-max(1, min(lines, 2000)):]
        done = self._done_on(self.bench.ports[pid].device_id)[-20:]
        return {"status": "done", "log": str(log.resolve()), "lines": len(tail), "text": "\n".join(tail),
                **({"through_benchscout": done} if done else {})}

    def _find_quote(self, target_id: str, quote: str) -> str | None:
        texts: list[tuple[str, str]] = []
        for p in self.bench.ports_reaching(target_id):
            for run in list(self.runs.values()):
                if len(run.plan.ports) == 1 and run.plan.ports[0][0] == p.id:
                    for output in (run.stdout, run.stderr):
                        if output.exists():
                            texts.append((str(output), _tail(output, RECENT_FOR_QUOTES)))
            w = self.watches.get(p.id)
            if w:
                texts.append(("the watched log", w.recent(RECENT_FOR_QUOTES)))
            log = self.state / "logs" / (p.id.replace(":", "_") + ".log")
            if log.exists():
                texts.append((log.name, _tail(log, RECENT_FOR_QUOTES)))
            for f in sorted(self.artifacts.glob(f"*{p.id.replace(':', '_')}*"))[-20:]:
                texts.append((f.name, f.read_text(encoding="utf-8", errors="replace")))
        for kind, (when, text) in self.observed.get(target_id, {}).items():
            texts.append((f"output seen {when}", text))
        for ev in self.bench.targets[target_id].evidence:
            if ev.source not in ("learned", "board-knowledge", "agent-registration"):
                texts.append((ev.source, ev.detail))
        return next((where for where, text in texts if quote in text), None)

    # ---- watching (design.md section 17) -------------------------------------------------
    def _autowatch(self) -> list[str]:
        """Selected devices' serial ports that are SAFE to open are watched right away. For the
        others benchscout asks the human itself (an approval card), since watching is what lets
        it notice crashes and resets; the agent does not have to think of it."""
        started = []
        for d in self.selected:
            if self._run_owns(d):
                continue
            for p in self.bench.ports_of(d):
                watch = self._watch_action(p)
                if not (watch and p.path and p.id not in self.watches):
                    continue
                if classify(self.bench, watch, p.id).effect == Effect.SAFE:
                    if self._watch(p.id, p.baud or DEFAULT_BAUD):
                        started.append(p.id)
                        self._watching_event(p.id)  # the human sees which ports are watched
                else:
                    self._offer_watch(p.id)
        return started

    @staticmethod
    def _watch_action(port) -> str | None:
        from . import protocols

        proto = protocols.for_port(port)
        return proto.watch_action if proto else None

    def _watching_event(self, port_id: str) -> None:
        w = self.watches[port_id]
        self._device_event({"event": "device_event", "port": port_id, "kind": "watching",
                            "what": "watching: everything it prints goes to the log", "line": None,
                            "log": str(w.log.resolve()), "time": time.time()})

    def _offer_watch(self, port_id: str) -> None:
        if port_id in self.watch_declined:
            return
        offered = self.watch_offers.get(port_id)
        if offered and any(a.id == offered for a in self.approvals.pending()):
            return
        watch = self._watch_action(self.bench.ports[port_id])
        if not watch:
            return
        args = A.get(watch).normalize({"baud": self.bench.ports[port_id].baud or DEFAULT_BAUD})
        impact = classify(self.bench, watch, port_id, args)
        dev = self.bench.devices[self.bench.ports[port_id].device_id]
        a = self.approvals.open(impact, A.canonical(args), by="benchscout", subject=dev.name,
                                purpose="Hold the port open so benchscout sees everything it prints and notices crashes and resets.")
        self.watch_offers[port_id] = a.id

    def _start_offered_watch(self, approval_id: str, port_id: str) -> None:
        r = self.request(self._watch_action(self.bench.ports[port_id]) or "", port_id,
                         {"baud": self.bench.ports[port_id].baud or DEFAULT_BAUD},
                         approval_id=approval_id, wait=0)
        w = self.watches.get(port_id)
        if r.get("status") != "done" or not w:
            return  # _watch already said why (unwatchable), or the device left the session
        self._watching_event(port_id)

        def settled() -> None:  # it did not vanish when opened: benchscout's own observation (design.md 6)
            now = self.watches.get(port_id)
            if now is not None and now is w and now.alive and now.vanishes == 0:
                self._observe_open(port_id, "")

        threading.Timer(3.0, settled).start()

    def watch_state(self) -> dict[str, str]:
        """For the agent: how each selected device's serial port is watched, in words."""
        out = {}
        for d in sorted(self.selected):
            for p in self.bench.ports_of(d):
                if not self._watch_action(p) or not p.path:
                    continue
                w = self.watches.get(p.id)
                if w and w.alive:
                    out[p.id] = f"watched; everything it prints is in {w.log.resolve()} (read it with bench_log)"
                elif p.id in self.watch_declined:
                    out[p.id] = "not watched: the human declined watching it in this session"
                elif any(a.id == self.watch_offers.get(p.id) for a in self.approvals.pending()):
                    out[p.id] = ("not watched yet: benchscout asked the human in the panel whether to watch it "
                                 "(opening it may disturb the device). Do not ask again; work with what you have meanwhile")
                else:
                    out[p.id] = "not watched"
        return out

    def _watch(self, port_id: str, baud: int) -> bool:
        from .watch import PortWatch

        port = self.bench.ports[port_id]
        if self._run_owns(port.device_id):
            return False
        w = PortWatch(self.io, port_id, port.path, self.state / "logs", self._device_event, baud, port.open_lines,
                      patterns=self.bench.devices[port.device_id].line_rules, device_id=port.device_id,
                      may_reopen=self._may_reopen)
        try:
            w.start()
        except Exception as e:  # busy elsewhere, gone: a fact, not a crash
            self._emit({"event": "device_event", "port": port_id, "device": port.device_id, "kind": "unwatchable", "what": str(e)})
            return False
        self.watches[port_id] = w
        return True

    def _may_reopen(self, w) -> tuple[bool, str]:
        """Asked by a watch before it opens its port again (design.md section 3: the broker decides every open). Read
        without the broker's lock: the session may be stopping the watch while it asks."""
        last = self.last
        port = last.bench.ports.get(w.port_id) if last else None
        if port is None or port.device_id != w.device_id:
            return False, "its port now belongs to another device, or is gone"
        if w.device_id not in self.selected:
            return False, "the device is no longer in the session"
        if port.may_reset_on_open:
            if w.port_id not in self.watch_approved:
                return False, "opening it may reset the device, so benchscout asks before opening it again"
            if w.reset_after_open:
                return False, "it dropped off right after it was opened: opening it resets the device, so benchscout asks again"
        return True, ""

    def _unwatch(self, port_id: str) -> None:
        w = self.watches.pop(port_id, None)
        if w:
            w.stop()

    def _device_event(self, ev: dict) -> None:
        last = self.last
        port = last.bench.ports.get(ev["port"]) if last else None
        if last and port:
            ev["device"] = port.device_id
            ev["device_name"] = last.bench.devices[port.device_id].name
            if ev["kind"] in ("alert", "vanished", "returned", "gone"):
                recent = self._done_on(port.device_id, within=120)
                if recent:  # benchscout's own record of what it did just before: a clue, not an explanation
                    ev["through_benchscout"] = recent
        self._emit(ev)
        w = self.watches.get(ev["port"])
        if ev["kind"] == "vanished" and ev.get("cause") != "host_sleep" and w and w.reset_after_open:
            self._reset_on_open(ev["port"], f"it vanished {time.monotonic() - w.opened_at:.1f} s after it was opened")
        if ev["kind"] == "gone" and w and "asks" in ev.get("what", "") and w.device_id in self.selected:
            self.watches.pop(ev["port"], None)
            self.watch_offers.pop(ev["port"], None)
            self._offer_watch(ev["port"])  # benchscout asks the human again, as it did the first time

    def end_session(self) -> None:
        with self._lock:
            self._stop_runs(set(self.selected))
            self.approvals.withdraw_ports(set(self.bench.ports))
        for pid in list(self.watches):
            self._unwatch(pid)
        with self._lock:
            if self.locks:
                self.locks.release_all()
            self.selected = set()
            self.approvals.end_session()

    # ---- agent-owned programs: no protocol or command interpretation -------------------
    def register_resource(self, address: str, label: str, device: str | None = None) -> dict:
        with self._lock:
            if not self.workspace_knowledge:
                raise ValueError("resource registration requires a trusted workspace")
            r = self.resources.register(self.bench, address, label, device)
            self._bench_event()
            return {"resource": r.id, "device": r.device, "address": r.address, "selected": r.device in self.selected}

    def _run_owns(self, device: str) -> bool:
        return any(not r.done.is_set() and any(p[1] == device for p in r.plan.ports) for r in self.runs.values())

    def _stop_runs(self, devices: set[str]) -> None:
        for r in self.runs.values():
            if not r.done.is_set() and any(p[1] in devices for p in r.plan.ports):
                r.stop()

    def run_state(self, job: str, stop: bool = False) -> dict:
        with self._lock:
            r = self.runs.get(job)
            if r is None:
                return {"status": "unknown_job", "job": job}
            if stop:
                r.stop()
            return r.view()

    def run_program(self, resources: list[str], argv: list[str], purpose: str, files: list[str] | None = None,
                    timeout: float = 60, observe: bool = False, max_bytes: int = 16777216,
                    approval_id: str | None = None) -> dict:
        from .execution import Run, prepare

        with self._lock:
            if not self.workspace_knowledge:
                return {"status": "untrusted_workspace"}
            try:
                if not isinstance(resources, list) or not resources or not all(isinstance(p, str) for p in resources):
                    raise ValueError("resources must contain at least one registered or discovered port id")
                ports = [self.bench.ports[self.resolve_port(p)] for p in sorted(set(resources))]
                for p in ports:
                    refused = self._outside_session(p.id)
                    if refused:
                        return refused
                plan = prepare(argv, [(p.id, p.device_id, p.path) for p in ports], files or [],
                               self.state.parent, purpose, timeout, observe, max_bytes)
            except (ValueError, KeyError, OSError) as e:
                return {"status": "invalid_plan", "message": str(e)}
            key = plan.canonical()
            impact = Impact("program.run", plan.ports[0][0], Effect.DANGEROUS,
                            "Trusted external program; its physical effects cannot be verified. " + purpose,
                            affected_ports=sorted({q.id for p in ports for q in self.bench.ports_of(p.device_id)}))
            if approval_id is None:
                approval_id = self.approvals.open(impact, key, purpose=purpose).id
            elif self.approvals.asked_for(approval_id) != (impact.action, impact.port_id, key):
                return {"status": "approval_mismatch", "approval_id": approval_id}
        status = self.approvals.wait(approval_id, self.approval_wait)
        if status == "pending":
            return {"status": "approval_required", "approval_id": approval_id, "impact": impact.to_dict()}
        if status != "granted":
            return {"status": status, "approval_id": approval_id}
        with self._lock:
            for pid, dev, address in plan.ports:
                p = self.bench.ports.get(pid)
                if not p or (p.device_id, p.path) != (dev, address) or dev not in self.selected:
                    return {"status": "resource_changed"}
            turns: list[threading.Lock] = []
            for dev in sorted({p[1] for p in plan.ports}):
                turn = self._device_turn(dev)
                if not turn.acquire(blocking=False):
                    for held in turns:
                        held.release()
                    return {"status": "busy", "device": dev, "approval_id": approval_id}
                turns.append(turn)
            def release() -> None:
                for held in turns:
                    held.release()
            try:
                plan.verify()
                used = self.approvals.consume(approval_id, impact.action, impact.port_id, key, "dangerous")
                if used != "ok":
                    release()
                    return {"status": used, "approval_id": approval_id}
                r = Run(plan, self.artifacts, self._emit)
                self.runs[r.id] = r
                for p in list(self.watches):
                    if self.bench.ports[p].device_id in {x[1] for x in plan.ports}:
                        self._unwatch(p)
                self.done.append({"at": time.time(), "time": time.strftime("%H:%M:%S"), "action": impact.action,
                                  "port": impact.port_id, "device": plan.ports[0][1], "args": key,
                                  "allowed": f"human approval {approval_id}"})
                def finished() -> None:
                    with self._lock:
                        self._autowatch()
                threading.Thread(target=r.work, args=(release, finished), daemon=True).start()
            except Exception:
                release()
                raise
        return r.view()

    # ---- the one entry point the agent uses -----------------------------------
    def request(self, action: str, port: str, args: dict | None = None, approval_id: str | None = None,
                wait: float | None = None, job: str | None = None) -> dict:
        """`args` are the action's typed arguments (see actions.py). An approval is bound to
        (action, port, args) after defaults are filled in. `job`: collect a run that outlasted an earlier call."""
        started = time.monotonic()
        if job:
            j = self._jobs.get(job)
            return self._collect(j, started) if j else {"status": "unknown_job", "job": job}
        go: tuple | None = None
        wait = self.approval_wait if wait is None else wait
        try:
            spec = A.get(action)
            args = spec.normalize(args)
            from .codec import until_pattern

            until_pattern(args.get("until"))  # a pattern that does not compile is said now, not after an approval
        except (A.ArgError, ValueError) as e:
            return {"status": "invalid_args", "message": str(e)}
        with self._lock:
            try:
                port_id = self.resolve_port(port)
            except KeyError as e:
                return {"status": "unknown_port", "message": str(e.args[0])}
            refused = self._outside_session(port_id)
            if refused:
                return refused
            if self._run_owns(self.bench.ports[port_id].device_id):
                return {"status": "busy", "message": "a managed program owns this device; stop or wait for that job"}
            kind = self.bench.ports[port_id].kind
            if spec.port_kinds and kind not in spec.port_kinds:
                return {"status": "invalid_args", "message": f"{action} works on {'/'.join(spec.port_kinds)} ports, not on a {kind} port"}
            proto = protocols.get(spec.protocol) if spec.protocol else None
            if proto:
                # what will actually be sent, resolved now (a remembered command's line ending): the approval binds to
                # it, and that is what runs; a definition changed while the human decides does not ride on their OK
                args = proto.resolve(self.bench.ports[port_id], args)
            key = A.canonical(args)
            try:
                impact = classify(self.bench, action, port_id, args, port_open=self._held_open(port_id, args))
            except A.ArgError as e:  # a SCPI line that is not one plain message
                return {"status": "invalid_args", "message": str(e)}
            # a tool not installed yet is downloaded only with the human's OK, even for a SAFE action
            fetch = self._fetch_needed(spec)
            if not fetch and (impact.effect == Effect.SAFE or
                              (approval_id is None and self.approvals.covered(action, port_id, impact.effect, impact.subjects))):
                go = (impact, args, "safe" if impact.effect == Effect.SAFE else "allowed by the human for the session"
                      if not self.approvals.allow_all else "allowed by the human: allow all", {})
            if go:
                pass  # runs below, outside the lock: nothing to ask
            elif approval_id is None:
                approval_id = self.approvals.open(impact, key, fetch=fetch).id
            else:
                # an id for another call: say so now, not after waiting on a card that shows something else
                asked = self.approvals.asked_for(approval_id)
                if asked is None:
                    return {"status": "unknown_approval", "approval_id": approval_id}
                if asked != (action, port_id, key):
                    return {"status": "approval_mismatch", "approval_id": approval_id,
                            "message": f"{approval_id} was asked for {asked[0]} on {asked[1]} with {asked[2]}; call without "
                                       "approval_id to ask for this one"}
        if go:  # (the lock is let go first: a long run must not hold up the human's selections and the agent's reads)
            g_impact, g_args, g_allowed, g_extra = go
            return self._start(g_impact, g_args, g_allowed, extra=g_extra, started=started)
        assert approval_id is not None  # (opened above, or the agent's own)

        t0 = time.monotonic()
        status = self.approvals.wait(approval_id, wait)  # the human decides in the panel (S3)
        waited = round(time.monotonic() - t0, 1)

        with self._lock:
            if status == "pending":
                return {"status": "approval_required", "approval_id": approval_id, "impact": impact.to_dict(),
                        "tell_user": "Waiting for a decision in the benchscout panel. Call again with this "
                                     "approval_id to keep waiting."}
            if status != "granted":
                return {"status": status, "approval_id": approval_id}
        if fetch:  # the human's OK covers the download its card named: it runs now, in the background
            dl = self._download(spec.argv[0])
            if not dl.done.wait(self.download_wait):
                return {"status": "downloading", "approval_id": approval_id, "download": dl.summary(),
                        "tell_user": f"benchscout is downloading {dl.summary()} (the human allowed it). It keeps going; "
                                     "call again with this approval_id to continue."}
            if dl.error:
                return {"status": "download_failed", "approval_id": approval_id, "message": dl.error,
                        "tell_user": "The download failed; the approval still stands, so calling again with this "
                                     "approval_id tries again."}
        with self._lock:
            refused = self._outside_session(port_id)  # the device may have left the session meanwhile
            if refused:
                return refused
            used = self.approvals.consume(approval_id, action, port_id, key, impact.effect.name.lower())
            if used != "ok":
                return {"status": used, "approval_id": approval_id}
        # say so, or the agent reports a long call as "ran without approval" (real bench, S5)
        return self._start(impact, args, "allowed by the human, once", started=started,
                           extra={"approval": {"id": approval_id, "granted_by": "the human, in the benchscout panel",
                                               "waited_s": waited}})

    def _start(self, impact: Impact, args: dict, allowed: str, extra: dict | None = None, started: float = 0.0) -> dict:
        """Run it outside the broker's lock, one at a time on each device. If it outlasts what one call may wait (a flash
        write can take minutes; the agent's call gives up), say it is running and give a job id to collect it with."""
        with self._lock:
            if impact.port_id not in self.bench.ports:
                return {"status": "resource_changed"}
            refused = self._outside_session(impact.port_id)
            if refused:
                return refused
            dev = self.bench.ports[impact.port_id].device_id
            turn = self._device_turn(dev)
            if not turn.acquire(blocking=False):
                return {"status": "busy", "device": dev}
        job = _Job(impact.action, impact.port_id, extra or {})

        def work() -> None:
            try:
                job.result = self._run(impact, args, allowed)
            except Exception as e:  # handed to whoever collects it
                job.error = e
            finally:
                turn.release()
                job.done.set()

        threading.Thread(target=work, name=f"run {impact.action}", daemon=True).start()
        return self._collect(job, started or time.monotonic())

    def _collect(self, job: "_Job", started: float) -> dict:
        left = self.call_budget - (time.monotonic() - started)
        if job.done.wait(max(left, 1.0)):
            self._jobs.pop(job.id, None)
            if job.error:
                raise job.error
            return dict(job.result or {}, **job.extra)
        self._jobs[job.id] = job
        return {"status": "running", "job": job.id, "action": job.action, "port": job.port,
                "running_s": round(time.monotonic() - job.t0),
                "tell_user": f"{job.action} is still running. Call bench_do again with job={job.id} to wait for its result; "
                             "do not start it again."}

    def _device_turn(self, device: str) -> threading.Lock:
        with self._lock:
            return self._turns.setdefault(device, threading.Lock())

    def _outside_session(self, port_id: str) -> dict | None:
        dev = self.bench.ports[port_id].device_id
        if dev in self.selected:
            return None
        return {"status": "not_selected", "device": dev,
                "message": f"{self.bench.devices[dev].name} is not in this session. Ask the user to select it "
                           f"in the benchscout panel; only selected devices can be used."}

    def _run(self, impact: Impact, args: dict, allowed: str) -> dict:
        port = self.bench.ports.get(impact.port_id)
        if port is not None and impact.action == self._watch_action(port) and impact.effect != Effect.SAFE:
            self.watch_approved.add(impact.port_id)  # the human accepted that opening it may reset the device
        self.done.append({"at": time.time(), "time": time.strftime("%H:%M:%S"), "action": impact.action, "port": impact.port_id,
                          "device": port.device_id if port else None, "args": A.canonical(args), "allowed": allowed})
        return {"status": "done", "impact": impact.to_dict(), "result": self._execute(impact.action, impact.port_id, args)}

    def _done_on(self, device: str | None, within: float | None = None) -> list[str]:
        """What benchscout did on this device's ports (the last `within` seconds), one line each, oldest first."""
        now = time.time()
        return [f"{d['time']} {d['action']} {d['args']} on {d['port']} ({d['allowed']})" for d in list(self.done)
                if d["device"] == device and (within is None or now - d["at"] <= within)]

    def _execute(self, action: str, port_id: str, args: dict):
        """Run what was judged and allowed. The gate does not know how: a tool defined as data runs as data says, and
        every other operation is its protocol's (protocols/), found by name."""
        from . import protocols

        port = self.bench.ports[port_id]
        spec = A.get(action)
        if spec.argv:
            return self._run_tool(spec, port, args)
        op = protocols.OPERATIONS.get(action)
        if op is None:
            return {"error": f"{action} is classified but nothing runs it"}
        return op(self, port, args)

    # ---- external tools defined as data (design.md section 4) ------------------------------
    def _download(self, exe: str) -> "_Download":
        """The download of `exe`: the one already running or done, or a new one (also after a failed one)."""
        with self._lock:
            job = self._downloads.get(exe)
            if job is None or job.error:
                job = self._downloads[exe] = _Download(exe, self._emit)
            return job

    def _usable_tool(self, spec) -> tuple[str | None, str | None]:
        """(path, why not): on PATH and new enough, else fetched before (fetch.py)."""
        from . import fetch

        exe = self._which(spec.argv[0])
        problem = self._version_problem(spec.version, exe) if exe and spec.version else None
        if exe and not problem:
            return exe, None
        got = fetch.cached(spec.argv[0])
        if got:
            return got, None
        return None, problem or f"{spec.argv[0]} is not installed"

    def _fetch_needed(self, spec) -> str:
        """What will be downloaded before this action can run (text for the approval card), or ""."""
        from . import fetch

        if not spec.argv or self._usable_tool(spec)[0]:
            return ""
        p = fetch.plan(spec.argv[0])
        return fetch.describe(p) if p else ""

    def _run_tool(self, spec, port, args: dict) -> dict:
        from . import fetch

        exe, problem = self._usable_tool(spec)
        if not exe and fetch.plan(spec.argv[0]):
            try:  # the human approved this action, and its card said what would be downloaded
                exe = fetch.get(spec.argv[0])
            except fetch.FetchError as e:
                return {"error": str(e)}
        if not exe:
            return {"error": problem, "fix": f"install {spec.argv[0]} and make sure it is on PATH, or add a pinned "
                                              f"[get.{spec.argv[0]}] for this platform in a knowledge file"}
        ws = self.state.parent
        dev = self.bench.devices[port.device_id]

        def path(p) -> str:
            f = (ws / p) if not Path(p).is_absolute() else Path(p)
            if not f.is_file():
                raise FileNotFoundError(f"{p} (looked in {ws})")
            return str(f.resolve())

        try:
            argv = [exe]
            for a in spec.argv[1:]:
                if a == "{port.select*}":  # how this tool reaches this port, from the USB data
                    sel = port.tools.get(spec.argv[0])
                    if not sel:
                        return {"error": f"no way is known to reach {port.id} with {spec.argv[0]}",
                                "fix": f"add `tools = {{ {spec.argv[0]} = [...] }}` for this USB id in a knowledge file"}
                    argv += sel
                    continue
                if a.startswith("{") and a.endswith("*}"):  # a list argument expands to several
                    for off, f in args[a[1:-2]]:
                        argv += [hex(off), path(f)]
                    continue
                val = a.replace("{port.path}", port.path or "").replace("{device.serial}", dev.serial or "")
                for p in spec.params:
                    if "{" + p.name + "}" in val:
                        v = args.get(p.name)
                        val = val.replace("{" + p.name + "}", path(v) if p.type is A.FilePath else str(v))
                argv.append(val)
        except FileNotFoundError as e:
            return {"error": f"file not found: {e}"}
        # hand the device over: the tool opens it itself (design.md section 3); take it back after
        watched = [(p.id, self.watches[p.id].baud) for p in self.bench.ports_of(dev.id) if p.id in self.watches] \
            if spec.releases_device else []
        for pid, _ in watched:
            self._unwatch(pid)
        try:
            text, code = run_tool(argv, spec.timeout, cwd=ws)
        finally:
            for pid, baud in watched:  # back to watching, if the device is still in the session and still there
                if dev.id in self.selected and pid in self.bench.ports and self.bench.ports[pid].device_id == dev.id:
                    self._watch(pid, baud)
        res = self._save_artifact(f"{port.id}-{spec.name}.txt", " ".join(argv) + "\n\n" + text, preview=text[-2000:])
        if spec.probe:  # read as its definition says: the parts it named go on the bench (the tool's own database)
            parts = R.read_parts(spec.probe, text)
            got = {"probe": "chain", "result": parts, "source": spec.probe.get("source", spec.argv[0]),
                   "time": time.strftime("%Y-%m-%d %H:%M")}
            self.recon.heard[port.id] = [g for g in self.recon.heard.get(port.id, []) if g["probe"] != "chain"] + [got]
            R._fold(self.bench, self.bench.targets_via(port.id), got, port=port)
            res["parts"] = [dict(p, idcode=f"0x{p['idcode']:08X}") for p in parts]
        return dict(res, returncode=code, argv=argv)

    @staticmethod
    def _which(exe: str) -> str | None:
        from .fetch import find

        return find(exe)  # the person's own, then shipped with benchscout, then fetched before

    def _version_problem(self, v: dict, exe: str | None = None) -> str | None:
        import re

        exe = exe or self._which(v["argv"][0])
        try:
            text, code = run_tool([exe or v["argv"][0], *v["argv"][1:]], timeout=30)
        except OSError as e:
            return f"could not check the version of {v['argv'][0]}: {e}"
        if code is None:
            return f"could not check the version of {v['argv'][0]}: it did not answer within 30 s"
        m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
        if not m:
            return None
        have = tuple(int(x or 0) for x in m.groups())
        need = tuple(int(x) for x in (v["min"].split(".") + ["0", "0"])[:3])
        if have < need:
            return f"{v['argv'][0]} {'.'.join(map(str, have))} is too old; need {v['min']} or newer: {v.get('why', '')}"
        return None

    def _remember(self, port_id: str, text: str | None) -> None:
        if not (text and text.strip()):
            return
        import time

        from . import knowledge as K

        stamp = (time.strftime("%H:%M:%S"), text)
        c = K.classify_log(text, self.bench.devices[self.bench.ports[port_id].device_id].line_rules)
        for t in self.bench.targets_via(port_id):
            kept = self.observed.setdefault(t.id, {})
            kept["latest"] = stamp
            if c["alerts"]:
                kept["alert"] = stamp

    def new_artifact(self, stem: str, ext: str) -> Path:
        """A new file in the workspace's artifacts, never one written before (a second listen must not overwrite the
        first: an agent noticed). One way for every kind of output (text, a capture, a tool's run)."""
        import time

        stem = stem.replace(":", "_")
        p = self.artifacts / f"{time.strftime('%Y%m%d-%H%M%S')}-{stem}.{ext}"
        n = 1
        while p.exists():
            n += 1
            p = self.artifacts / f"{time.strftime('%Y%m%d-%H%M%S')}-{stem}-{n}.{ext}"
        return p

    def _save_artifact(self, name: str, content: str, preview: str) -> dict:
        stem, _, ext = name.rpartition(".")
        p = self.new_artifact(stem, ext)
        p.write_text(content, encoding="utf-8")
        return {"saved_to": str(p.resolve()), "preview": preview}
