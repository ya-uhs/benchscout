"""Safety contracts for agent-owned programs; local scripts only, no hardware."""

import sys
import time
from pathlib import Path

import pytest

from benchscout.broker import Broker
from benchscout.core import agent_dispatch
from benchscout.io import FixtureIo


@pytest.fixture
def bench(tmp_path):
    events = []
    b = Broker(FixtureIo(Path(__file__).parent.parent / "fixtures/bench_example.json"),
               tmp_path / ".benchscout", approval_wait=0, on_event=events.append)
    b.scan()
    r = b.register_resource("opaque:test-endpoint", "Unknown test endpoint")
    b.select([r["device"]])
    yield b, r, events
    b.end_session()


def plan(b, r, code, **extra):
    f = b.state.parent / "program.py"
    f.write_text(code, encoding="utf-8")
    return dict(resources=[r["resource"]], argv=[sys.executable, str(f)], purpose="Local test program", **extra)


def start(b, args):
    asked = b.run_program(**args)
    assert asked["status"] == "approval_required"
    b.decide(asked["approval_id"], True)
    return b.run_program(**args, approval_id=asked["approval_id"])


def finish(b, job):
    assert b.runs[job].done.wait(5), b.run_state(job)
    return b.run_state(job)


def test_unknown_resource_bytes_and_single_use_approval(bench):
    b, r, events = bench
    args = plan(b, r, "import sys\nsys.stdout.buffer.write(bytes(range(256)))\nsys.stderr.buffer.write(b'\\x00\\xff')")
    b.approvals.set_allow_all(True)
    asked = b.run_program(**args)
    assert asked["status"] == "approval_required"  # even allow-all never authorizes arbitrary code
    assert not b.runs
    b.decide(asked["approval_id"], True)
    run = b.run_program(**args, approval_id=asked["approval_id"])
    result = finish(b, run["job"])
    assert result["status"] == "completed"
    assert Path(result["stdout"]).read_bytes() == bytes(range(256))
    assert Path(result["stderr"]).read_bytes() == b"\x00\xff"
    assert b.run_program(**args, approval_id=asked["approval_id"])["status"] == "used"
    assert any(e["event"] == "run_changed" for e in events)


def test_changed_script_needs_new_approval(bench):
    b, r, _ = bench
    args = plan(b, r, "print('original')")
    asked = b.run_program(**args)
    b.decide(asked["approval_id"], True)
    Path(args["argv"][1]).write_text("raise RuntimeError('must never run')")
    assert b.run_program(**args, approval_id=asked["approval_id"])["status"] == "approval_mismatch"
    assert not b.runs


def test_interpreter_keeps_its_virtual_environment(bench):
    b, r, _ = bench
    args = plan(b, r, "import sys; print(sys.prefix)")
    run = start(b, args)
    result = finish(b, run["job"])
    assert result["status"] == "completed"
    assert Path(result["stdout"]).read_text().strip() == sys.prefix


@pytest.mark.parametrize("during_verification", [False, True])
def test_cancelled_before_launch_never_starts_program(bench, monkeypatch, during_verification):
    from benchscout.execution import Plan, Run, prepare

    b, r, _ = bench
    marker = b.state.parent / "must-not-exist"
    args = plan(b, r, f"from pathlib import Path; Path({str(marker)!r}).touch()")
    prepared = prepare(args["argv"], [(r["resource"], r["device"], r["address"])], [],
                       b.state.parent, args["purpose"], 60, False, 1024)
    run = Run(prepared, b.artifacts, lambda event: None)
    if during_verification:
        verify = Plan.verify
        def cancel_after_verify(self):
            verify(self)
            run.cancel.set()
        monkeypatch.setattr(Plan, "verify", cancel_after_verify)
    else:
        run.cancel.set()
    released = []
    run.work(lambda: released.append(True), lambda: None)
    assert run.done.is_set()
    assert run.status == "cancelled"
    assert released == [True]
    assert not marker.exists()



def test_registration_is_not_selection_and_survives_rescan(bench):
    b, _, _ = bench
    r = b.register_resource("opaque:second", "Another unknown endpoint")
    args = plan(b, r, "print('no')")
    assert b.run_program(**args)["status"] == "not_selected"
    b.scan()
    assert r["resource"] in b.bench.ports
    assert b.bench.targets_via(r["resource"])[0].name is None
    assert b.register_resource("opaque:second", "New label")["device"] == r["device"]
    with pytest.raises(ValueError, match="owner"):
        b.register_resource("opaque:second", "Changed owner", next(iter(b.selected)))


