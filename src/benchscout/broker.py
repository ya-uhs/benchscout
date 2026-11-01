"""L2 broker: owns ports, enforces policy, holds approvals and the session's selection.

Two kinds of caller, two sets of methods (design.md section 3 and 15 R3):

- the agent (through the MCP relay): `scan`, `impact`, `request`
- the human (through the control channel only): `select`, `decide`

Approvals live in memory (`ApprovalBook`); nothing on disk grants anything.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from . import actions as A
from . import recon as R
from .approvals import ApprovalBook, Scope
from .io import Io
from .locks import DeviceLocks
from .policy import Effect, Impact, classify

RECENT_FOR_QUOTES = 2 * 1024 * 1024  # how much recent output a quote is checked against


class Broker:
    def __init__(self, io: Io, state_dir: str | Path = ".benchscout", *, locks: DeviceLocks | None = None,
                 on_event: Callable[[dict], None] | None = None, approval_wait: float = 0.0):
        self.io = io
        self.state = Path(state_dir)
        self.state.mkdir(parents=True, exist_ok=True)
        from . import knowledge as K

        K.load(self.state.parent)  # shipped + user + this workspace's .benchscout/knowledge/
        self.artifacts = self.state / "artifacts"
        self.artifacts.mkdir(exist_ok=True)
        self.locks = locks
        self.on_event = on_event  # set by the core to forward events to the control channel
        self.approval_wait = approval_wait  # seconds a request waits for the human (S3)
        self.approvals = ApprovalBook(self.state / "audit.jsonl", on_event=self._emit)
        from .questions import QuestionBook

        self.questions = QuestionBook(on_event=self._emit)
        self.questions.on_answer = self._on_answer
        self.selected: set[str] = set()  # device ids in this session
        self.watches: dict = {}  # port id -> PortWatch (selected devices' ports held open)
        self.watch_offers: dict[str, str] = {}  # port id -> approval id: benchscout asked the human to watch it
        self.watch_declined: set[str] = set()  # ports the human said not to watch, this session
        from .memory import Memory

        self.memory = Memory(self.state.parent)  # design.md section 6
        # Output heard from each target, kept across re-scans so what was learned is not lost
        # when the graph is rebuilt (design.md section 10). Per target: the latest output, plus
        # the latest one that named the part and the latest one that raised an alert, so a later
        # quiet answer does not wipe them (real bench: `hang` after `panic` lost the ESP32-S3).
        self.observed: dict[str, dict[str, tuple[str, str]]] = {}  # target id -> kind -> (time, text)
        self._lock = threading.RLock()
        self.last: R.Recon | None = None

    def _emit(self, event: dict) -> None:
        if self.on_event:
            self.on_event(event)

    # ---- recon ---------------------------------------------------------------
    def scan(self) -> R.Recon:
        with self._lock:
            busy = self.locks.holder if self.locks else None
            self.last = R.run(self.io, busy=busy)
            for tid, kept in self.observed.items():
                if tid not in self.last.bench.targets:
                    continue
                seen_texts = []
                for kind in ("identity", "alert", "latest"):
                    if kind in kept and kept[kind][1] not in seen_texts:
                        seen_texts.append(kept[kind][1])
                        R.apply_log([self.last.bench.targets[tid]], kept[kind][1], seen=kept[kind][0])
            self._apply_memory(self.last.bench)
            self.last.proposals = R.propose(self.last.bench)
            (self.state / "inventory.json").write_text(json.dumps(self.last.to_dict(), indent=2, default=str))
        self._emit({"event": "scanned", "devices": list(self.last.bench.devices)})
        return self.last

    @property
    def bench(self):
        return (self.last or self.scan()).bench

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
            return classify(self.bench, action, self.resolve_port(port), A.get(action).normalize(args))

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
        if ref in devs:
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
        from dataclasses import asdict

        wait = self.approval_wait if wait is None else wait
        q = self.questions.wait(question_id, 0) if question_id else self.questions.ask(text, options)
        if q is None:
            return {"status": "unknown_question", "question_id": question_id}
        q = self.questions.wait(q.id, wait)
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

        a = self.approvals.decide(approval_id, granted, scope)
        if a.by == "benchscout" and a.action == "serial.watch" and self.watch_offers.get(a.port) == a.id:
            if granted:  # benchscout asked for itself: nobody is waiting on the call, so run it here
                threading.Thread(target=self._start_offered_watch, args=(a.id, a.port), daemon=True).start()
            else:
                self.watch_declined.add(a.port)
        return asdict(a)

    # ---- memory (design.md section 6) ---------------------------------------------------------
    def _key(self, dev) -> str:
        from .memory import device_key

        return device_key(dev.vid, dev.pid, dev.serial)

    def _header(self, dev) -> dict:
        from . import knowledge as K

        return {"device": dev.name, "vid": f"{dev.vid or 0:04x}", "pid": f"{dev.pid or 0:04x}", "serial": dev.serial or "",
                "shared_serial": dev.serial in K.SHARED_SERIALS}

    def _apply_memory(self, bench) -> None:
        """What was learned before shows up in the first read, without an LLM."""
        from . import knowledge as K
        from .model import Confidence, Evidence

        self.patterns: dict[str, list[dict]] = {}
        for dev in bench.devices.values():
            shared = dev.serial in K.SHARED_SERIALS
            targets = [t for p in bench.ports_of(dev.id) for t in bench.targets_via(p.id)]
            for e in self.memory.entries(self._key(dev)):
                if e.status != "active":
                    continue
                when = f"{e.time[:10]}, {e.source}"
                if e.kind == "identity" and e.target in bench.targets:
                    t = bench.targets[e.target]
                    if t.confidence != Confidence.IDENTIFIED:
                        t.name, t.confidence = e.text, Confidence.IDENTIFIED
                    t.evidence.append(Evidence("learned", f"{e.text}: \"{e.evidence}\" ({when})"))
                elif e.kind == "safety":
                    if shared:
                        continue  # another unit of this model may answer to the same serial: never loosen safety on it
                    if e.target in bench.ports and e.data.get("listen_safe"):
                        bench.ports[e.target].may_reset_on_open = False
                elif e.kind == "interpretation" and e.data.get("pattern"):
                    self.patterns.setdefault(dev.id, []).append(e.data)
                else:
                    for t in targets[:1]:
                        t.facts.append(f"remembered: {e.text} ({when})")

    def _observe_open(self, port_id: str, text: str | None) -> None:
        """benchscout's own observation, written without asking: opening a port that may reset did
        not make it vanish. After two, propose (pending the human) treating listening as SAFE."""
        from .io import VANISHED_MARK
        from .memory import Entry

        port = self.bench.ports[port_id]
        if not port.may_reset_on_open or text is None or VANISHED_MARK in text:
            return
        dev = self.bench.devices[port.device_id]
        key = self._key(dev)
        self.memory.add(key, Entry(kind="observation", source="benchscout", target=port_id,
                                   text=f"opened {port.path} and it did not vanish (no re-enumeration seen)",
                                   time=time.strftime("%Y-%m-%dT%H:%M:%S")), self._header(dev))
        entries = self.memory.entries(key)
        seen = sum(1 for e in entries if e.kind == "observation" and e.target == port_id and "did not vanish" in e.text)
        if seen >= 2 and not any(e.kind == "safety" and e.target == port_id and e.status in ("pending", "active", "rejected") for e in entries):
            e = self.memory.add(key, Entry(kind="safety", source="benchscout", target=port_id, status="pending",
                                           text=f"Listening to {port.path} did not reset {dev.name} ({seen} times). Treat listening as SAFE?",
                                           data={"listen_safe": True}), self._header(dev))
            self._memory_event(dev, e)

    def _memory_event(self, dev, entry) -> None:
        from dataclasses import asdict

        self._emit({"event": "memory_added", "key": self._key(dev), "device": dev.id, "device_name": dev.name, "entry": asdict(entry)})

    def remember(self, ref: str, kind: str, text: str, evidence: str = "", data: dict | None = None, source: str = "agent") -> dict:
        """Written without asking (design.md 6). Safety-loosening entries wait for the human."""
        from .memory import Entry

        with self._lock:
            dev, target = self._resolve_ref(ref)
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
        self._memory_event(dev, e)
        return {"status": "pending_human" if e.status == "pending" else "remembered", "id": e.id}

    def recall(self, ref: str | None = None) -> list[dict]:
        from dataclasses import asdict

        devs = [self._resolve_ref(ref)[0]] if ref else [self.bench.devices[d] for d in sorted(self.selected)] or list(self.bench.devices.values())
        return [{"device": d.name, "key": self._key(d), "entries": [asdict(e) for e in self.memory.entries(self._key(d))]} for d in devs]

    def decide_memory(self, key: str, entry_id: str, accept: bool) -> dict:
        """Control channel only: the human confirms (or rejects) a safety-loosening entry."""
        from dataclasses import asdict

        e = self.memory.set_status(key, entry_id, "active" if accept else "rejected")
        if accept and e.kind == "safety" and self.last and e.target in self.last.bench.ports and e.data.get("listen_safe"):
            self.last.bench.ports[e.target].may_reset_on_open = False
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
        return {"status": "identified", "target": tid, "found_in": where}

    def read_log(self, port: str, lines: int = 200, contains: str | None = None) -> dict:
        """The end of a watched port's log (SAFE: it reads a file benchscout wrote)."""
        pid = self.resolve_port(port)
        log = self.state / "logs" / (pid.replace(":", "_") + ".log")
        if not log.exists():
            return {"status": "no_log", "message": f"{pid} has not been watched in this folder yet; use serial.watch"}
        text = log.read_bytes()[-RECENT_FOR_QUOTES:].decode("utf-8", errors="replace").splitlines()
        if contains:
            text = [l for l in text if contains in l]
        tail = text[-max(1, min(lines, 2000)):]
        return {"status": "done", "log": str(log.resolve()), "lines": len(tail), "text": "\n".join(tail)}

    def _find_quote(self, target_id: str, quote: str) -> str | None:
        texts: list[tuple[str, str]] = []
        for p in self.bench.ports_reaching(target_id):
            w = self.watches.get(p.id)
            if w:
                texts.append(("the watched log", w.recent(RECENT_FOR_QUOTES)))
            log = self.state / "logs" / (p.id.replace(":", "_") + ".log")
            if log.exists():
                texts.append((log.name, log.read_bytes()[-RECENT_FOR_QUOTES:].decode("utf-8", errors="replace")))
            for f in sorted(self.artifacts.glob(f"*{p.id.replace(':', '_')}*"))[-20:]:
                texts.append((f.name, f.read_text(errors="replace")))
        for kind, (when, text) in self.observed.get(target_id, {}).items():
            texts.append((f"output seen {when}", text))
        for ev in self.bench.targets[target_id].evidence:
            texts.append((ev.source, ev.detail))
        return next((where for where, text in texts if quote in text), None)

    # ---- watching (design.md section 17) -------------------------------------------------
    def _autowatch(self) -> list[str]:
        """Selected devices' serial ports that are SAFE to open are watched right away. For the
        others benchscout asks the human itself (an approval card), since watching is what lets
        it notice crashes and resets; the agent does not have to think of it."""
        started = []
        for d in self.selected:
            for p in self.bench.ports_of(d):
                if not (p.kind == "serial" and p.path and p.id not in self.watches):
                    continue
                if classify(self.bench, "serial.watch", p.id).effect == Effect.SAFE:
                    if self._watch(p.id, 115200):
                        started.append(p.id)
                        self._watching_event(p.id)  # the human sees which ports are watched
                else:
                    self._offer_watch(p.id)
        return started

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
        args = A.get("serial.watch").normalize({"baud": 115200})
        impact = classify(self.bench, "serial.watch", port_id, args)
        dev = self.bench.devices[self.bench.ports[port_id].device_id]
        a = self.approvals.open(impact, A.canonical(args), by="benchscout", subject=dev.name,
                                purpose="Hold the port open so benchscout sees everything it prints and notices crashes and resets.")
        self.watch_offers[port_id] = a.id

    def _start_offered_watch(self, approval_id: str, port_id: str) -> None:
        r = self.request("serial.watch", port_id, {"baud": 115200}, approval_id=approval_id, wait=0)
        w = self.watches.get(port_id)
        if r.get("status") != "done" or not w:
            return  # _watch already said why (unwatchable), or the device left the session
        self._watching_event(port_id)

        def settled() -> None:  # it did not vanish when opened: benchscout's own observation (design.md 6)
            if self.watches.get(port_id) is w and w.alive and w.vanishes == 0:
                self._observe_open(port_id, "")

        threading.Timer(3.0, settled).start()

    def watch_state(self) -> dict[str, str]:
        """For the agent: how each selected device's serial port is watched, in words."""
        out = {}
        for d in sorted(self.selected):
            for p in self.bench.ports_of(d):
                if p.kind != "serial" or not p.path:
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
        w = PortWatch(self.io, port_id, port.path, self.state / "logs", self._device_event, baud, port.open_lines,
                      patterns=getattr(self, "patterns", {}).get(port.device_id, []))
        try:
            w.start()
        except Exception as e:  # busy elsewhere, gone: a fact, not a crash
            self._emit({"event": "device_event", "port": port_id, "device": port.device_id, "kind": "unwatchable", "what": str(e)})
            return False
        self.watches[port_id] = w
        return True

    def _unwatch(self, port_id: str) -> None:
        w = self.watches.pop(port_id, None)
        if w:
            w.stop()

    def _device_event(self, ev: dict) -> None:
        port = self.last.bench.ports.get(ev["port"]) if self.last else None
        if port:
            ev["device"] = port.device_id
            ev["device_name"] = self.last.bench.devices[port.device_id].name
        self._emit(ev)

    def end_session(self) -> None:
        for pid in list(self.watches):
            self._unwatch(pid)
        with self._lock:
            if self.locks:
                self.locks.release_all()
            self.selected = set()
            self.approvals.end_session()

    # ---- the one entry point the agent uses -----------------------------------
    def request(self, action: str, port: str, args: dict | None = None, approval_id: str | None = None,
                wait: float | None = None) -> dict:
        """`args` are the action's typed arguments (see actions.py). An approval is bound to
        (action, port, args) after defaults are filled in."""
        wait = self.approval_wait if wait is None else wait
        try:
            spec = A.get(action)
            args = spec.normalize(args)
        except A.ArgError as e:
            return {"status": "invalid_args", "message": str(e)}
        if not spec.implemented:
            return {"status": "not_implemented", "message": f"{action} is classified but not implemented yet"}
        key = A.canonical(args)
        with self._lock:
            try:
                port_id = self.resolve_port(port)
            except KeyError as e:
                return {"status": "unknown_port", "message": str(e.args[0])}
            refused = self._outside_session(port_id)
            if refused:
                return refused
            kind = self.bench.ports[port_id].kind
            if spec.port_kinds and kind not in spec.port_kinds:
                return {"status": "invalid_args", "message": f"{action} works on {'/'.join(spec.port_kinds)} ports, not on a {kind} port"}
            impact = classify(self.bench, action, port_id, args)
            w = self.watches.get(port_id)
            if w and w.alive and action in ("serial.listen", "serial.watch"):
                # the port is already open: listening opens nothing, so it cannot reset anything
                impact = Impact(action, port_id, Effect.SAFE, "the port is already open (watched); reading it opens nothing")
            if impact.effect == Effect.SAFE or (approval_id is None and self.approvals.covered(action, port_id, impact.effect)):
                return self._run(impact, args)
            if approval_id is None:
                approval_id = self.approvals.open(impact, key).id

        import time

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
            refused = self._outside_session(port_id)  # the device may have left the session meanwhile
            if refused:
                return refused
            used = self.approvals.consume(approval_id, action, port_id, key)
            if used != "ok":
                return {"status": used, "approval_id": approval_id}
            # say so, or the agent reports a long call as "ran without approval" (real bench, S5)
            return dict(self._run(impact, args), approval={"id": approval_id, "granted_by": "the human, in the benchscout panel",
                                                           "waited_s": waited})

    def _outside_session(self, port_id: str) -> dict | None:
        dev = self.bench.ports[port_id].device_id
        if dev in self.selected:
            return None
        return {"status": "not_selected", "device": dev,
                "message": f"{self.bench.devices[dev].name} is not in this session. Ask the user to select it "
                           f"in the benchscout panel; only selected devices can be used."}

    def _run(self, impact: Impact, args: dict) -> dict:
        return {"status": "done", "impact": impact.to_dict(), "result": self._execute(impact.action, impact.port_id, args)}

    def _execute(self, action: str, port_id: str, args: dict):
        port = self.bench.ports[port_id]
        spec = A.get(action)
        if spec.argv:
            return self._run_tool(spec, port, args)
        w = self.watches.get(port_id)
        if w and w.alive and action in ("serial.listen", "serial.send") and args["baud"] != w.baud:
            # a different rate was asked for: reopen the watch at it (the port is SAFE to open, it was watched)
            self._unwatch(port_id)
            w = self.watches.get(port_id) if self._watch(port_id, args["baud"]) else None
        if action == "serial.watch":
            if w and w.alive:
                return {"watching": port_id, "log": str(w.log.resolve()), "already": True}
            self._unwatch(port_id)
            if not self._watch(port_id, args["baud"]):
                return {"error": "could not open the port to watch it"}
            return {"watching": port_id, "log": str(self.watches[port_id].log.resolve())}
        if action == "serial.listen" and w and w.alive:
            text = w.read_window(args["seconds"])
            R.apply_log(self.bench.targets_via(port_id), text)
            self.last.proposals = R.propose(self.bench)
            self._remember(port_id, text)
            return dict(self._save_artifact(f"{port_id}-listen-{args['baud']}.txt", text, preview=text[-2000:]),
                        watched_log=str(w.log.resolve()))
        if action == "serial.send" and w and w.alive:
            text = w.send((args["data"] + args["eol"]).encode(), args["seconds"])
            R.apply_log(self.bench.targets_via(port_id), text)
            self.last.proposals = R.propose(self.bench)
            self._remember(port_id, text)
            safe_name = "".join(c if c.isalnum() else "_" for c in args["data"])[:24]
            return self._save_artifact(f"{port_id}-send-{safe_name}.txt", text, preview=text[-2000:])
        if action == "serial.listen":
            text = R.listen_into(self.io, port.path, self.bench.targets_via(port_id), args["seconds"],
                                 exclusive=True, baud=args["baud"], lines=port.open_lines)
            self.last.proposals = R.propose(self.bench)  # what we heard may change the next steps
            self._remember(port_id, text)
            self._observe_open(port_id, text)
            if text is None:
                detail = next((f for t in self.bench.targets_via(port_id) for f in reversed(t.facts) if "port disappeared" in f), "")
                return {"error": "port disappeared while listening; the device likely reset or re-enumerated",
                        "detail": detail}
            return self._save_artifact(f"{port_id}-listen-{args['baud']}.txt", text, preview=text[-2000:])
        if action == "serial.send":
            text = self.io.serial_listen(port.path, args["seconds"], args["baud"], True, port.open_lines,
                                         send=(args["data"] + args["eol"]).encode())
            R.apply_log(self.bench.targets_via(port_id), text)  # the answer can name the part too
            self.last.proposals = R.propose(self.bench)
            self._remember(port_id, text)
            safe_name = "".join(c if c.isalnum() else "_" for c in args["data"])[:24]
            return self._save_artifact(f"{port_id}-send-{safe_name}.txt", text, preview=text[-2000:])
        if action in ("scpi.query", "scpi.write"):
            return self.io.visa_query(port.path, args["command"])
        if action == "jtag.scan":
            dev = self.bench.devices[port.device_id]
            return [dict(d, idcode=f"0x{d['idcode']:08X}") for d in self.io.jtag_scan(dev.serial)]
        return {"error": f"{action} is classified but not implemented in this prototype"}

    # ---- external tools defined as data (design.md section 4) ------------------------------
    def _run_tool(self, spec, port, args: dict) -> dict:
        exe = self._which(spec.argv[0])
        if not exe:
            return {"error": f"{spec.argv[0]} is not installed", "fix": f"install {spec.argv[0]} and make sure it is on PATH"}
        if spec.version:
            problem = self._version_problem(spec.version)
            if problem:
                return {"error": problem}
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
            out = subprocess.run(argv, capture_output=True, text=True, timeout=spec.timeout, cwd=ws)
            text, code = (out.stdout or "") + (out.stderr or ""), out.returncode
        except subprocess.TimeoutExpired as e:
            text, code = f"{e}", None
        finally:
            for pid, baud in watched:
                self._watch(pid, baud)
        res = self._save_artifact(f"{port.id}-{spec.name}.txt", " ".join(argv) + "\n\n" + text, preview=text[-2000:])
        return dict(res, returncode=code, argv=argv)

    @staticmethod
    def _which(exe: str) -> str | None:
        found = shutil.which(exe)
        if found:
            return found
        for d in (Path.home() / ".local" / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin")):
            if (d / exe).exists():
                return str(d / exe)
        return None

    def _version_problem(self, v: dict) -> str | None:
        import re

        exe = self._which(v["argv"][0])
        try:
            out = subprocess.run([exe or v["argv"][0], *v["argv"][1:]], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"could not check the version of {v['argv'][0]}: {e}"
        m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", out.stdout + out.stderr)
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
        c = K.classify_log(text)
        for t in self.bench.targets_via(port_id):
            kept = self.observed.setdefault(t.id, {})
            kept["latest"] = stamp
            if c["exact"]:
                kept["identity"] = stamp
            if c["alerts"]:
                kept["alert"] = stamp

    def _save_artifact(self, name: str, content: str, preview: str) -> dict:
        # One file per capture: a second listen must not overwrite the first (an agent noticed this).
        import time

        stem, _, ext = name.replace(":", "_").rpartition(".")
        p = self.artifacts / f"{time.strftime('%Y%m%d-%H%M%S')}-{stem}.{ext}"
        n = 1
        while p.exists():
            n += 1
            p = self.artifacts / f"{time.strftime('%Y%m%d-%H%M%S')}-{stem}-{n}.{ext}"
        p.write_text(content)
        return {"saved_to": str(p.resolve()), "preview": preview}
