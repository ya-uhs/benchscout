"""Core, control channel, agent channel, approvals in memory, device locks.

design.md section 3 and 15 (R2, R3, R8, S1, S2, S3). No hardware: FixtureIo only.
"""

import io as stdio
import json
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest

from benchscout import recon
from benchscout.approvals import ApprovalBook
from benchscout.broker import Broker
from benchscout.core import Core, agent_dispatch, parse_shorthand
from benchscout.io import FixtureIo
from benchscout.locks import DeviceLocks
from benchscout.mcp_relay import NO_CORE, CoreClient
from benchscout.policy import classify
from benchscout.runtime import WINDOWS

FIXTURE = Path(__file__).parent.parent / "fixtures" / "bench_example.json"
CH340 = "serial:usbserial-10"  # bridge: opening may reset -> DISRUPTIVE
VCP = "/dev/cu.usbmodem21403"  # ST-Link VCP: listening is SAFE


@pytest.fixture
def io():
    return FixtureIo(FIXTURE)


@pytest.fixture
def addr():
    """An agent-channel address for one test: a short Unix socket path, or a named pipe on Windows."""
    if WINDOWS:
        yield rf"\\.\pipe\benchscout-test-{uuid.uuid4().hex[:8]}"
        return
    d = Path(tempfile.mkdtemp(prefix="bs", dir="/tmp"))  # AF_UNIX paths must stay short
    yield str(d / "c.sock")
    shutil.rmtree(d, ignore_errors=True)


def gone(address):
    return WINDOWS or not Path(address).exists()


def broker_with(io, state, *ports, locks=None, wait=0.0):
    br = Broker(io, state, locks=locks, approval_wait=wait)
    br.scan()
    if ports:
        br.select([br.bench.ports[br.resolve_port(p)].device_id for p in ports])
    return br


# ---- session and ports ---------------------------------------------------------

def test_unselected_device_is_refused_even_for_safe_actions(io, tmp_path):
    br = broker_with(io, tmp_path)
    before = len(io.calls)
    res = br.request("serial.listen", VCP)
    assert res["status"] == "not_selected" and "panel" in res["message"]
    assert len(io.calls) == before  # nothing was opened
    br.select([br.bench.ports[br.resolve_port(VCP)].device_id])
    assert br.request("serial.listen", VCP, {"seconds": 0.1})["status"] == "done"


def test_port_can_be_given_as_path_and_unknown_port_names_the_ids(io, tmp_path):
    br = broker_with(io, tmp_path, VCP)
    assert br.request("serial.listen", VCP, {"seconds": 0.1})["status"] == "done"
    res = br.request("serial.listen", "/dev/cu.nothing")
    assert res["status"] == "unknown_port" and "serial:usbserial-10" in res["message"]


def test_selected_ports_open_exclusively_recon_does_not(io, tmp_path):
    br = broker_with(io, tmp_path, VCP)
    assert io.exclusive_opens == []  # recon listened to the VCP without locking others out
    br.request("serial.listen", VCP, {"seconds": 0.1})
    assert io.exclusive_opens == [VCP]


# ---- approvals: memory only, waiting, scopes ----------------------------------

def test_waiting_request_runs_as_soon_as_the_human_approves(io, tmp_path):
    br = broker_with(io, tmp_path, CH340, wait=5)
    seen = []
    br.on_event = lambda e: seen.append(e) if e["event"] == "approval_requested" else None

    def human():
        for _ in range(500):
            if seen:
                br.decide(seen[0]["approval"]["id"], True)
                return
            time.sleep(0.01)

    threading.Thread(target=human, daemon=True).start()
    res = br.request("serial.listen", CH340, {"seconds": 0.1})
    assert res["status"] == "done"
    assert ("serial_listen", "/dev/cu.usbserial-10") in io.calls


