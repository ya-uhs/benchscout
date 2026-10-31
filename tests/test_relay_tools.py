"""The extension must recognise every benchscout MCP tool, or it puts the agent's use of it to the
human as if it were a shell command (real bench, 2026-11-01: bench_recall was missing)."""

import re
from pathlib import Path

import anyio

from benchscout.mcp_relay import CoreClient, build_server

TYPES = Path(__file__).parent.parent / "vscode" / "src" / "agents" / "types.ts"


def test_extension_knows_every_relay_tool():
    server = build_server(CoreClient("/nonexistent"))
    tools = {t.name for t in anyio.run(server.list_tools)}
    listed = set(re.findall(r'"(\w+)"', TYPES.read_text().split("BENCHSCOUT_TOOLS = [", 1)[1].split("]", 1)[0]))
    assert tools == listed, f"in mcp_relay.py only: {tools - listed}; in types.ts only: {listed - tools}"
