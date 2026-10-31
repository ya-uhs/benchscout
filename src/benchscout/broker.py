"""L2 broker: owns ports, enforces policy, keeps the approval queue.

The agent never touches /dev/cu.* directly. It asks the broker; SAFE actions run,
anything else becomes a pending approval the human grants out of band
(`benchscout approve <id>`), so the gate does not depend on which backend runs.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import recon as R
from .io import Io
from .policy import Effect, classify


class Broker:
    def __init__(self, io: Io, state_dir: str | Path = ".benchscout"):
        self.io = io
        self.state = Path(state_dir)
        self.state.mkdir(parents=True, exist_ok=True)
        self.artifacts = self.state / "artifacts"
        self.artifacts.mkdir(exist_ok=True)
        self._leases: dict[str, str] = {}
        self._lock = threading.Lock()
        self.last: R.Recon | None = None

    # ---- recon ---------------------------------------------------------------
    def scan(self) -> R.Recon:
        self.last = R.run(self.io)
        (self.state / "inventory.json").write_text(json.dumps(self.last.to_dict(), indent=2, default=str))
        return self.last

    @property
    def bench(self):
        return (self.last or self.scan()).bench

    # ---- approvals -----------------------------------------------------------
    def _approvals_path(self) -> Path:
        return self.state / "approvals.json"

    def approvals(self) -> dict:
        p = self._approvals_path()
        return json.loads(p.read_text()) if p.exists() else {}

    def _save(self, a: dict) -> None:
        self._approvals_path().write_text(json.dumps(a, indent=2))

    def decide(self, approval_id: str, granted: bool) -> dict:
        a = self.approvals()
        if approval_id not in a:
            raise KeyError(approval_id)
        a[approval_id]["status"] = "granted" if granted else "denied"
        self._save(a)
        return a[approval_id]

    # ---- leases --------------------------------------------------------------
    def _acquire(self, port_id: str, holder: str) -> None:
        with self._lock:
            if self._leases.get(port_id) not in (None, holder):
                raise RuntimeError(f"{port_id} is held by {self._leases[port_id]}")
            self._leases[port_id] = holder

    def _release(self, port_id: str) -> None:
        with self._lock:
            self._leases.pop(port_id, None)

    # ---- the one entry point the agent uses -----------------------------------
    def request(self, action: str, port_id: str, arg: str | None = None, approval_id: str | None = None) -> dict:
        impact = classify(self.bench, action, port_id, arg)
        if impact.effect > Effect.SAFE:
            a = self.approvals()
            if approval_id:
                rec = a.get(approval_id)
                if not rec or rec["status"] != "granted":
                    return {"status": rec["status"] if rec else "unknown_approval", "approval_id": approval_id}
                if (rec["action"], rec["port"], rec.get("arg")) != (action, port_id, arg):
                    return {"status": "approval_mismatch", "approval_id": approval_id}
                rec["status"] = "used"
                self._save(a)
            else:
                new_id = uuid.uuid4().hex[:6]
                a[new_id] = {"action": action, "port": port_id, "arg": arg, "status": "pending",
                             "impact": impact.to_dict(), "created": time.time()}
                self._save(a)
                return {
                    "status": "approval_required",
                    "approval_id": new_id,
                    "impact": impact.to_dict(),
                    "tell_user": f"Approve with: benchscout approve {new_id}",
                }
        return {"status": "done", "impact": impact.to_dict(), "result": self._execute(action, port_id, arg)}

    def _execute(self, action: str, port_id: str, arg: str | None):
        port = self.bench.ports[port_id]
        self._acquire(port_id, action)
        try:
            if action == "serial.listen":
                secs = float(arg) if arg else 3.0
                text = R.listen_into(self.io, port.path, self.bench.targets_via(port_id), secs)
                self.last.proposals = R.propose(self.bench)  # what we heard may change the next steps
                if text is None:
                    return {"error": "port disappeared while listening; the device likely reset or re-enumerated"}
                return self._save_artifact(f"{port_id}-listen.txt", text, preview=text[-2000:])
            if action in ("scpi.query", "scpi.write"):
                return self.io.visa_query(port.path, arg or "*IDN?")
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
        finally:
            self._release(port_id)

    def _save_artifact(self, name: str, content: str, preview: str) -> dict:
        p = self.artifacts / name
        p.write_text(content)
        return {"saved_to": str(p.resolve()), "preview": preview}