def test_secondary_resource_deselection_withdraws_approval(bench):
    b, r, _ = bench
    other = b.register_resource("opaque:second", "second")
    b.select([r["device"], other["device"]])
    args = plan(b, r, "print('no')")
    args["resources"].append(other["resource"])
    asked = b.run_program(**args)
    primary = b.approvals.get(asked["approval_id"]).port
    b.select([b.bench.ports[primary].device_id])  # keep primary; only the secondary leaves
    assert b.approvals.get(asked["approval_id"]).status == "withdrawn"
    assert not b.runs


@pytest.mark.parametrize("stop", ["agent", "deselect", "session"])
def test_observer_stops_before_releasing_ownership(bench, stop):
    b, r, events = bench
    args = plan(b, r, "import time\nprint('observation', flush=True)\ntime.sleep(60)", observe=True)
    run = start(b, args)
    deadline = time.monotonic() + 5
    while not any(e.get("kind") == "observation" for e in events) and time.monotonic() < deadline:
        time.sleep(.02)
    observation = next(e for e in events if e.get("kind") == "observation")
    assert observation["stderr"] == b.run_state(run["job"])["stderr"]
    assert observation["job"] == run["job"]
    assert b._run_owns(r["device"])
    if stop == "agent":
        agent_dispatch(b, "run_state", {"job": run["job"], "stop": True})
    elif stop == "deselect":
        b.select([])
    else:
        b.end_session()
    assert finish(b, run["job"])["status"] == "cancelled"
    assert not b._run_owns(r["device"])
    assert b._device_turn(r["device"]).acquire(False)
    b._device_turn(r["device"]).release()


def test_busy_program_does_not_queue_or_consume_approval(bench):
    b, r, _ = bench
    args = plan(b, r, "import time\ntime.sleep(60)")
    run = start(b, args)
    asked = b.run_program(**args)
    b.decide(asked["approval_id"], True)
    assert b.run_program(**args, approval_id=asked["approval_id"])["status"] == "busy"
    assert b.approvals.get(asked["approval_id"]).status == "granted"
    b.run_state(run["job"], stop=True)


def test_attached_resource_shares_existing_device_and_pauses_watch(bench):
    b, _, _ = bench
    port = b.bench.ports[b.resolve_port("/dev/cu.usbmodem21403")]
    r = b.register_resource("opaque:attached", "Alternate access", port.device_id)
    b.select([port.device_id])
    assert port.id in b.watches
    run = start(b, plan(b, r, "import time; time.sleep(60)"))
    assert port.id not in b.watches
    assert b.request("serial.listen", port.id, {"seconds": .1})["status"] == "busy"
    assert b._watch(port.id, 115200) is False
    b.scan()
    assert b.bench.ports[r["resource"]].device_id == port.device_id
    b.run_state(run["job"], stop=True)


def test_input_link_changed_while_approval_waits_is_rechecked(bench):
    from benchscout.execution import prepare

    b, r, _ = bench
    original = b.state.parent / "original.py"
    original.write_text("print('original')")
    changed = b.state.parent / "changed.py"
    changed.write_text("print('changed')")
    link = b.state.parent / "input.py"
    try:
        link.symlink_to(original)
    except OSError:
        pytest.skip("this host does not permit creating symbolic links")
    p = prepare([sys.executable, str(link)], [(r["resource"], r["device"], r["address"])], [],
                b.state.parent, "check input", 60, False, 1024)
    link.unlink()
    link.symlink_to(changed)
    with pytest.raises(ValueError, match="input changed"):
        p.verify()


@pytest.mark.parametrize("code,extra,status", [
    ("import time; time.sleep(60)", {"timeout": 1}, "timed_out"),
    ("raise SystemExit(3)", {}, "failed"),
    ("print('too much output')", {"max_bytes": 1}, "output_limit"),
])
def test_terminal_outcomes_are_explicit(bench, code, extra, status):
    b, r, _ = bench
    run = start(b, plan(b, r, code, **extra))
    assert finish(b, run["job"])["status"] == status


def test_final_observation_is_not_lost_to_notification_throttle(bench):
    b, r, events = bench
    snapshots = []
    original = b.on_event

    def capture(event):
        if event.get("kind") == "observation":
            snapshots.append(Path(event["log"]).read_bytes())
        original(event)

    b.on_event = capture
    run = start(b, plan(b, r, "import time\nprint('first', flush=True)\ntime.sleep(.2)\nprint('last', flush=True)",
                        observe=True))
    result = finish(b, run["job"])
    assert result["status"] == "completed"
    assert snapshots[-1].splitlines() == [b"first", b"last"]