def test_denied_request_does_not_run(io, tmp_path):
    br = broker_with(io, tmp_path, CH340)
    aid = br.request("serial.listen", CH340)["approval_id"]
    br.decide(aid, False)
    assert br.request("serial.listen", CH340, approval_id=aid)["status"] == "denied"
    assert ("serial_listen", "/dev/cu.usbserial-10") not in io.calls


def test_nothing_on_disk_grants_anything(io, tmp_path):
    br = broker_with(io, tmp_path, CH340)
    aid = br.request("serial.listen", CH340)["approval_id"]
    assert not (tmp_path / "approvals.json").exists()
    with open(tmp_path / "audit.jsonl", "a") as f:  # an agent forging the log
        f.write(json.dumps({"what": "granted", "id": aid}) + "\n")
    assert br.request("serial.listen", CH340, approval_id=aid)["status"] == "approval_required"


def test_session_grant_covers_repeated_disruptive_calls(io, tmp_path):
    br = broker_with(io, tmp_path, CH340)
    aid = br.request("serial.listen", CH340, {"seconds": 0.1})["approval_id"]
    br.decide(aid, True, "session")
    assert br.request("serial.listen", CH340, {"seconds": 0.1}, approval_id=aid)["status"] == "done"
    assert br.request("serial.listen", CH340, {"seconds": 0.1})["status"] == "done"  # no new approval needed
    assert br.request("esptool.chip_id", CH340)["status"] == "approval_required"  # other action: not covered


PSU = "visa:TCPIP0::192.168.1.50::INSTR"


def test_dangerous_cannot_be_granted_beyond_one_use(io, tmp_path):
    br = broker_with(io, tmp_path, PSU)
    res = br.request("scpi.write", PSU, {"command": "OUTP CH1,ON"})
    assert res["impact"]["effect"] == "dangerous"
    with pytest.raises(ValueError, match="only DISRUPTIVE"):
        br.decide(res["approval_id"], True, "session")


def test_unimplemented_actions_are_not_put_to_the_human(io, tmp_path):
    br = broker_with(io, tmp_path, "jtag:210319A8C7D1")
    assert br.request("fpga.program", "jtag:210319A8C7D1", {"bitstream": "x.fs"})["status"] == "not_implemented"
    assert br.approvals.pending() == []


def test_listen_args_are_typed_and_checked(io, tmp_path):
    """Real bench, 2026-10-31: `arg=9600` meant a baud rate but was read as 9600 seconds."""
    br = broker_with(io, tmp_path, VCP)
    assert br.request("serial.listen", VCP, {"seconds": 0.1, "baud": 9600})["status"] == "done"
    assert io.bauds[-1] == 9600
    too_long = br.request("serial.listen", VCP, {"seconds": 9600})
    assert too_long["status"] == "invalid_args" and "out of range" in too_long["message"]
    bad = br.request("serial.listen", VCP, {"rate": 9600})
    assert bad["status"] == "invalid_args" and "baud" in bad["message"]
    assert br.request("serial.listen", VCP, "9600")["status"] == "invalid_args"


def test_approval_is_bound_to_the_filled_in_args(io, tmp_path):
    br = broker_with(io, tmp_path, CH340)
    aid = br.request("serial.listen", CH340, {"baud": 9600})["approval_id"]
    br.decide(aid, True)
    assert br.request("serial.listen", CH340, {"baud": 115200}, approval_id=aid)["status"] == "approval_mismatch"
    assert br.request("serial.listen", CH340, {"baud": 9600, "seconds": 3}, approval_id=aid)["status"] == "done"


def test_timed_grant_expires(io, tmp_path):
    now = [0.0]
    book = ApprovalBook(clock=lambda: now[0])
    br = broker_with(io, tmp_path)
    imp = classify(br.bench, "serial.listen", CH340)
    a = book.open(imp, None)
    book.decide(a.id, True, 5)  # 5 minutes
    assert book.covered("serial.listen", CH340, imp.effect)
    now[0] = 5 * 60 + 1
    assert not book.covered("serial.listen", CH340, imp.effect)


