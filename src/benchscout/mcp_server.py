"""MCP face of the broker. Backends (Claude Code, Copilot...) only see these tools."""

from __future__ import annotations

import json
import os

from mcp.server.mcpserver import MCPServer

from .broker import Broker
from .io import FixtureIo, LiveIo
from .report import render


def make_broker() -> Broker:
    fixture = os.environ.get("BENCHSCOUT_FIXTURE")
    io = FixtureIo(fixture) if fixture else LiveIo()
    return Broker(io, os.environ.get("BENCHSCOUT_STATE", ".benchscout"))


mcp = MCPServer("benchscout")
broker = make_broker()


@mcp.tool()
def bench_scan() -> str:
    """Re-scan the bench. Returns what is connected, what each thing is (with evidence),
    what is unknown, and proposed next steps. Only safe probes run."""
    return render(broker.scan())


@mcp.tool()
def bench_inventory_json() -> str:
    """Full bench graph (devices, ports, targets, links) as JSON from the last scan."""
    return json.dumps((broker.last or broker.scan()).to_dict(), default=str)


@mcp.tool()
def bench_impact(action: str, port: str, arg: str | None = None) -> str:
    """Explain what an action would disturb before doing it. Actions: serial.listen,
    serial.send, esptool.chip_id, jtag.scan, jtag.halt, fpga.program, scpi.query, scpi.write."""
    from .policy import classify

    return json.dumps(classify(broker.bench, action, port, arg).to_dict())


@mcp.tool()
def bench_do(action: str, port: str, arg: str | None = None, approval_id: str | None = None) -> str:
    """Perform an action on a port. Safe actions run immediately. Others return
    approval_required with an id: tell the user the exact approve command, wait for
    them to confirm, then call again with approval_id. Never try to reach the
    hardware any other way."""
    return json.dumps(broker.request(action, port, arg, approval_id), default=str)


def main() -> None:
    mcp.run()
