"""`benchscout mcp`: the agent's MCP server. It relays to the core of this workspace.

It owns no broker, no ports and no approvals (docs/notes.md review R2/R3). Every
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
- Name a part only with bench_identify and a quote of the device's own output. Remember what you learn with bench_remember.
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
        watch = reply["result"].get("watch") or {}
        if watch:
            session += "\nSerial ports of the selected devices:\n" + "\n".join(f"- {p}: {w}" for p, w in watch.items())
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
and that approval_id. A tool used for the first time is downloaded after the human's OK; if
that takes longer than one call you get downloading (with how far it is): tell the user, and
call again the same way. Never try to reach the hardware any other way.
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
    async def bench_log(port: str, lines: int = 200, contains: str | None = None) -> str:
        """Read the end of a watched serial port's log (everything it printed since it was
        watched). Safe: it only reads the file. `contains` keeps only lines with that text."""
        return await _call("log", {"port": port, "lines": lines, "contains": contains})

    @mcp.tool()
    async def bench_identify(target: str, name: str, quote: str) -> str:
        """Name a part, quoting the device's own output that proves it (e.g. a ROM banner line).
        `target` is a port id/path or target id from bench_scan. benchscout checks the quote is
        really in what it captured from that device; if so the part is identified and remembered
        for next time, otherwise it is kept only as a guess. Never name a part without this."""
        return await _call("identify", {"target": target, "name": name, "quote": quote})

    @mcp.tool()
    async def bench_define_tool(tool: dict | None = None, evidence: str = "", probes: list[dict] | None = None,
                                proposal_id: str | None = None) -> str:
        """Propose an action for a tool benchscout ships nothing for, e.g. the FPGA or MCU vendor's own command-line
        programmer when openFPGALoader or esptool do not support the part or cable. Find the tool in the vendor's
        documentation and check with the user that it is installed (benchscout never downloads it). `tool`:
        {"name": "<tool>.<verb>", "argv": ["<program>", "...", "{bitstream}"], "effect": "safe"|"disruptive"|
        "dangerous" (by what it does to the device: reading an IDCODE is safe, a reset disruptive, writing SRAM or
        flash dangerous), "reason": one line, "doc": what it does, "args": {"bitstream": {"type": "file",
        "required": true, "ext": [".bit"]}}, "port_kinds": ["jtag"], "releases_device": true, "timeout": 300}.
        argv is run without a shell; it may use {port.path}, {device.serial} and the args. `evidence`: the vendor's
        page and the words that say what the command does. `probes`: for a programmer cable benchscout does not know,
        [{"vid": "0x1234", "pid": "0x5678", "probe": "jtag"}] so it becomes a port. The human confirms it in the
        inbox, seeing the exact command; then it is an action like any other (bench_do), under the same policy.
        If the wait runs out you get waiting: call again with the proposal_id."""
        return await _call("define_tool", {"tool": tool, "evidence": evidence, "probes": probes, "proposal_id": proposal_id},
                           timeout=APPROVAL_WAIT_S + 60)

    @mcp.tool()
    async def bench_remember(ref: str, kind: str, text: str, evidence: str = "", data: dict | None = None) -> str:
        """Remember something about a device for next time, without asking the human (they can
        edit or delete it in the panel). `ref` is a device/port/target from bench_scan. `kind`:
        observation (what it does), config (wiring, probe nets the human told you), interpretation
        (data {"pattern": regex, "alert": text}: a line that means trouble on this device; it will be
        watched for), command (a firmware command of one serial port, e.g. read from its `help`: data
        {"command": "status", "effect": "safe"|"disruptive"|"dangerous", "eol": "\\r\\n" if not "\\n"}; text says
        what it does; then run it with bench_do serial.command {"name": "status"}; or a SCPI command of one
        instrument, by its header, as its programming manual describes it: data {"command": "MEAS:CURR?", "effect":
        ...}; scpi.query/scpi.write then treat that header so on this instrument). kind "safety" (data
        {"listen_safe": true} for one port), a firmware command with effect "safe" and a SCPI command made to ask
        less than benchscout's default wait for the human to confirm (until then they ask as before); a stricter
        one applies at once. Quote evidence (the manual's words) when you have it."""
        return await _call("remember", {"ref": ref, "kind": kind, "text": text, "evidence": evidence, "data": data})

    @mcp.tool()
    async def bench_recall(ref: str | None = None) -> str:
        """What is remembered about a device (or the selected devices), with dates and sources."""
        return await _call("recall", {"ref": ref})

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

    from . import knowledge as K

    K.load(Path(workspace or os.getcwd()))  # so the action list includes the folder's own tools
    address = os.environ.get("BENCHSCOUT_ADDRESS") or agent_address(Path(workspace or os.getcwd()))[0]
    build_server(CoreClient(address), user_language()).run()