def test_deselecting_a_device_withdraws_its_grants(io, tmp_path):
    br = broker_with(io, tmp_path, CH340, VCP)
    aid = br.request("serial.listen", CH340, {"seconds": 0.1})["approval_id"]
    br.decide(aid, True, "session")
    br.select([br.bench.ports[br.resolve_port(VCP)].device_id])
    br.select([br.bench.ports[br.resolve_port(p)].device_id for p in (CH340, VCP)])
    assert br.request("serial.listen", CH340, {"seconds": 0.1})["status"] == "approval_required"


# ---- device locks between windows (S1) -----------------------------------------

def test_a_device_is_used_by_one_window_at_a_time(io, tmp_path):
    a_locks = DeviceLocks({"pid": 1, "workspace": "/work/a"}, root=tmp_path / "rt")
    b_locks = DeviceLocks({"pid": 2, "workspace": "/work/b"}, root=tmp_path / "rt")
    a = broker_with(io, tmp_path / "a", VCP, locks=a_locks)
    stlink = a.bench.ports[a.resolve_port(VCP)].device_id

    io_b = FixtureIo(FIXTURE)
    b = broker_with(io_b, tmp_path / "b", locks=b_locks)
    assert ("serial_listen", VCP) not in io_b.calls  # window B's recon leaves A's device alone
    t = b.bench.targets_via(b.resolve_port(VCP))[0]
    assert any("in use by another benchscout window (/work/a)" in f for f in t.facts)
    assert b.select([stlink])["busy"][stlink]["workspace"] == "/work/a"

    a.end_session()
    assert b.select([stlink])["selected"] == [stlink]


# ---- channels -------------------------------------------------------------------

def test_agent_channel_has_no_way_to_approve_or_select(io, tmp_path):
    br = broker_with(io, tmp_path, CH340)
    for method in ("decide", "select", "pending"):
        with pytest.raises(ValueError, match="agent channel"):
            agent_dispatch(br, method, {"approval_id": "x", "granted": True, "devices": []})


def test_end_to_end_agent_waits_while_human_approves_on_control(io, tmp_path, addr):
    br = Broker(io, tmp_path, approval_wait=5)
    out = stdio.StringIO()
    core = Core(br, addr, out)
    br.on_event = core.emit
    core.start()
    try:
        core.handle_line('{"id": 1, "method": "scan"}')
        dev = br.bench.ports[CH340].device_id
        core.handle_line(json.dumps({"id": 2, "method": "select", "params": {"devices": [dev]}}))

        client = CoreClient(addr)
        got = {}
        t = threading.Thread(daemon=True, target=lambda: got.update(client.call("do", {"action": "serial.listen", "port": CH340, "args": {"seconds": 0.1}})))
        t.start()
        aid = None
        for _ in range(200):
            events = [json.loads(l) for l in out.getvalue().splitlines()]
            req = [e for e in events if e.get("event") == "approval_requested"]
            if req:
                aid = req[0]["approval"]["id"]
                break
            time.sleep(0.01)
        assert aid
        assert client.call("decide", {"approval_id": aid, "granted": True})["error"].startswith("ValueError")
        core.handle_line(f"approve {aid}")
        t.join(5)
        assert got["result"]["status"] == "done"
    finally:
        core.stop()
    assert gone(addr)


def test_second_core_for_the_same_folder_is_refused(io, tmp_path, addr):
    first = Core(Broker(io, tmp_path / "1"), addr, stdio.StringIO())
    first.start()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            Core(Broker(FixtureIo(FIXTURE), tmp_path / "2"), addr, stdio.StringIO()).start()
    finally:
        first.stop()


