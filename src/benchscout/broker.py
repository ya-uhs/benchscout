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
from pathlib import Path
from typing import Callable

from . import actions as A
from . import recon as R
from .approvals import ApprovalBook, Scope
from .io import Io
from .locks import DeviceLocks
from .policy import Effect, Impact, classify


class Broker:
    def __init__(self, io: Io, state_dir: str | Path = ".benchscout", *, locks: DeviceLocks | None = None,
                 on_event: Callable[[dict], None] | None = None, approval_wait: float = 0.0):
        self.io = io
        self.state = Path(state_dir)
        self.state.mkdir(parents=True, exist_ok=True)
        self.artifacts = self.state / "artifacts"
        self.artifacts.mkdir(exist_ok=True)
        self.locks = locks
        self.on_event = on_event  # set by the core to forward events to the control channel
        self.approval_wait = approval_wait  # seconds a request waits for the human (S3)
        self.approvals = ApprovalBook(self.state / "audit.jsonl", on_event=self._emit)
        self.selected: set[str] = set()  # device ids in this session
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
            self.selected = new
        out = {"selected": sorted(self.selected), "busy": busy}
        self._emit({"event": "selection_changed", **out})
        return out

    def _resolve_device(self, ref: str) -> str:
        b = self.bench
        devs = b.devices
        if ref in devs:
            return ref
        # also accept what people see: a port id or path (usbserial-10), or part of the name
        names = {d: [d, devs[d].name.lower()] + [x for p in b.ports_of(d) for x in (p.id, p.path or "")] for d in devs}
        hits = [d for d, keys in names.items() if any(ref.lower() in k.lower() for k in keys if k)]
        if len(hits) == 1:
            return hits[0]
        raise KeyError(f"device {ref!r} matches {len(hits)} devices; known: {', '.join(devs)}")

    def decide(self, approval_id: str, granted: bool, scope: Scope = "once") -> dict:
        from dataclasses import asdict

        return asdict(self.approvals.decide(approval_id, granted, scope))

    def end_session(self) -> None:
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
            impact = classify(self.bench, action, port_id, args)
            if impact.effect == Effect.SAFE or (approval_id is None and self.approvals.covered(action, port_id, impact.effect)):
                return self._run(impact, args)
            if approval_id is None:
                approval_id = self.approvals.open(impact, key).id

        status = self.approvals.wait(approval_id, wait)  # the human decides in the panel (S3)

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
            return self._run(impact, args)

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
        if action == "serial.listen":
            text = R.listen_into(self.io, port.path, self.bench.targets_via(port_id), args["seconds"],
                                 exclusive=True, baud=args["baud"])
            self.last.proposals = R.propose(self.bench)  # what we heard may change the next steps
            if text is None:
                return {"error": "port disappeared while listening; the device likely reset or re-enumerated"}
            return self._save_artifact(f"{port_id}-listen-{args['baud']}.txt", text, preview=text[-2000:])
        if action in ("scpi.query", "scpi.write"):
            return self.io.visa_query(port.path, args["command"])
        if action == "esptool.chip_id":
            exe = shutil.which("esptool.py") or shutil.which("esptool")
            if not exe:
                return {"error": "esptool not installed"}
            out = subprocess.run([exe, "--port", port.path, "chip_id"], capture_output=True, text=True, timeout=30)
            return out.stdout[-2000:] or out.stderr[-2000:]
        if action == "jtag.scan":
            dev = self.bench.devices[port.device_id]
            return [dict(d, idcode=f"0x{d['idcode']:08X}") for d in self.io.jtag_scan(dev.serial)]
        return {"error": f"{action} is classified but not implemented in this prototype"}

    def _save_artifact(self, name: str, content: str, preview: str) -> dict:
        p = self.artifacts / name.replace(":", "_")
        p.write_text(content)
        return {"saved_to": str(p.resolve()), "preview": preview}
