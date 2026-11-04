"""Opaque execution plans and bounded, byte-preserving process capture.

This is a trusted-program boundary, not a sandbox. Files explicitly included in the
plan are checked again before launch; imports, network access and physical effects
of the program are not inferred by the harness.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


def digest(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


@dataclass(frozen=True)
class Plan:
    argv: tuple[str, ...]
    ports: tuple[tuple[str, str, str | None], ...]
    files: tuple[tuple[str, str], ...]
    cwd: str
    purpose: str
    timeout: float
    observe: bool
    max_bytes: int

    def canonical(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True, ensure_ascii=False)

    def verify(self) -> None:
        for name, expected in self.files:
            if digest(Path(name)) != expected:
                raise ValueError(f"input changed since approval: {name}")


def prepare(argv: list[str], ports: list[tuple[str, str, str | None]], files: list[str], cwd: Path,
            purpose: str, timeout: float, observe: bool, max_bytes: int) -> Plan:
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and "\0" not in a for a in argv):
        raise ValueError("argv must be a nonempty list of strings, executed without a shell")
    if not isinstance(purpose, str) or not purpose.strip():
        raise ValueError("purpose must explain what the program will do, including physical effects")
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 1 <= timeout <= 86400:
        raise ValueError("timeout must be 1..86400 seconds, including for observers")
    if not isinstance(observe, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= 256 * 1024 * 1024:
        raise ValueError("observe must be boolean; max_bytes must be 1..268435456")
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        raise ValueError("files must list input scripts and data files")
    cwd = cwd.resolve()
    candidate = Path(argv[0])
    exe = str((cwd / candidate).absolute()) if candidate.is_absolute() or "/" in argv[0] or "\\" in argv[0] else shutil.which(argv[0])
    if not exe or not Path(exe).is_file():
        raise ValueError("program is not installed; no program is downloaded by this operation")
    # The invocation path is meaningful (a virtual environment's interpreter is
    # often a symlink). Hash through it, but do not replace it with its target.
    resolved = [str(Path(exe).absolute()), *argv[1:]]
    inputs = {Path(resolved[0]), *((cwd / f).absolute() for f in files)}
    # Standalone file arguments are included automatically. Embedded options and
    # imported dependencies must be declared in files by the agent.
    for a in argv[1:]:
        try:
            p = (cwd / a).absolute()
            if p.is_file():
                inputs.add(p)
        except (OSError, ValueError):
            pass
    hashed = tuple((str(p), digest(p)) for p in sorted(inputs))
    return Plan(tuple(resolved), tuple(sorted(ports)), hashed, str(cwd), purpose, timeout, observe, max_bytes)


def kill_tree(p: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=10)
    else:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            # Some hosts return EPERM for an already-exited process group. Reap
            # the parent; if still alive, killing it must succeed before release.
            if p.poll() is None:
                p.kill()
    if p.poll() is None:
        p.kill()


class Run:
    def __init__(self, plan: Plan, artifacts: Path, emit: Callable[[dict], None]):
        self.id = uuid.uuid4().hex
        self.plan, self.emit = plan, emit
        self.directory = artifacts / self.id
        self.directory.mkdir()
        self.stdout, self.stderr = self.directory / "stdout.bin", self.directory / "stderr.bin"
        (self.directory / "plan.json").write_text(plan.canonical(), encoding="utf-8")
        self.cancel = threading.Event()
        self.done = threading.Event()
        self.status = "starting"
        self.code: int | None = None
        self.error = ""
        self.started = time.time()

    def view(self) -> dict:
        return {"job": self.id, "status": self.status, "exit_code": self.code, "error": self.error,
                "stdout": str(self.stdout), "stderr": str(self.stderr), "plan": str(self.directory / "plan.json"),
                "resources": [p[0] for p in self.plan.ports], "purpose": self.plan.purpose,
                "observe": self.plan.observe}

    def stop(self) -> None:
        self.cancel.set()
        if not self.done.wait(15):
            raise RuntimeError("process shutdown not confirmed; resource ownership retained")

    def work(self, release: Callable[[], None], finished: Callable[[], None]) -> None:
        p = None
        try:
            if self.cancel.is_set():
                self.status = "cancelled"
                return
            self.plan.verify()
            if self.cancel.is_set():
                self.status = "cancelled"
                return
            with self.stdout.open("wb") as out, self.stderr.open("wb") as err:
                p = subprocess.Popen(self.plan.argv, cwd=self.plan.cwd, stdin=subprocess.DEVNULL,
                                     stdout=out, stderr=err, start_new_session=os.name != "nt")
                self.status = "running"
                self.emit({"event": "run_changed", **self.view()})
                end = time.monotonic() + self.plan.timeout
                seen, notified = 0, 0.0
                while True:
                    exited = p.poll() is not None
                    size = self.stdout.stat().st_size + self.stderr.stat().st_size
                    now = time.monotonic()
                    if self.plan.observe and size > seen and (exited or now - notified >= 1):
                        self.emit({"event": "device_event", "kind": "observation", "port": self.plan.ports[0][0],
                                   "device": self.plan.ports[0][1], "what": self.plan.purpose,
                                   "log": str(self.stdout), "stderr": str(self.stderr), "job": self.id, "time": time.time()})
                        seen, notified = size, now
                    if self.cancel.is_set():
                        self.status = "cancelled"
                        break
                    if size > self.plan.max_bytes:
                        self.status = "output_limit"
                        break
                    if now >= end:
                        self.status = "timed_out"
                        break
                    if exited:
                        self.status = "completed" if p.returncode == 0 else "failed"
                        break
                    self.cancel.wait(0.05)
                # Also stop children of a parent that has already exited.
                kill_tree(p)
                self.code = p.wait(timeout=5)
        except Exception as e:
            self.error, self.status = str(e), "failed"
            if p is not None:
                try:
                    kill_tree(p)
                    p.wait(timeout=5)
                except Exception as cleanup:
                    self.error += f"; shutdown failed: {cleanup}"
        finally:
            if p is not None and p.poll() is None:
                self.status = "stop_failed"
                self.emit({"event": "run_changed", **self.view()})
                return  # do not release ownership without confirming the parent stopped
            try:
                (self.directory / "result.json").write_text(json.dumps(self.view()), encoding="utf-8")
            except OSError as e:
                self.error, self.status = f"could not save result: {e}", "failed"
            release()
            self.done.set()
            self.emit({"event": "run_changed", **self.view()})
            finished()