def test_closing_the_control_channel_stops_the_core_and_frees_devices(io, tmp_path, addr):
    locks = DeviceLocks({"pid": 1, "workspace": "/w"}, root=tmp_path / "rt")
    br = Broker(io, tmp_path, locks=locks)
    core = Core(br, addr, stdio.StringIO())
    br.scan()
    dev = br.bench.ports[CH340].device_id
    core.run(stdio.StringIO(f'{json.dumps({"method": "select", "params": {"devices": [dev]}})}\n'), initial_scan=False)
    assert not locks.holds(dev) and gone(addr)


def test_controller_vanishing_does_not_crash_the_core(io, tmp_path, addr):
    class Gone(stdio.StringIO):
        def write(self, _):
            raise BrokenPipeError(32, "Broken pipe")

    locks = DeviceLocks({"pid": 1, "workspace": "/w"}, root=tmp_path / "rt")
    br = Broker(io, tmp_path, locks=locks)
    br.scan()
    dev = br.bench.ports[CH340].device_id
    br.select([dev])
    Core(br, addr, Gone()).run(stdio.StringIO(""), initial_scan=False)
    assert not locks.holds(dev) and gone(addr)


def test_relay_without_a_core_says_how_to_start_one(addr):
    assert CoreClient(addr).call("scan") == {"error": NO_CORE}


@pytest.mark.parametrize("line,expected", [
    ("approve ab12", {"method": "decide", "params": {"approval_id": "ab12", "granted": True, "scope": "once"}}),
    ("approve ab12 session", {"method": "decide", "params": {"approval_id": "ab12", "granted": True, "scope": "session"}}),
    ("approve ab12 10m", {"method": "decide", "params": {"approval_id": "ab12", "granted": True, "scope": 10}}),
    ("deny ab12", {"method": "decide", "params": {"approval_id": "ab12", "granted": False}}),
    ("select a b", {"method": "select", "params": {"devices": ["a", "b"]}}),
    ("quit", {"method": "shutdown", "params": {}}),
])
def test_control_shorthand(line, expected):
    assert parse_shorthand(line) == expected


def test_select_by_what_people_see(io, tmp_path):
    br = broker_with(io, tmp_path)
    assert br.select(["usbserial-10"])["selected"] == [br.bench.ports[CH340].device_id]
    with pytest.raises(KeyError, match="matches"):
        br.select(["usbmodem"])  # two devices: ambiguous


# ---- setup per agent (S4) -----------------------------------------------------------

def test_setup_puts_the_skill_where_each_agent_reads_it(tmp_path):
    from benchscout.cli import setup_workspace

    claude, copilot = tmp_path / "c", tmp_path / "p"
    claude.mkdir()
    copilot.mkdir()
    setup_workspace(claude, "claude")
    setup_workspace(copilot, "copilot")
    assert (claude / ".claude/skills/bench-expert/SKILL.md").exists()
    settings = json.loads((claude / ".claude/settings.local.json").read_text())
    assert "Bash(benchscout:*)" in settings["permissions"]["deny"]
    assert json.loads((claude / ".mcp.json").read_text())["mcpServers"]["benchscout"]["args"][:2] == ["mcp", "--workspace"]
    assert (copilot / ".agents/skills/bench-expert/SKILL.md").exists()
    assert not (copilot / ".claude").exists()
    assert json.loads((copilot / ".vscode/mcp.json").read_text())["servers"]["benchscout"]["type"] == "stdio"


def test_setup_re_enables_a_server_disabled_earlier(tmp_path):
    from benchscout.cli import setup_workspace

    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/settings.local.json").write_text(json.dumps(
        {"enabledMcpjsonServers": ["benchscout"], "disabledMcpjsonServers": ["benchscout", "other"]}))
    setup_workspace(tmp_path, "claude")
    settings = json.loads((tmp_path / ".claude/settings.local.json").read_text())
    assert settings["disabledMcpjsonServers"] == ["other"]
    assert "benchscout" in settings["enabledMcpjsonServers"]
