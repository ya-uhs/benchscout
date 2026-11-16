"""Drives benchscout from outside, the way its users do, and nothing else:

- the human: the core's control channel (`benchscout core --json`, JSON lines on its stdin/stdout)
- the agent: the MCP tools of `benchscout mcp` (JSON-RPC over its stdin/stdout)
- the hardware: a fake bench, a JSON file the core reads instead of the OS (BENCHSCOUT_FIXTURE); what reached it is
  written to a log file (BENCHSCOUT_FIXTURE_LOG), as a real device would have felt it

The program under test is BENCHSCOUT_BIN (default: `benchscout` on PATH). This file imports nothing from benchscout.
See INTERFACES.md for the messages.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

KIT = Path(__file__).resolve().parent
# the program under test: BENCHSCOUT_BIN, else the one in core/ (an exported kit), else `benchscout` on PATH
BIN = os.environ.get("BENCHSCOUT_BIN") or next(
    (str(p) for p in (KIT / "core" / ("benchscout.exe" if sys.platform == "win32" else "benchscout"),) if p.exists()),
    shutil.which("benchscout") or "benchscout")
# example benches and the knowledge files they assume: bench/ in an exported kit, fixtures/ in the repository
BENCHES = KIT / "bench" if (KIT / "bench").is_dir() else KIT.parent / "fixtures"


class CoreError(Exception):
    """The control channel answered a request with an error."""


class Lines:
    """A child process's stdout as JSON lines, read on a thread."""

    def __init__(self, proc: subprocess.Popen):
        self.proc, self.items, self.cond = proc, [], threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            with self.cond:
                self.items.append(item)
                self.cond.notify_all()

    def wait(self, match, timeout: float, what: str):
        end = time.monotonic() + timeout
        with self.cond:
            while True:
                found = next((m for m in self.items if match(m)), None)
                if found is not None:
                    return found
                left = end - time.monotonic()
                if left <= 0 or self.proc.poll() is not None and not any(match(m) for m in self.items):
                    raise TimeoutError(f"no {what} within {timeout} s (process {'ended' if self.proc.poll() is not None else 'running'})")
                self.cond.wait(min(left, 0.2))


