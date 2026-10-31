"""benchscout CLI (development and testing; the product UI is the VS Code extension).

  benchscout scan [--json]                 recon the bench (safe probes only)
  benchscout core [--workspace DIR]        run the core for a folder; this terminal is the control channel
  benchscout mcp [--workspace DIR]         MCP server for agents; relays to that folder's core
  benchscout setup [--agent claude|copilot]  put the skill and MCP config into this folder

Set BENCHSCOUT_FIXTURE=fixtures/bench_example.json to run without hardware.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

from .broker import Broker
from .io import FixtureIo, LiveIo
from .report import render

LANGUAGE_NAMES = {"ja": "Japanese", "en": "English", "zh": "Chinese", "ko": "Korean", "de": "German", "fr": "French"}

# Where each agent reads project skills (design.md section 15 S4).
SKILL_DIRS = {"claude": ".claude/skills", "copilot": ".agents/skills"}


def user_language(explicit: str | None = None) -> str:
    """--lang, then BENCHSCOUT_LANG, then LANG/LC_ALL, then the Windows or macOS UI language."""
    code = explicit or os.environ.get("BENCHSCOUT_LANG") or os.environ.get("LC_ALL") or os.environ.get("LANG")
    if not code and sys.platform == "win32":
        import locale

        code = windows_locale_code(locale.getlocale()[0])
    if not code and sys.platform == "darwin":
        try:
            out = subprocess.run(["defaults", "read", "-g", "AppleLanguages"], capture_output=True, text=True).stdout
            code = next((l.strip(' ",()') for l in out.splitlines() if l.strip(' ",()')), None)
        except OSError:
            code = None
    code = (code or "en").split(".")[0].replace("_", "-").split("-")[0].lower()
    if code in ("c", "posix"):
        code = "en"
    return LANGUAGE_NAMES.get(code, code)


def windows_locale_code(name: str | None) -> str | None:
    """Windows reports 'Japanese_Japan' (or 'ja_JP'); map it to a language code."""
    if not name:
        return None
    head = name.split("_")[0].split("-")[0].lower()
    by_name = {v.lower(): k for k, v in LANGUAGE_NAMES.items()}
    return by_name.get(head, head)


def _io():
    f = os.environ.get("BENCHSCOUT_FIXTURE")
    return FixtureIo(f) if f else LiveIo()


def cmd_scan(a) -> None:
    r = Broker(_io(), Path.cwd() / ".benchscout").scan()
    print(json.dumps(r.to_dict(), indent=2, default=str) if a.json else render(r))


def cmd_core(a) -> None:
    from .core import Core
    from .locks import DeviceLocks, owner_info
    from .runtime import agent_address

    ws = Path(a.workspace).resolve()
    broker = Broker(_io(), ws / ".benchscout", locks=DeviceLocks(owner_info(ws)), approval_wait=a.wait)
    human = sys.stdin.isatty() and not a.json
    core = Core(broker, agent_address(ws)[0], human=human)
    broker.on_event = core.emit
    try:
        core.run()
    except KeyboardInterrupt:
        pass  # run() already stopped the core and released the devices


def cmd_mcp(a) -> None:
    from .mcp_relay import main

    main(a.workspace)


def setup_workspace(cwd: Path, agent: str = "claude") -> None:
    """Put the expert skill and the MCP relay config where `agent` reads them."""
    skill_src = resources.files("benchscout") / "skills" / "bench-expert" / "SKILL.md"
    skill_dst = cwd / SKILL_DIRS[agent] / "bench-expert" / "SKILL.md"
    skill_dst.parent.mkdir(parents=True, exist_ok=True)
    skill_dst.write_text(skill_src.read_text())

    command = shutil.which("benchscout") or sys.argv[0]
    args = ["mcp", "--workspace", str(cwd.resolve())]

    if agent == "claude":
        def add_server(d):
            d.setdefault("mcpServers", {})["benchscout"] = {"command": command, "args": args}

        def settings(d):
            servers = d.setdefault("enabledMcpjsonServers", [])
            if "benchscout" not in servers:
                servers.append("benchscout")
            # setup means "use benchscout here": undo an earlier disable from /mcp, which wins otherwise
            if "benchscout" in d.get("disabledMcpjsonServers", []):
                d["disabledMcpjsonServers"].remove("benchscout")
            perms = d.setdefault("permissions", {})
            allow = perms.setdefault("allow", [])
            if "mcp__benchscout" not in allow:
                allow.append("mcp__benchscout")
            # The agent must not drive benchscout itself, e.g. start its own core (design.md 15 R3).
            deny = perms.setdefault("deny", [])
            if "Bash(benchscout:*)" not in deny:
                deny.append("Bash(benchscout:*)")

        _merge_json(cwd / ".mcp.json", add_server)
        _merge_json(cwd / ".claude/settings.local.json", settings)
    else:
        def add_server(d):
            d.setdefault("servers", {})["benchscout"] = {"type": "stdio", "command": command, "args": args}

        _merge_json(cwd / ".vscode/mcp.json", add_server)


def _merge_json(path: Path, update) -> None:
    data = json.loads(path.read_text()) if path.exists() else {}
    update(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def cmd_setup(a) -> None:
    setup_workspace(Path.cwd(), a.agent)
    print(f"Bench workspace ready in {Path.cwd()} for {a.agent}.")
    print("Start the core here with `benchscout core` (this terminal approves), then open your agent.")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="benchscout")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_scan)
    c = sub.add_parser("core", help="run the core for a folder; stdin/stdout is the control channel")
    c.add_argument("--workspace", default=".")
    c.add_argument("--wait", type=float, default=100.0, help="seconds a call waits for a decision")
    c.add_argument("--json", action="store_true", help="JSON lines even at a terminal (what the extension uses)")
    c.set_defaults(fn=cmd_core)
    m = sub.add_parser("mcp", help="MCP server for agents; relays to the folder's core")
    m.add_argument("--workspace", default=None)
    m.set_defaults(fn=cmd_mcp)
    st = sub.add_parser("setup", help="write the skill and MCP config for an agent into this folder")
    st.add_argument("--agent", choices=sorted(SKILL_DIRS), default="claude")
    st.set_defaults(fn=cmd_setup)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
