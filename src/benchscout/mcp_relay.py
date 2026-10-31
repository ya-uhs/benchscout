"""`benchscout mcp`: the agent's MCP server. It relays to the core of this workspace.

It owns no broker, no ports and no approvals (design.md section 15 R2/R3). Every
agent (Copilot, Claude Code, ...) that starts it reaches the same core, the one the
human's panel controls. It never starts a core itself (S1): without one there is no
human channel, so it says how to start one instead.

The docstrings below are what the agent sees; keep them accurate.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import anyio
from mcp.server.mcpserver import MCPServer

from .runtime import socket_path

# How long a call that needs approval waits for the human in the panel (S3). Kept under
# typical client tool timeouts; on expiry the agent gets approval_required and can retry.
APPROVAL_WAIT_S = float(os.environ.get("BENCHSCOUT_APPROVAL_WAIT", "100"))

INSTRUCTIONS = """benchscout gives you the hardware bench. Rules that always hold:
- Reach hardware only through these tools. Never open serial ports, run esptool, openocd or SCPI scripts yourself.
- Never try to approve anything yourself. Approvals and device selection are made by the human in the benchscout panel.
- Only devices the human selected can be used; if a call returns not_selected, ask the human to select it.
- Name a part only when the evidence proves it; otherwise say it is unknown and list the facts.
- When a proposal or impact carries a reason (why:), quote it; do not invent another one."""

OPENING_PROMPT = (
    "Use benchscout. Scan the bench with bench_scan and give me your read: what is connected, what is "
    "unknown, anything alarming, and which device(s) you suggest I select, with up to three next steps "
    "for them. Reply in {language}."
)

NO_CORE = ("No benchscout core is running for this folder. Open the benchscout panel in VS Code "
           "(or run `benchscout core` in a terminal in this folder), then try again.")


class CoreClient:
    def __init__(self, sock: Path):
        self.sock = sock

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        """One request on a fresh connection. Returns {"result": ...} or {"error": ...}."""
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(timeout)
        try:
            s.connect(str(self.sock))
        except OSError:
            s.close()
            return {"error": NO_CORE}
        try:
            s.sendall((json.dumps({"id": 1, "method": method, "params": params or {}}) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    return {"error": "the benchscout core closed the connection"}
                buf += chunk
            return json.loads(buf)
        except TimeoutError:
            return {"error": f"the benchscout core did not answer within {timeout:.0f} s"}
        finally:
            s.close()


def build_server(client: CoreClient, language: str = "English") -> MCPServer:
    mcp = MCPServer("benchscout", instructions=INSTRUCTIONS)

    async def _call(method: str, params: dict | None = None, timeout: float = 30.0) -> str:
        reply = await anyio.to_thread.run_sync(lambda: client.call(method, params, timeout))
        if "error" in reply:
            return f"error: {reply['error']}"
        r = reply["result"]
        return r if isinstance(r, str) else json.dumps(r, default=str)

    @mcp.tool()
    async def bench_scan() -> str:
        """Re-scan the bench. Returns what is connected, what each thing is (with evidence),
        what is unknown, proposed next steps, and which devices the human selected for this
        session (only those can be used). Only safe probes run."""
        reply = await anyio.to_thread.run_sync(lambda: client.call("scan", timeout=60))
        if "error" in reply:
            return f"error: {reply['error']}"
        sel = reply["result"]["selected"]
        session = ("Selected for this session: " + ", ".join(sel)) if sel else \
            "No device is selected yet. Suggest which ones to select; the human selects them in the benchscout panel."
        return reply["result"]["text"] + "\n\n" + session

    @mcp.tool()
    async def bench_inventory_json() -> str:
        """Full bench graph (devices, ports, targets, links) as JSON from the last scan."""
        return await _call("inventory")

    @mcp.tool()
    async def bench_impact(action: str, port: str, arg: str | None = None) -> str:
        """Explain what an action would disturb before doing it. `port` is a port id
        (serial:usbmodem1101) or its path (/dev/cu.usbmodem1101). Actions: serial.listen,
        serial.send, esptool.chip_id, jtag.scan, jtag.halt, fpga.program, scpi.query, scpi.write."""
        return await _call("impact", {"action": action, "port": port, "arg": arg})

    @mcp.tool()
    async def bench_do(action: str, port: str, arg: str | None = None, approval_id: str | None = None) -> str:
        """Perform an action on a port of a selected device. `port` is a port id or its path.
        For serial.listen, `arg` is the listen time in seconds (baud is 115200 for now).
        Safe actions run at once. Others wait while the human decides in the benchscout
        panel, then run or return denied. If the wait runs out you get approval_required with
        an approval_id: tell the user it is waiting in the panel, and call again with that
        approval_id to keep waiting. Never try to reach the hardware any other way."""
        return await _call("do", {"action": action, "port": port, "arg": arg, "approval_id": approval_id},
                           timeout=APPROVAL_WAIT_S + 60)

    @mcp.prompt()
    def bench_start() -> str:
        """Start a bench session: scan, read the bench, suggest which devices to select."""
        return OPENING_PROMPT.format(language=language)

    return mcp


def main(workspace: str | None = None) -> None:
    from .cli import user_language

    sock = Path(os.environ["BENCHSCOUT_SOCKET"]) if os.environ.get("BENCHSCOUT_SOCKET") else \
        socket_path(Path(workspace or os.getcwd()))
    build_server(CoreClient(sock), user_language()).run()