@pytest.mark.parametrize("stop", ["job", "deselect"])
def test_adapter_tool_uses_managed_stop_and_raw_artifacts(bench, monkeypatch, stop):
    from concurrent.futures import ThreadPoolExecutor
    from benchscout import knowledge as K

    b, r, events = bench
    script = b.state.parent / "adapter.py"
    script.write_text("import sys,time\nsys.stdout.buffer.write(b'\\x00\\xff'); sys.stdout.flush()\ntime.sleep(60)")
    monkeypatch.setitem(K.TOOLS, "fixture.observe", {
        "name": "fixture.observe", "argv": [sys.executable, str(script)], "effect": "dangerous",
        "reason": "test program", "doc": "fixture observation", "timeout": 60,
    })
    asked = b.request("fixture.observe", r["resource"])
    assert asked["status"] == "approval_required"
    assert not b.runs
    b.decide(asked["approval_id"], True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(b.request, "fixture.observe", r["resource"], approval_id=asked["approval_id"])
        deadline = time.monotonic() + 5
        while not any(e.get("event") == "run_changed" for e in events) and time.monotonic() < deadline:
            time.sleep(.01)
        event = next(e for e in events if e.get("event") == "run_changed")
        job = event["job"]
        while Path(event["stdout"]).stat().st_size < 2 and time.monotonic() < deadline:
            time.sleep(.01)
        if stop == "job":
            b.run_state(job, stop=True)
        else:
            b.select([])
        result = pending.result(timeout=5)["result"]
    assert result["execution"]["status"] == "cancelled"
    assert Path(result["execution"]["stdout"]).read_bytes() == b"\x00\xff"
    assert result["returncode"] is None


@pytest.mark.parametrize("change", ["script", "definition", "connection"])
def test_adapter_approval_rejects_changed_execution_plan(bench, monkeypatch, change):
    from benchscout import knowledge as K

    b, r, _ = bench
    script = b.state.parent / "approved.py"
    script.write_text("print('approved')")
    definition = {"name": "fixture.run", "argv": [sys.executable, str(script)], "effect": "dangerous",
                  "reason": "fixture program", "timeout": 60}
    monkeypatch.setitem(K.TOOLS, "fixture.run", definition)
    asked = b.request("fixture.run", r["resource"])
    assert asked["status"] == "approval_required"
    b.decide(asked["approval_id"], True)
    if change == "script":
        script.write_text("print('changed')")
    elif change == "definition":
        monkeypatch.setitem(K.TOOLS, "fixture.run", dict(definition, argv=[sys.executable, "-c", "print('changed')"]))
    else:
        b.bench.ports[r["resource"]].path = "another:endpoint"
    assert b.request("fixture.run", r["resource"], approval_id=asked["approval_id"])["status"] == "approval_mismatch"
    assert not b.runs


@pytest.mark.parametrize("stop", ["job", "deselect"])
def test_native_listen_can_be_cancelled_without_releasing_early(bench, stop):
    from concurrent.futures import ThreadPoolExecutor

    b, _, events = bench
    port = b.resolve_port("/dev/cu.usbmodem21403")
    b.select([b.bench.ports[port].device_id])
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(b.request, "serial.listen", port, {"seconds": 30})
        deadline = time.monotonic() + 5
        while not any(e.get("event") == "run_changed" for e in events) and time.monotonic() < deadline:
            time.sleep(.01)
        job = next(e["job"] for e in events if e.get("event") == "run_changed")
        if stop == "job":
            b.run_state(job, stop=True)
        else:
            b.select([])
        result = pending.result(timeout=5)
    assert result["status"] == "cancelled"
    assert b.run_state(job)["status"] == "cancelled"


def test_recipe_restart_restores_no_selection_or_permission(bench):
    b, r, _ = bench
    args = plan(b, r, "print('recipe output')", observe=True)
    b.recipe("save", "observe", args)
    b.end_session()
    other = Broker(b.io, b.state, approval_wait=0)
    try:
        assert other.recipe()["recipes"] == ["observe"]
        assert not other.selected and not other.runs
        restored = other.recipe("restore", "observe")
        assert restored["selected"] == []
        assert other.recipe("run", "observe")["status"] == "not_selected"
        other.select([r["device"]])
        asked = other.recipe("run", "observe")
        assert asked["status"] == "approval_required"
        other.decide(asked["approval_id"], True)
        run = other.recipe("run", "observe", approval_id=asked["approval_id"])
        assert finish(other, run["job"])["status"] == "completed"
        assert other.recipe("run", "observe")["status"] == "approval_required"
        other.recipe("delete", "observe")
        assert other.recipe()["recipes"] == []
    finally:
        other.end_session()


def test_invalid_recipe_file_is_preserved(bench):
    b, r, _ = bench
    path = b.state / "recipes.json"
    path.write_text("broken")
    with pytest.raises(ValueError):
        b.recipe("save", "new", plan(b, r, "print('no')"))
    assert path.read_text() == "broken"
