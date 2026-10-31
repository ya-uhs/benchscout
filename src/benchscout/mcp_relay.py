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
from multiprocessing.connection import Client
from pathlib import Path

import anyio
from mcp.server.mcpserver import MCPServer

from .actions import catalog as actions_catalog
from .runtime import address_family, agent_address

# How long a call that needs approval waits for the human in the panel (S3). Kept under
# typical client tool timeouts; on expiry the agent gets approval_required and can retry.
APPROVAL_WAIT_S = float(os.environ.get("BENCHSCOUT_APPROVAL_WAIT", "100"))

INSTRUCTIONS = """benchscout gives you the hardware bench. Rules that always hold:
- Reach hardware only through these tools. Never open serial ports, run esptool, openocd or SCPI scripts yourself.
- Never try to approve anything yourself. Approvals and device selection are made by the human in the benchscout panel.
- Only devices the human selected can be used; if a call returns not_selected, use request_devices to ask for them.
- When you need a decision from the human, call ask_user with short options instead of guessing or stopping.
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
    def __init__(self, address: str | Path):
        self.address = str(address)

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        """One request on a fresh connection. Returns {"result": ...} or {"error": ...}."""
        try:
            conn = Client(self.address, address_family(self.address))
        except OSError:
            return {"error": NO_CORE}
        with conn:
            try:
                conn.send_bytes(json.dumps({"id": 1, "method": method, "params": params or {}}).encode())
                if not conn.poll(timeout):
                    return {"error": f"the benchscout core did not answer within {timeout:.0f} s"}
                return json.loads(conn.recv_bytes())
            except (EOFError, OSError):
                return {"error": "the benchscout core closed the connection"}


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

    catalog = actions_catalog()

    @mcp.tool(description=f"""Explain what an action would disturb before doing it. `port` is a port id
(serial:usbmodem1101) or its path (/dev/cu.usbmodem1101). `args` are the action's arguments.
Actions:
{catalog}""")
    async def bench_impact(action: str, port: str, args: dict | None = None) -> str:
        return await _call("impact", {"action": action, "port": port, "args": args})

    @mcp.tool(description=f"""Perform an action on a port of a selected device. `port` is a port id or its
path. `args` is an object with the action's arguments, e.g. {{"seconds": 3, "baud": 9600}} for
serial.listen; unknown or out-of-range arguments are rejected with the valid ones listed.
Safe actions run at once. Others wait while the human decides in the benchscout panel, then
run or return denied. If the wait runs out you get approval_required with an approval_id:
tell the user it is waiting in the panel, and call again with the same action, port, args
and that approval_id. Never try to reach the hardware any other way.
Actions:
{catalog}""")
    async def bench_do(action: str, port: str, args: dict | None = None, approval_id: str | None = None) -> str:
        return await _call("do", {"action": action, "port": port, "args": args, "approval_id": approval_id},
                           timeout=APPROVAL_WAIT_S + 60)

    @mcp.tool()
    async def ask_user(question: str, options: list[str], question_id: str | None = None) -> str:
        """Ask the human a question with options to pick from; it appears as a card in the
        benchscout inbox, where they can also write their own reply. Use it whenever you need
        a decision (what to look at next, which hypothesis to test) instead of guessing.
        Keep options short (2-5). The call waits for the answer; if it returns status
        "waiting", call again with the question_id."""
        return await _call("ask", {"question": question, "options": options, "question_id": question_id},
                           timeout=APPROVAL_WAIT_S + 60)

    @mcp.tool()
    async def request_devices(devices: list[str], reason: str, question_id: str | None = None) -> str:
        """Ask the human to add devices to this session (only selected devices can be used).
        `devices` are device ids, port names or names from bench_scan. The human decides in the
        benchscout inbox; the call waits. Returns "selected" or "denied" (with their reply)."""
        return await _call("request_devices", {"devices": devices, "reason": reason, "question_id": question_id},
                           timeout=APPROVAL_WAIT_S + 60)

    @mcp.prompt()
    def bench_start() -> str:
        """Start a bench session: scan, read the bench, suggest which devices to select."""
        return OPENING_PROMPT.format(language=language)

    return mcp


def main(workspace: str | None = None) -> None:
    from .cli import user_language

    address = os.environ.get("BENCHSCOUT_ADDRESS") or agent_address(Path(workspace or os.getcwd()))[0]
    build_server(CoreClient(address), user_language()).run()
