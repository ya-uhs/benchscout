"""Watching selected devices: hold the port, log it, notice events (design.md section 17)."""

import json
import time
from pathlib import Path

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
    out = select(br, VCP)
    assert out["watching"] == ["serial:usbmodem21403"]
    hit = wait_for(events, lambda e: e["kind"] == "alert")
    time.sleep(0.3)
    alerts = [e for e in events if e["kind"] == "alert"]
    assert len(alerts) == 1, "the same alert is reported once per cooldown"
    a = alerts[0]
    assert a["what"].startswith("crash: abort()") and a["line"] == CRASH.strip()
    assert a["device_name"] == "STLINK-V3"
    assert CRASH in Path(a["log"]).read_text()
    br.end_session()


def test_vanish_and_return_are_events_and_reading_goes_on(tmp_path):
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
