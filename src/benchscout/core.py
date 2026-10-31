"""benchscout core: one per workspace (VS Code window), started by its controller.

Two channels (design.md section 3, 15 R3/S1):

- control channel = this process's stdin/stdout, owned by whoever started it (the VS Code
  extension; a developer's terminal for now). JSON lines both ways. Approvals and device
  selection come only from here. When stdin closes, the core shuts down and frees the devices.
- agent channel = a Unix socket (named pipe on Windows) that `benchscout mcp` relays to. It offers the agent's
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
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from typing import IO, Any

from multiprocessing.connection import Client, Listener

from .broker import Broker
from .runtime import address_family
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


class AgentServer:
    """Serves the agent channel: a Unix socket on POSIX, a named pipe on Windows
    (multiprocessing.connection gives both, with message framing)."""

    def __init__(self, address: str, broker: Broker):
        self.address, self.family = address, address_family(address)
        self.broker = broker
        self._closing = False
        claim_address(address)
        self._listener = Listener(address, self.family)
        if self.family == "AF_UNIX":
            os.chmod(address, 0o600)

    def serve_forever(self) -> None:
        while not self._closing:
            try:
                conn = self._listener.accept()
            except OSError:
                break
            if self._closing:
                conn.close()
                break
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn) -> None:
        with conn:
            while True:
                try:
                    raw = conn.recv_bytes()
                except (EOFError, OSError):
                    return
                msg_id = None
                try:
                    msg = json.loads(raw)
                    msg_id = msg.get("id")
                    reply = {"id": msg_id, "result": agent_dispatch(self.broker, msg["method"], msg.get("params") or {})}
                except Exception as e:  # report to the agent instead of dropping the connection
                    reply = {"id": msg_id, "error": f"{type(e).__name__}: {e}"}
                try:
                    conn.send_bytes(json.dumps(reply, default=str).encode())
                except OSError:
                    return

    def close(self) -> None:
        self._closing = True
        try:  # wake a blocked accept(); closing alone does not on Windows
            Client(self.address, self.family).close()
        except OSError:
            pass
        self._listener.close()  # also removes the socket file on POSIX


def claim_address(address: str) -> None:
    """Refuse to start a second core for the same workspace; clear a dead one's socket."""
    family = address_family(address)
    try:
        Client(address, family).close()
    except OSError:
        if family == "AF_UNIX" and os.path.exists(address):
            os.unlink(address)
        return
    raise RuntimeError(f"a benchscout core is already running for this workspace ({address})")


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
    def __init__(self, broker: Broker, address: str | Path, out: IO[str] = sys.stdout):
        self.broker = broker
        self.address = str(address)
        self._out = out
        self._out_lock = threading.Lock()
        self.server: AgentServer | None = None

    def emit(self, msg: dict) -> None:
        with self._out_lock:
            self._out.write(json.dumps(msg, default=str) + "\n")
            self._out.flush()

    def start(self) -> None:
        self.server = AgentServer(self.address, self.broker)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.emit({"event": "ready", "address": self.address})

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
            self.server.close()
            self.server = None
        self.broker.end_session()
        self.emit({"event": "stopped"})
