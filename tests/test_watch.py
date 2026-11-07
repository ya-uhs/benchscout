"""Watching selected devices: hold the port, log it, notice events (design.md section 17)."""

import json
import time
from pathlib import Path

from benchscout import watch as watch_mod
from benchscout.broker import Broker
from benchscout.io import FixtureIo

FIXTURE = Path(__file__).parent.parent / "fixtures" / "bench_example.json"
VCP = "/dev/cu.usbmodem21403"  # ST-Link VCP: SAFE to open, watched as soon as it is selected
CH340 = "/dev/cu.usbserial-10"  # bridge: opening may reset, so watching needs approval
CRASH = "abort() was called at PC 0x42001c85 on core 1\n"


def bench(tmp_path, streams):
    data = json.loads(FIXTURE.read_text())
    data["streams"] = streams
    f = tmp_path / "bench.json"
    f.write_text(json.dumps(data))
    io = FixtureIo(f)
    events = []
    br = Broker(io, tmp_path / "ws" / ".benchscout")
    br.on_event = lambda e: events.append(e) if e["event"] == "device_event" else None
    br.scan()
    return io, br, events


def wait_for(events, pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        hit = [e for e in events if pred(e)]
        if hit:
            return hit
        time.sleep(0.02)
    raise AssertionError(f"no such event; got {events}")


def select(br, path):
    return br.select([br.bench.ports[br.resolve_port(path)].device_id])


def test_safe_port_is_watched_on_selection_and_a_crash_is_noticed_once(tmp_path):
    io, br, events = bench(tmp_path, {VCP: [{"after": 0.05, "text": "boot ok\n"}, {"after": 0.15, "text": CRASH},
                                            {"after": 0.2, "text": CRASH}]})
    br.remember(VCP, "interpretation", "abort marker", data={"pattern": "abort", "alert": "abort marker"})
    out = select(br, VCP)
    assert out["watching"] == ["serial:usbmodem21403"]
    assert events[0]["kind"] == "watching", "the human is told the port is watched"
    wait_for(events, lambda e: e["kind"] == "alert")
    time.sleep(0.3)
    alerts = [e for e in events if e["kind"] == "alert"]
    assert len(alerts) == 1, "the same alert is reported once per cooldown"
    a = alerts[0]
    assert a["what"] == "abort marker" and a["line"] == CRASH.strip()
    assert a["device_name"] == "STMicroelectronics STLINK-V3"
    assert CRASH in Path(a["log"]).read_text()
    br.end_session()


def test_vanish_and_return_are_events_and_reading_goes_on(tmp_path, monkeypatch):
    monkeypatch.setattr(watch_mod, "OPEN_RESET_S", 0.05)  # a reset well after opening, not one the open caused
    io, br, events = bench(tmp_path, {VCP: [{"after": 0.05, "text": "before\n"}, {"after": 0.1, "vanish": 0.3},
                                            {"after": 0.6, "text": "after reset\n"}]})
    select(br, VCP)
    wait_for(events, lambda e: e["kind"] == "vanished")
    wait_for(events, lambda e: e["kind"] == "returned", timeout=4)
    w = br.watches["serial:usbmodem21403"]
    end = time.monotonic() + 3
    while "after reset" not in w.recent() and time.monotonic() < end:
        time.sleep(0.05)
    assert "before" in w.recent() and "after reset" in w.recent()
    br.end_session()


def test_listening_to_a_watched_port_is_safe_and_reads_the_watch(tmp_path):
    io, br, events = bench(tmp_path, {CH340: [{"after": 0.3, "text": "[I] sensor ok\n"}]})
    select(br, CH340)
    assert br.watches == {}, "a port that may reset on open is not watched without approval"
    res = br.request("serial.watch", CH340)
    assert res["status"] == "approval_required"
    br.decide(res["approval_id"], True)
    assert br.request("serial.watch", CH340, approval_id=res["approval_id"])["status"] == "done"
    listen = br.request("serial.listen", CH340, {"seconds": 0.5})
    assert listen["status"] == "done", "already open: nothing to reset, so no approval"
    assert "sensor ok" in listen["result"]["preview"]
    assert io.streams_opened.count(CH340) == 1, "the watch did not reopen the port"
    br.end_session()


def test_deselecting_stops_the_watch(tmp_path):
    io, br, events = bench(tmp_path, {VCP: []})
    select(br, VCP)
    w = br.watches["serial:usbmodem21403"]
    br.select([])
    assert br.watches == {} and not w.alive


def _all_events(br):
    seen = []
    br.on_event = seen.append
    return seen


def test_benchscout_offers_to_watch_a_port_it_cannot_open_safely(tmp_path):
    """Selecting a device whose port may reset on open: benchscout asks the human itself (an
    approval card) instead of leaving watching to the agent; nothing is opened until then."""
    io, br, _ = bench(tmp_path, {CH340: [{"after": 0.3, "text": "[I] sensor ok\n"}]})
    seen = _all_events(br)
    select(br, CH340)
    asked = [e["approval"] for e in seen if e["event"] == "approval_requested"]
    assert len(asked) == 1
    a = asked[0]
    assert (a["by"], a["action"], a["port"], a["effect"]) == ("benchscout", "serial.watch", "serial:usbserial-10", "disruptive")
    assert a["subject"] == "QinHeng USB Serial" and a["purpose"]
    assert br.watches == {} and not io.opened(CH340)
    assert "Do not ask again" in br.watch_state()["serial:usbserial-10"]

    select(br, CH340)  # selecting again does not ask twice
    assert len([e for e in seen if e["event"] == "approval_requested"]) == 1

    br.decide(a["id"], True)
    hit = wait_for(seen, lambda e: e.get("event") == "device_event" and e["kind"] == "watching")
    assert hit[0]["device_name"] == "QinHeng USB Serial" and hit[0]["log"].endswith("serial_usbserial-10.log")
    assert br.watches["serial:usbserial-10"].alive
    assert br.watch_state()["serial:usbserial-10"].startswith("watched")
    end = time.monotonic() + 3
    while "sensor ok" not in br.watches["serial:usbserial-10"].recent() and time.monotonic() < end:
        time.sleep(0.05)
    assert "sensor ok" in br.watches["serial:usbserial-10"].recent()
    br.end_session()


def test_declined_watch_is_not_offered_again_this_session(tmp_path):
    io, br, _ = bench(tmp_path, {})
    seen = _all_events(br)
    select(br, CH340)
    a = next(e["approval"] for e in seen if e["event"] == "approval_requested")
    br.decide(a["id"], False)
    select(br, CH340)
    br.select([])
    select(br, CH340)
    assert len([e for e in seen if e["event"] == "approval_requested"]) == 1
    assert br.watches == {}
    assert "declined" in br.watch_state()["serial:usbserial-10"]


def test_dropping_a_device_closes_its_waiting_offer(tmp_path):
    io, br, _ = bench(tmp_path, {})
    seen = _all_events(br)
    select(br, CH340)
    a = next(e["approval"] for e in seen if e["event"] == "approval_requested")
    br.select([])
    decided = [e["approval"] for e in seen if e["event"] == "approval_decided"]
    assert [(d["id"], d["status"]) for d in decided] == [(a["id"], "withdrawn")]


def test_an_offered_watch_that_did_not_reset_the_device_is_remembered_as_an_observation(tmp_path, monkeypatch):
    import threading

    io, br, _ = bench(tmp_path, {})
    seen = _all_events(br)
    timers = []
    monkeypatch.setattr(threading, "Timer", lambda t, f: timers.append(f) or type("T", (), {"start": lambda s: None})())
    esp = "/dev/cu.usbmodem1101"  # the chip's own USB: if opening reset it, the port would drop off
    select(br, esp)
    a = next(e["approval"] for e in seen if e["event"] == "approval_requested")
    br.decide(a["id"], True)
    wait_for(seen, lambda e: e.get("event") == "device_event" and e["kind"] == "watching")
    assert timers, "benchscout checks after a moment whether opening reset the device"
    timers[0]()
    dev = br.bench.devices[br.bench.ports[br.resolve_port(esp)].device_id]
    obs = [e for e in br.memory.entries(br._key(dev)) if e.kind == "observation"]
    assert obs and "own USB port stayed" in obs[0].text
    br.end_session()



def test_a_port_lost_while_the_computer_slept_is_put_down_to_the_sleep(tmp_path):
    """Real bench 2026-11-01: the lid closed overnight; ports vanished and returned at each dark wake and
    were reported as resets (the boards' counters never reset)."""
    io, br, events = bench(tmp_path, {VCP: [{"after": 0.05, "text": "hb 1\n"}, {"after": 0.6, "vanish": 0.3},
                                            {"after": 1.2, "text": "hb 4000\n"}]})
    select(br, VCP)
    w = br.watches["serial:usbmodem21403"]
    time.sleep(0.2)
    real = time.time
    w._wall = lambda: real() + 3600  # an hour passed on the wall clock while the process stood still
    gone = wait_for(events, lambda e: e["kind"] == "vanished")[0]
    back = wait_for(events, lambda e: e["kind"] == "returned", timeout=4)[0]
    assert gone["cause"] == back["cause"] == "host_sleep"
    assert "asleep" in back["what"]
    br.end_session()


def test_a_port_lost_while_awake_has_no_cause(tmp_path, monkeypatch):
    monkeypatch.setattr(watch_mod, "OPEN_RESET_S", 0.05)  # a reset well after opening, not one the open caused
    io, br, events = bench(tmp_path, {VCP: [{"after": 0.1, "vanish": 0.3}, {"after": 0.6, "text": "boot\n"}]})
    select(br, VCP)
    back = wait_for(events, lambda e: e["kind"] == "returned", timeout=4)[0]
    assert "cause" not in back
    br.end_session()


def test_a_crash_right_after_a_command_sent_through_benchscout_comes_with_that_command(tmp_path):
    """Real bench, 2026-11-03 (acceptance test, Claude Code): `panic` was sent through benchscout with the human's OK,
    the ESP32-S3 crashed, benchscout woke the agent, and the agent had to ask the human whether anything had been sent:
    benchscout knew, and did not say. Its own record now comes with the event and with bench_log."""
    panic = (Path(__file__).parent.parent / "fixtures" / "real" / "esp32s3-testfw-panic.txt").read_text(encoding="utf-8")
    io, br, events = bench(tmp_path, {VCP: [{"after": 0.05, "text": "hb n=1 uptime_ms=1306\n"}]})
    br.remember(VCP, "interpretation", "abort marker", data={"pattern": "abort", "alert": "abort marker"})
    io.data["replies"] = {VCP: {"panic\n": panic}}
    select(br, VCP)
    port = br.resolve_port(VCP)
    aid = br.request("serial.send", port, {"data": "panic", "seconds": 0.3})["approval_id"]
    br.decide(aid, True)
    br.request("serial.send", port, {"data": "panic", "seconds": 0.3}, approval_id=aid)
    alert = wait_for(events, lambda e: e["kind"] == "alert")[0]
    assert any("serial.send" in d and '"panic"' in d and "allowed by the human, once" in d for d in alert["through_benchscout"])
    log = br.read_log(VCP)
    assert any('"panic"' in d for d in log["through_benchscout"]) and "abort()" in log["text"]
    br.end_session()
