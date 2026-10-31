"""benchscout CLI.

  benchscout scan [--json]            recon the bench (safe probes only)
  benchscout approvals                list pending approvals
  benchscout approve|deny <id>        decide on one
  benchscout mcp                      run the MCP broker on stdio
  benchscout setup                    make this folder a bench workspace for plain `claude`
  benchscout start [--backend claude] [--lang ja]  scan, set up skills + MCP, launch the agent

Set BENCHSCOUT_FIXTURE=fixtures/bench_example.json to run without hardware.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

from .broker import Broker
from .io import FixtureIo, LiveIo
from .report import render

# How to start each backend with an opening prompt. {prompt} = opening prompt,
# {mcp} = path to a standalone mcp.json. Override with BENCHSCOUT_BACKEND_CMD.
# Claude Code needs no flags: setup_workspace() writes .mcp.json and enables it, so the
# same wiring is there again on `claude --resume`.
BACKENDS = {
    "claude": "claude {prompt}",
}

OPENING_PROMPT = (
    "Use the bench-expert skill. Scan the bench with bench_scan and give me your opening "
    "read: what is connected, what is unknown, anything alarming, and up to three next steps. "
    "Write your reply in {language}."
)

LANGUAGE_NAMES = {"ja": "Japanese", "en": "English", "zh": "Chinese", "ko": "Korean", "de": "German", "fr": "French"}


def user_language(explicit: str | None = None) -> str:
    """--lang, then BENCHSCOUT_LANG, then LANG/LC_ALL, then the macOS UI language."""
    code = explicit or os.environ.get("BENCHSCOUT_LANG") or os.environ.get("LC_ALL") or os.environ.get("LANG")
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


def _io():
    f = os.environ.get("BENCHSCOUT_FIXTURE")
    return FixtureIo(f) if f else LiveIo()


def _state() -> str:
    return os.environ.get("BENCHSCOUT_STATE", ".benchscout")


def cmd_scan(a) -> None:
    r = Broker(_io(), _state()).scan()
    print(json.dumps(r.to_dict(), indent=2, default=str) if a.json else render(r))


def cmd_approvals(a) -> None:
    pending = {k: v for k, v in Broker(_io(), _state()).approvals().items() if v["status"] == "pending"}
    if not pending:
        print("No pending approvals.")
    for k, v in pending.items():
        imp = v["impact"]
        also = f"; also disturbs {', '.join(imp['affected_ports'])}" if imp["affected_ports"] else ""
        print(f"{k}  [{imp['effect'].upper()}] {v['action']} on {v['port']} {v.get('arg') or ''}\n        {imp['reason']}{also}")


def cmd_decide(a, granted: bool) -> None:
    rec = Broker(_io(), _state()).decide(a.id, granted)
    print(f"{a.id}: {rec['status']} ({rec['action']} on {rec['port']})")


def _merge_json(path: Path, update) -> None:
    data = json.loads(path.read_text()) if path.exists() else {}
    update(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def setup_workspace(cwd: Path, state: Path) -> Path:
    """Make `cwd` a bench workspace that plain `claude` / `claude --resume` picks up:
    project skill, project MCP server (.mcp.json), and that server pre-enabled."""
    skill_src = resources.files("benchscout") / "skills" / "bench-expert" / "SKILL.md"
    skill_dst = cwd / ".claude/skills/bench-expert/SKILL.md"
    skill_dst.parent.mkdir(parents=True, exist_ok=True)
    skill_dst.write_text(skill_src.read_text())

    env = {"BENCHSCOUT_STATE": str(state)}
    if os.environ.get("BENCHSCOUT_FIXTURE"):
        env["BENCHSCOUT_FIXTURE"] = str(Path(os.environ["BENCHSCOUT_FIXTURE"]).resolve())
    server = {"command": shutil.which("benchscout") or sys.argv[0], "args": ["mcp"], "env": env}

    def add_server(d):
        d.setdefault("mcpServers", {})["benchscout"] = server

    def enable(d):
        servers = d.setdefault("enabledMcpjsonServers", [])
        if "benchscout" not in servers:
            servers.append("benchscout")
        allow = d.setdefault("permissions", {}).setdefault("allow", [])
        if "mcp__benchscout" not in allow:
            allow.append("mcp__benchscout")

    _merge_json(cwd / ".mcp.json", add_server)
    _merge_json(cwd / ".claude/settings.local.json", enable)
    mcp_cfg = state / "mcp.json"  # standalone copy for backends that take a config path
    mcp_cfg.parent.mkdir(parents=True, exist_ok=True)
    mcp_cfg.write_text(json.dumps({"mcpServers": {"benchscout": server}}, indent=2))
    return mcp_cfg


def cmd_setup(a) -> None:
    mcp_cfg = setup_workspace(Path.cwd(), Path(_state()).resolve())
    print(f"Bench workspace ready in {Path.cwd()} (MCP config also at {mcp_cfg}).")
    print("Open it with `claude`, or continue a session with `claude --resume`.")


def cmd_start(a) -> None:
    state = Path(_state()).resolve()
    broker = Broker(_io(), state)
    r = broker.scan()
    print(render(r))
    print()
    mcp_cfg = setup_workspace(Path.cwd(), state)

    template = os.environ.get("BENCHSCOUT_BACKEND_CMD") or BACKENDS.get(a.backend)
    if not template:
        sys.exit(f"Unknown backend {a.backend}; set BENCHSCOUT_BACKEND_CMD")
    prompt = OPENING_PROMPT.format(language=user_language(a.lang))
    cmd = template.format(mcp=shlex.quote(str(mcp_cfg)), prompt=shlex.quote(prompt))
    if a.dry_run:
        print("Would run:", cmd)
        return
    os.execvp("sh", ["sh", "-c", cmd])


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="benchscout")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_scan)
    sub.add_parser("approvals").set_defaults(fn=cmd_approvals)
    sub.add_parser("setup", help="write skill + MCP config here so plain `claude` works").set_defaults(fn=cmd_setup)
    for name, granted in (("approve", True), ("deny", False)):
        p = sub.add_parser(name)
        p.add_argument("id")
        p.set_defaults(fn=lambda a, g=granted: cmd_decide(a, g))
    sub.add_parser("mcp").set_defaults(fn=lambda a: __import__("benchscout.mcp_server").mcp_server.main())
    st = sub.add_parser("start")
    st.add_argument("--backend", default="claude")
    st.add_argument("--dry-run", action="store_true")
    st.add_argument("--lang", help="reply language code, e.g. ja or en (default: from the OS)")
    st.set_defaults(fn=cmd_start)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
