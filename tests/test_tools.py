"""External tools defined as data (design.md section 4): written by users, run without a shell."""

import sys
from pathlib import Path

import pytest

from benchscout import actions as A
from benchscout import knowledge as K
from benchscout.broker import Broker
from benchscout.io import FixtureIo
from benchscout.policy import Effect, classify

FIXTURE = Path(__file__).parent.parent / "fixtures" / "bench_example.json"
VCP = "/dev/cu.usbmodem21403"  # SAFE to open: watched as soon as it is selected

# paths go in TOML literal strings ('...'): a Windows path's backslashes are not escapes there
TOOL = '''
[[tool]]
name = "lab.flash"
doc = "our lab's flasher"
effect = "disruptive"
reason = "writes our test image"
port_kinds = ["serial"]
argv = ['{python}', '{script}', "{{port.path}}", "{{image}}", "{{images*}}"]
releases_device = true
args.image = {{ type = "file", required = true, ext = [".bin"] }}
args.images = {{ type = "flash_map", default = "0x0:boot.bin" }}

[[tool]]
name = "lab.old"
argv = ['{python}', "-c", "print('x')"]
version = {{ argv = ['{python}', "-c", "print('flasher v4.5.1')"], min = "5.0", why = "4.x hangs on our board" }}
'''


@pytest.fixture
def ws(tmp_path):
    w = tmp_path / "ws"
    (w / ".benchscout" / "knowledge").mkdir(parents=True)
    script = tmp_path / "echo_args.py"
    script.write_text("import sys; print('ARGS', *sys.argv[1:], sep='\\n')\n")  # plain lines: a repr doubles Windows backslashes
    (w / ".benchscout" / "knowledge" / "lab.toml").write_text(TOOL.format(python=sys.executable, script=script))
    (w / "app.bin").write_bytes(b"\0")
    (w / "boot.bin").write_bytes(b"\0")
    yield w
    K.load()


def broker(ws):
    br = Broker(FixtureIo(FIXTURE), ws / ".benchscout")
    br.scan()
    br.select([br.bench.ports[br.resolve_port(VCP)].device_id])
    return br


def approved(br, action, port, args):
    res = br.request(action, port, args)
    if res["status"] != "approval_required":
        return res
    br.decide(res["approval_id"], True)
    return br.request(action, port, args, approval_id=res["approval_id"])


def test_a_user_tool_runs_without_a_shell_with_filled_in_args(ws):
    br = broker(ws)
    assert br.request("lab.flash", VCP, {"image": "app.bin"})["status"] == "approval_required"  # declared DISRUPTIVE
    res = approved(br, "lab.flash", VCP, {"image": "app.bin"})
    out = res["result"]["preview"]
    assert res["status"] == "done" and res["result"]["returncode"] == 0
    assert VCP in out and str((ws / "app.bin").resolve()) in out and "0x0" in out and str((ws / "boot.bin").resolve()) in out
    w = br.watches["serial:usbmodem21403"]
    assert w.alive, "the device was handed back to the watch after the tool ran"
    br.end_session()


def test_files_and_port_kinds_are_checked_before_anything_runs(ws):
    br = broker(ws)
    assert br.request("lab.flash", VCP, {"image": "app.hex"})["status"] == "invalid_args"
    assert "file not found" in approved(br, "lab.flash", VCP, {"image": "missing.bin"})["result"]["error"]
    res = br.request("lab.flash", "swd:0039003A3133510637363934", {"image": "app.bin"})
    assert res["status"] == "invalid_args" and "serial ports" in res["message"]
    br.end_session()


def test_too_old_a_tool_is_refused_with_the_reason(ws):
    br = broker(ws)
    res = approved(br, "lab.old", VCP, {})
    assert "too old" in res["result"]["error"] and "4.x hangs on our board" in res["result"]["error"]
    br.end_session()


def test_a_tool_with_no_declared_effect_is_dangerous(ws):
    br = broker(ws)
    assert classify(br.bench, "lab.old", br.resolve_port(VCP)).effect == Effect.DANGEROUS
    br.end_session()


def test_shipped_writes_are_dangerous_and_fpga_program_disturbs_the_fabric():
    K.load()
    assert A.get("esptool.write_flash").effect == Effect.DANGEROUS
    assert A.get("fpga.program").port_kinds == ("jtag",)
    flash = A.get("fpga.write_flash")
    assert flash.effect == Effect.DANGEROUS and flash.port_kinds == ("jtag",) and flash.releases_device
    assert "--write-flash" in flash.argv
    assert A.get("esptool.write_flash").params[0].coerce("0x0:a.bin,0x10000:b.bin") == [[0, "a.bin"], [0x10000, "b.bin"]]


def test_a_long_run_does_not_hold_up_the_human_and_its_result_can_be_collected(ws):
    """Review 7 (21): a run held the broker's lock to the end (a flash write takes minutes), so the human's selection
    waited behind it, and a run longer than the agent's call left nothing to collect its result with."""
    import threading
    import time

    (ws / ".benchscout" / "knowledge" / "slow.toml").write_text(
        "[[tool]]\nname = \"lab.slow\"\neffect = \"disruptive\"\nreason = \"takes a while\"\nport_kinds = [\"serial\"]\n"
        f"argv = ['{sys.executable}', \"-c\", \"import time; time.sleep(1.5); print('slow done')\"]\n")
    br = broker(ws)
    br.call_budget = 0.5  # (one call may wait this long in all)
    asked = br.request("lab.slow", VCP, {})
    br.decide(asked["approval_id"], True)
    running = br.request("lab.slow", VCP, {}, approval_id=asked["approval_id"])
    assert running["status"] == "running" and running["job"], running
    t0 = time.monotonic()
    other = threading.Thread(target=lambda: br.select(sorted(br.selected)))
    other.start()
    other.join(1.0)
    assert not other.is_alive() and time.monotonic() - t0 < 1.0, "the human's selection does not wait for the run"
    br.call_budget = 10
    done = br.request("lab.slow", VCP, {}, job=running["job"])
    assert done["status"] == "done" and "slow done" in done["result"]["preview"] and done["approval"]["id"] == asked["approval_id"]
    assert br.request("lab.slow", VCP, {}, job=running["job"])["status"] == "unknown_job", "collected once"
    br.end_session()
