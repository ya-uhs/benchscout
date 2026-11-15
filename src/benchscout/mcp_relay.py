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
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client
from pathlib import Path

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .actions import catalog as actions_catalog
from .runtime import address_family, agent_address, prove

# How long a call that needs approval waits for the human in the panel (S3). Kept under
# typical client tool timeouts; on expiry the agent gets approval_required and can retry.
APPROVAL_WAIT_S = float(os.environ.get("BENCHSCOUT_APPROVAL_WAIT", "100"))

INSTRUCTIONS = """benchscout gives you the hardware bench. Rules that always hold:
- You own device, transport, framing and command knowledge. Built-in actions are optional conveniences.
- For anything else, prepare a script/tool and use bench_run; register unknown addresses with bench_register_resource.
- External programs run with the user's OS privileges, not in a sandbox. Declare every resource and input file.
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

NOT_OURS = ("Something holds {address}, but it is not a benchscout core of this OS user (it cannot prove this user's "
            "key, or this user may not even open it), so nothing it says is used. Another account may have taken the "
            "address. Tell the user.")
NO_CORE = ("No benchscout core is running for this folder. Open the benchscout panel in VS Code "
           "(or run `benchscout core` in a terminal in this folder), then try again.")


class CoreClient:
    def __init__(self, address: str | Path):
        self.address = str(address)

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        """One request on a fresh connection. Returns {"result": ...} or {"error": ...}."""
        try:
            conn = Client(self.address, address_family(self.address))
        except PermissionError:  # something is there that this user may not open (Windows: another account's pipe)
            return {"error": NOT_OURS.format(address=self.address)}
        except OSError:
            return {"error": NO_CORE}
        with conn:
            try:
                prove(conn, server=False, seconds=min(timeout, 10.0))
            except (AuthenticationError, EOFError):  # it hung up rather than prove it: not ours either
                return {"error": NOT_OURS.format(address=self.address)}
            except OSError as e:  # the key itself (agent_key says what is wrong with it), or the channel
                return {"error": f"the agent channel failed: {e}"}
            try:
                conn.send_bytes(json.dumps({"id": 1, "method": method, "params": params or {}}).encode())
                if not conn.poll(timeout):
                    return {"error": f"the benchscout core did not answer within {timeout:.0f} s"}
                return json.loads(conn.recv_bytes())
            except (EOFError, OSError):
                return {"error": "the benchscout core closed the connection"}


def build_server(client: CoreClient, language: str = "English", catalog: str | None = None) -> MCPServer:
    """`catalog`: the actions as the core describes them (it reads the folder's knowledge only where VS Code trusts the
    folder); without one, the shipped and the person's own actions."""
    mcp = MCPServer("benchscout", instructions=INSTRUCTIONS)

    async def _call(method: str, params: dict | None = None, timeout: float = 30.0) -> str:
        reply = await anyio.to_thread.run_sync(lambda: client.call(method, params, timeout))
        if "error" in reply:  # a tool error (isError) that every agent reads as one: the MCP SDK's ToolError
            raise ToolError(reply["error"])
        r = reply["result"]
        return r if isinstance(r, str) else json.dumps(r, default=str)

    @mcp.tool()
    async def bench_scan() -> str:
        """Re-scan the bench. Returns what is connected, what each thing is (with evidence),
        what is unknown, proposed next steps, and which devices the human selected for this
        session (only those can be used). Only safe probes run, and none on the selected devices
        (they are in use: what their earlier probes got is shown again, marked as earlier; their
        live output is in the watch log). benchscout notices plugs and unplugs by itself."""
        reply = await anyio.to_thread.run_sync(lambda: client.call("scan", timeout=60))
        if "error" in reply:  # a tool error (isError) that every agent reads as one: the MCP SDK's ToolError
            raise ToolError(reply["error"])
        sel = reply["result"]["selected"]
        session = ("Selected for this session: " + ", ".join(sel)) if sel else \
            "No device is selected yet. Suggest which ones to select; the human selects them in the benchscout panel."
        if reply["result"].get("unplugged"):
            session += ("\nUnplugged for now (still selected; watched again when plugged back in; anything allowed for "
                        "it before was withdrawn): " + ", ".join(reply["result"]["unplugged"]))
        watch = reply["result"].get("watch") or {}
        if watch:
            session += "\nSerial ports of the selected devices:\n" + "\n".join(f"- {p}: {w}" for p, w in watch.items())
        return reply["result"]["text"] + "\n\n" + session

    @mcp.tool()
    async def bench_inventory_json() -> str:
        """Full bench graph (devices, ports, targets, links) as JSON from the last scan."""
        return await _call("inventory")

    catalog = catalog or actions_catalog()

    @mcp.tool()
    async def bench_recipe(operation: str = "list", name: str = "", plan: dict | None = None,
                           approval_id: str | None = None) -> str:
        """Save and reuse an execution/observation recipe in this workspace. Operations: list, save,
        get, restore, run, delete. save takes the same plan fields as bench_run (no approval_id).
        Registration metadata is saved too. restore only restores registrations and returns the plan;
        ask the human to select its devices before run. run uses fresh file hashes and the current
        selection and requires human approval, even if this recipe ran before. Retry run with the
        approval_id while the plan is unchanged. Nothing auto-starts when a workspace opens.
        The file is .benchscout/recipes.json; unreadable files are preserved, not overwritten."""
        return await _call("recipe", {"operation": operation, "name": name, "plan": plan,
                                      "approval_id": approval_id}, timeout=APPROVAL_WAIT_S + 60)

    @mcp.tool()
    async def bench_register_resource(address: str, label: str, device: str | None = None) -> str:
        """Register an opaque connection address for this session, without opening or probing it.
        label is a description, not an identification. For an existing device, pass its device id
        so all its ports share ownership. Otherwise a new resource device is shown for human selection.
        Exact addresses share ownership; the harness cannot detect aliases for the same physical resource.
        Use existing port ids when available. Registration grants no permission; request_devices next."""
        return await _call("register_resource", {"address": address, "label": label, "device": device})

    @mcp.tool()
    async def bench_run(resources: list[str], argv: list[str], purpose: str, files: list[str] | None = None,
                        timeout: float = 60, observe: bool = False, max_bytes: int = 16777216,
                        approval_id: str | None = None) -> str:
        """Run an agent-prepared program on selected resource/port ids. argv is literal, no implicit shell;
        cwd is this workspace. purpose explains intent and physical effects. Supply all script/input
        dependencies in files; executable and standalone file arguments are also hashed. Every execution
        needs one human approval tied to this exact plan and file hashes. Changes need a new approval.
        This is trusted code with the user's privileges, NOT a sandbox or a verifier of physical effects.
        You implement transport, framing, commands and interpretation in your program; no core plugin needed.
        Returns approval_required (retry the same plan with approval_id) or a job id (poll bench_job,
        never start again). stdout/stderr are saved as raw bytes in separate artifacts.
        observe=true notifies the agent when output arrives: emit only observations worth investigating.
        timeout bounds lifetime (1..86400 seconds). max_bytes stops excessive output (checked periodically,
        can overshoot). Stop via bench_job; deselection/session end also stops it. Do not daemonize or
        detach children: process-tree cleanup is best effort, not OS containment."""
        return await _call("run_program", {"resources": resources, "argv": argv, "purpose": purpose, "files": files,
                                          "timeout": timeout, "observe": observe, "max_bytes": max_bytes,
                                          "approval_id": approval_id}, timeout=APPROVAL_WAIT_S + 60)

    @mcp.tool()
    async def bench_job(job: str, stop: bool = False) -> str:
        """Read a bench_run or built-in operation job's state and raw artifact paths, or stop it. Stopping grants no further access.
        Terminal states: completed, failed, timed_out, output_limit, cancelled. An exit code alone does not
        prove the intended hardware effect. Read stdout/stderr and interpret them yourself."""
        return await _call("run_state", {"job": job, "stop": stop})

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
call again the same way. A run that outlasts one call (a flash write can take minutes) returns
running with a job id: call again with that `job` to wait for its result; never start it again.
Never try to reach the hardware any other way.
Actions:
{catalog}""")
    async def bench_do(action: str, port: str, args: dict | None = None, approval_id: str | None = None,
                       job: str | None = None) -> str:
        return await _call("do", {"action": action, "port": port, "args": args, "approval_id": approval_id, "job": job},
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
        watched). Safe: it only reads the file. `contains` keeps only lines with that text.
        `through_benchscout` lists what was done to this device through benchscout this session
        (time, action, arguments, who allowed it), so a crash right after a command sent through
        benchscout is not taken for a fault without saying so."""
        return await _call("log", {"port": port, "lines": lines, "contains": contains})

    @mcp.tool()
    async def bench_identify(target: str, name: str, quote: str) -> str:
        """Name a part, quoting the device's own output that proves it (e.g. a ROM banner line).
        `target` is a port id/path or target id from bench_scan. benchscout checks the quote is
        really in what it captured from that device, not counting what was sent to it and echoed
        back; if so the part is identified and remembered for next time, otherwise it is kept only
        as a guess. Never name a part without this."""
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
        observation (what it does), config (wiring, probe nets the human told you; kept for this folder only; data
        {"inside": "<target id>"} when this port's far end is implemented inside that target, e.g. a UART in an FPGA's
        fabric: then whatever disturbs that target is known to disturb this port too; {"baud": n} when you found the
        rate it talks at: it is watched at that rate from then on), interpretation
        (data {"pattern": regex, "alert": text}: a line that means trouble on this device; it will be
        watched for), command (a firmware command of one serial port, e.g. read from its `help`: data
        {"command": "status", "effect": "safe"|"disruptive"|"dangerous", "eol": "\\r\\n" if not "\\n", "until": regex for
        the line that ends its answer, e.g. the shell's prompt or "^(ok|err)", read from its output, "limits": [one per
        value after the command: null or {"min": n, "max": n}] when the effect holds only for some values}; text says
        what it does; then send it with bench_do serial.send {"data": "status"} (its line ending and end line come from
        what you remembered); or a SCPI command of one
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


def main(workspace: str | None = None, language: str = "English") -> None:
    """`language`: what the agent is asked to reply in (the CLI reads the person's; the relay imports nothing above it)."""
    from . import knowledge as K

    address = os.environ.get("BENCHSCOUT_ADDRESS") or agent_address(Path(workspace or os.getcwd()))[0]
    client = CoreClient(address)
    # the actions the core offers, the folder's own tools among them where the folder is trusted: the core decides
    # (design.md section 3, S2). The relay does not read the folder's files itself: in a folder VS Code does not trust,
    # their words would reach the agent as tool descriptions although the core leaves them out.
    reply = client.call("actions", timeout=5)
    catalog = reply.get("result") if isinstance(reply.get("result"), str) else None
    if catalog is None:
        K.load(None)  # no core yet: what is shipped and the person's own
    build_server(client, language, catalog).run()
