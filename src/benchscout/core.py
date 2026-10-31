"""benchscout core: one per workspace (VS Code window), started by its controller.

Two channels (design.md section 3, 15 R3/S1):

- control channel = this process's stdin/stdout, owned by whoever started it (the VS Code
  extension; a developer's terminal for now). JSON lines both ways. Approvals and device
  selection come only from here. When stdin closes, the core shuts down and frees the devices.
- agent channel = a Unix socket that `benchscout mcp` relays to. It offers the agent's
  methods only; there is no method that approves or selects anything.

Control messages:   {"id": 1, "method": "select", "params": {"devices": ["..."]}}
                    -> {"id": 1, "result": {...}}  or  {"id": 1, "error": "..."}
Events:             {"event": "approval_requested", ...}
A developer can also type shorthand lines: scan | select <dev>... | approve <id> [once|session|<N>m]
| deny <id> | pending | inventory | quit
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from typing import IO, Any

from .broker import Broker
from .report import render

# ---- agent channel --------------------------------------------------------------

AGENT_METHODS = ("scan", "inventory", "impact", "do")


def agent_dispatch(broker: Broker, method: str, params: dict) -> Any:
    if method == "scan":
        r = broker.scan()
        return {"text": render(r), "selected": sorted(broker.selected)}
    if method == "inventory":
        return (broker.last or broker.scan()).to_dict()
    if method == "impact":
        return broker.impact(params["action"], params["port"], params.get("arg")).to_dict()
    if method == "do":
        return broker.request(params["action"], params["port"], params.get("arg"), params.get("approval_id"))
    raise ValueError(f"no method {method!r} on the agent channel (available: {', '.join(AGENT_METHODS)})")


class _AgentHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        for raw in self.rfile:
            msg_id = None
            try:
                msg = json.loads(raw)
                msg_id = msg.get("id")
                reply = {"id": msg_id, "result": agent_dispatch(self.server.broker, msg["method"], msg.get("params") or {})}
            except Exception as e:  # report to the agent instead of dropping the connection
                reply = {"id": msg_id, "error": f"{type(e).__name__}: {e}"}
            self.wfile.write((json.dumps(reply, default=str) + "\n").encode())
            self.wfile.flush()


class AgentServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, broker: Broker):
        self.broker = broker
        super().__init__(str(path), _AgentHandler)
        os.chmod(path, 0o600)


def claim_socket(path: Path) -> None:
    """Refuse to start a second core for the same workspace; clear a dead one's socket."""
    if not path.exists():
        return
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(str(path))
    except OSError:
        path.unlink()
        return
    finally:
        s.close()
    raise RuntimeError(f"a benchscout core is already running for this workspace ({path})")


# ---- control channel ------------------------------------------------------------

def parse_scope(word: str | None):
    if not word or word == "once":
        return "once"
    if word == "session":
        return "session"
    if word.endswith("m") and word[:-1].isdigit():
        return int(word[:-1])
    raise ValueError(f"scope must be once, session or <N>m, not {word!r}")


def parse_shorthand(line: str) -> dict:
    words = line.split()
    cmd, args = words[0], words[1:]
    if cmd in ("approve", "deny"):
        if not args:
            raise ValueError(f"usage: {cmd} <approval_id>")
        params = {"approval_id": args[0], "granted": cmd == "approve"}
        if cmd == "approve":
            params["scope"] = parse_scope(args[1] if len(args) > 1 else None)
        return {"method": "decide", "params": params}
    if cmd == "select":
        return {"method": "select", "params": {"devices": args}}
    if cmd in ("scan", "pending", "inventory", "quit"):
        return {"method": "shutdown" if cmd == "quit" else cmd, "params": {}}
    raise ValueError(f"unknown command {cmd!r}")


def control_dispatch(broker: Broker, method: str, params: dict) -> Any:
    if method == "scan":
        r = broker.scan()
        return {"text": render(r), "bench": r.bench.to_dict()}
    if method == "inventory":
        return (broker.last or broker.scan()).to_dict()
    if method == "select":
        return broker.select(params.get("devices") or [])
    if method == "decide":
        return broker.decide(params["approval_id"], bool(params["granted"]), params.get("scope", "once"))
    if method == "pending":
        return [asdict(a) for a in broker.approvals.pending()]
    raise ValueError(f"unknown control method {method!r}")


class Core:
    def __init__(self, broker: Broker, sock: Path, out: IO[str] = sys.stdout):
        self.broker = broker
        self.sock = sock
        self._out = out
        self._out_lock = threading.Lock()
        self.server: AgentServer | None = None

    def emit(self, msg: dict) -> None:
        with self._out_lock:
            self._out.write(json.dumps(msg, default=str) + "\n")
            self._out.flush()

    def start(self) -> None:
        claim_socket(self.sock)
        self.server = AgentServer(self.sock, self.broker)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.emit({"event": "ready", "socket": str(self.sock)})

    def handle_line(self, line: str) -> bool:
        """Handle one control line. Returns False when the controller asked to stop."""
        line = line.strip()
        if not line:
            return True
        msg_id = None
        try:
            msg = json.loads(line) if line.startswith("{") else parse_shorthand(line)
            msg_id = msg.get("id")
            if msg["method"] == "shutdown":
                self.emit({"id": msg_id, "result": "bye"})
                return False
            result = control_dispatch(self.broker, msg["method"], msg.get("params") or {})
            self.emit({"id": msg_id, "result": result})
        except Exception as e:
            self.emit({"id": msg_id, "error": f"{type(e).__name__}: {e}"})
        return True

    def run(self, inp: IO[str] = sys.stdin, initial_scan: bool = True) -> None:
        self.start()
        if initial_scan:  # the first read comes at once, before any LLM is involved (design.md 15 R4)
            self.handle_line('{"id": "initial", "method": "scan"}')
        try:
            for line in inp:
                if not self.handle_line(line):
                    break
        finally:
            self.stop()

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.sock.exists():
            self.sock.unlink()
        self.broker.end_session()
        self.emit({"event": "stopped"})