class Bench:
    """The fake bench: a JSON file of what the OS and VISA report (INTERFACES.md, "The fake bench"). Writing a device
    into the file plugs it in, taking it out unplugs it (the core notices within a few seconds)."""

    def __init__(self, root: Path, data: dict):
        self.path, self.log = root / "bench.json", root / "seen.jsonl"
        self.data = data
        self.write()

    def write(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def seen(self, what: str | None = None, where: str | None = None) -> list[dict]:
        """What reached the bench so far, oldest first: {"what", "where", "data"} (INTERFACES.md)."""
        if not self.log.exists():
            return []
        out = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [s for s in out if (what is None or s["what"] == what) and (where is None or s["where"] == where)]

    def sent(self, where: str | None = None) -> list[str]:
        """What was written to serial ports (or to the port at path `where`): by opening the port, or through a watch
        that already holds it open."""
        return [s["data"] for s in self.seen(where=where) if s["what"] in ("serial_send", "serial_write")]

    def opened(self, where: str | None = None) -> list[str]:
        """Each time a serial port was opened (to listen, to send, or to hold it for a watch), its path."""
        return [s["where"] for s in self.seen(where=where) if s["what"] in ("serial_listen", "serial_send", "serial_stream")]


class Core:
    """The human's side: one core for one workspace. `call` sends a control request and returns its result."""

    def __init__(self, workspace: Path, env: dict, wait: float = 0):
        """`wait`: seconds an agent's call waits for the human before it answers approval_required (the agent then
        calls again with the approval id); 0 answers at once."""
        self.workspace = workspace
        # its stderr goes to a file: a pipe nobody reads fills up and stops the core
        self.stderr = open(workspace.parent / "core.stderr", "a", encoding="utf-8")
        self.proc = subprocess.Popen([BIN, "core", "--workspace", str(workspace), "--json", "--wait", str(wait)],
                                     stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self.stderr, env=env, text=True,
                                     encoding="utf-8", cwd=workspace)
        self.lines = Lines(self.proc)
        self._n = 0
        self.wait_event("ready", timeout=60)

    def call(self, method: str, timeout: float = 30, **params):
        self._n += 1
        msg_id = f"t{self._n}"
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps({"id": msg_id, "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        reply = self.lines.wait(lambda m: m.get("id") == msg_id, timeout, f"reply to {method}")
        if "error" in reply:
            raise CoreError(reply["error"])
        return reply["result"]

    def events(self, name: str | None = None) -> list[dict]:
        with self.lines.cond:
            return [m for m in self.lines.items if "event" in m and (name is None or m["event"] == name)]

    def wait_event(self, name: str, where=lambda e: True, timeout: float = 15) -> dict:
        return self.lines.wait(lambda m: m.get("event") == name and where(m), timeout, f"event {name}")

    def stop(self) -> int | None:
        """Closing the control channel stops the core (and frees its devices)."""
        if self.proc.poll() is None:
            try:
                assert self.proc.stdin is not None
                self.proc.stdin.close()
                self.proc.wait(20)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()
        self.stderr.close()
        return self.proc.returncode


class Agent:
    """The agent's side: `benchscout mcp` started in the workspace, spoken to as an MCP client does."""

    def __init__(self, workspace: Path, env: dict):
        self.proc = subprocess.Popen([BIN, "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, env=env, text=True, encoding="utf-8", cwd=workspace)
        self.lines = Lines(self.proc)
        self._n, self._lock = 0, threading.Lock()
        self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "blackbox", "version": "0"}}, 60)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, msg: dict) -> None:
        assert self.proc.stdin is not None
        with self._lock:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()

    def _rpc(self, method: str, params: dict, timeout: float) -> dict:
        with self._lock:
            self._n += 1
            n = self._n
        self._send({"jsonrpc": "2.0", "id": n, "method": method, "params": params})
        reply = self.lines.wait(lambda m: m.get("id") == n, timeout, f"MCP reply to {method}")
        if "error" in reply:
            raise CoreError(json.dumps(reply["error"]))
        return reply["result"]

    def tools(self) -> list[dict]:
        return self._rpc("tools/list", {}, 30)["tools"]

    def call(self, tool: str, timeout: float = 120, **arguments) -> tuple[str, bool]:
        """The tool's text, and whether MCP marked it an error (isError)."""
        result = self._rpc("tools/call", {"name": tool, "arguments": arguments}, timeout)
        return "".join(c.get("text", "") for c in result.get("content", [])), bool(result.get("isError"))

    def call_json(self, tool: str, timeout: float = 120, **arguments):
        """The tool's text read as JSON (most tools answer JSON; INTERFACES.md)."""
        text, error = self.call(tool, timeout, **arguments)
        if error:
            raise CoreError(text)
        return json.loads(text)

    def start(self, tool: str, timeout: float = 120, **arguments) -> "queue.Queue[tuple[str, bool]]":
        """Call a tool without waiting for it (one that waits for the human): `.get(timeout=...)` its answer."""
        q: queue.Queue = queue.Queue()
        threading.Thread(target=lambda: q.put(self.call(tool, timeout, **arguments)), daemon=True).start()
        return q

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)


class World:
    """A fake bench, a workspace, the core (the human's side) and the relay (the agent's side), each test its own.
    Memory, knowledge and the runtime folder are the test's own too: nothing of the person running it is read."""

    def __init__(self, bench: dict | str = "bench_example.json", approval_wait: float = 0, knowledge: bool = True):
        # short: a Unix socket's path is limited to about 100 bytes (macOS)
        self.root = Path(tempfile.mkdtemp(prefix="bb", dir=None if sys.platform == "win32" else "/tmp"))
        data = json.loads((BENCHES / bench).read_text(encoding="utf-8")) if isinstance(bench, str) else bench
        self.bench = Bench(self.root, data)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        empty = self.root / "no-knowledge"
        empty.mkdir()
        self.env = dict(os.environ, BENCHSCOUT_FIXTURE=str(self.bench.path), BENCHSCOUT_FIXTURE_LOG=str(self.bench.log),
                        BENCHSCOUT_RUNTIME=str(self.root / "rt"), BENCHSCOUT_MEMORY=str(self.root / "memory"),
                        BENCHSCOUT_KNOWLEDGE=str(BENCHES / "knowledge" if knowledge else empty),
                        BENCHSCOUT_TOOLS=str(self.root / "tools"), BENCHSCOUT_APPROVAL_WAIT=str(approval_wait),
                        BENCHSCOUT_LANG="en")
        self.approval_wait = approval_wait
        self.core: Core | None = None
        self.agent: Agent | None = None

    def start(self) -> "World":
        self.core = Core(self.workspace, self.env, self.approval_wait)
        self.agent = Agent(self.workspace, self.env)
        return self

    def restart(self) -> "World":
        """A new session on the same bench, memory and workspace (what was remembered stays; approvals do not)."""
        self.stop()
        return self.start()

    def stop(self) -> None:
        if self.agent:
            self.agent.stop()
        if self.core:
            self.core.stop()
        self.core = self.agent = None

    def close(self) -> None:
        self.stop()
        shutil.rmtree(self.root, ignore_errors=True)
