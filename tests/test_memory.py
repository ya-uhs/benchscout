"""Memory (design.md section 6) and naming by quoting the device (section 3)."""

import json
import time
import tomllib
from pathlib import Path

from benchscout import knowledge as K
from benchscout.broker import Broker
from benchscout.io import FixtureIo
from benchscout.model import Confidence

FIXTURE = Path(__file__).parent.parent / "fixtures" / "bench_example.json"
ESP = "/dev/cu.usbmodem1101"
CH340 = "/dev/cu.usbserial-10"
VCP = "/dev/cu.usbmodem21403"
BANNER = "ESP-ROM:esp32s3-20210327"


def fixture(tmp_path, **extra):
    data = json.loads(FIXTURE.read_text())
    data["replies"] = {ESP: {"reboot\n": f"ok\n{BANNER}\nrst:0xc (RTC_SW_CPU_RST)\n"}}
    data.update(extra)
    f = tmp_path / "bench.json"
    f.write_text(json.dumps(data))
    return f


def session(tmp_path, f, *paths, events=None):
    br = Broker(FixtureIo(f), tmp_path / "ws" / ".benchscout")
    if events is not None:
        br.on_event = events.append
    br.scan()
    if paths:
        br.select([br.bench.ports[br.resolve_port(p)].device_id for p in paths])
    return br


def send_approved(br, port, data):
    aid = br.request("serial.send", port, {"data": data})["approval_id"]
    br.decide(aid, True)
    return br.request("serial.send", port, {"data": data}, approval_id=aid)


def test_a_quoted_name_is_checked_remembered_and_back_next_session_without_an_llm(tmp_path):
    f = fixture(tmp_path)
    events = []
    br = session(tmp_path, f, ESP, events=events)
    send_approved(br, ESP, "reboot")
    res = br.identify(ESP, "ESP32-S3", BANNER)
    assert res["status"] == "identified"
    assert any(e["event"] == "memory_added" and e["entry"]["kind"] == "identity" for e in events)
    br.end_session()

    nxt = Broker(FixtureIo(fixture(tmp_path)), tmp_path / "ws" / ".benchscout")  # a new session, nothing heard yet
    nxt.scan()
    t = nxt.bench.targets[res["target"]]
    assert t.name == "ESP32-S3" and t.confidence == Confidence.IDENTIFIED
    assert any(ev.source == "learned" and BANNER in ev.detail for ev in t.evidence)


def test_a_quote_not_in_the_output_names_nothing(tmp_path):
    br = session(tmp_path, fixture(tmp_path), ESP)
    res = br.identify(ESP, "ESP32-C6", "ESP-ROM:esp32c6-20220919")
    assert res["status"] == "unverified"
    t = br.bench.targets_via(br.resolve_port(ESP))[0]
    assert t.name is None and any(f.startswith("guess (not verified): ESP32-C6") for f in t.facts)
    assert br.identify(ESP, "ESP32-S3", "ESP")["status"] == "rejected"  # too short to be evidence


def test_safety_memory_waits_for_the_human(tmp_path):
    f = fixture(tmp_path)
    br = session(tmp_path, f, ESP)
    res = br.remember(ESP, "safety", "opening does not reset it", data={"listen_safe": True})
    assert res["status"] == "pending_human"
    assert br.request("serial.listen", ESP)["status"] == "approval_required", "not in effect yet"
    key = br._key(br.bench.devices[br.bench.ports[br.resolve_port(ESP)].device_id])
    br.decide_memory(key, res["id"], True)
    assert br.request("serial.listen", ESP, {"seconds": 0.1})["status"] == "done", "confirmed: listening is SAFE now"
    br.end_session()
    again = session(tmp_path, f)
    assert again.bench.ports[again.resolve_port(ESP)].may_reset_on_open is False


def test_rejected_or_shared_serial_safety_memory_does_nothing(tmp_path, monkeypatch):
    f = fixture(tmp_path)
    br = session(tmp_path, f, ESP)
    dev = br.bench.devices[br.bench.ports[br.resolve_port(ESP)].device_id]
    res = br.remember(ESP, "safety", "x", data={"listen_safe": True})
    br.decide_memory(br._key(dev), res["id"], False)
    assert session(tmp_path, f).bench.ports[br.resolve_port(ESP)].may_reset_on_open is True
    # another unit of the same model could answer to a shared serial: never loosen safety on it
    res = br.remember(ESP, "safety", "y", data={"listen_safe": True})
    br.decide_memory(br._key(dev), res["id"], True)
    kdir = tmp_path / "ws" / ".benchscout" / "knowledge"
    kdir.mkdir(parents=True, exist_ok=True)
    (kdir / "shared.toml").write_text(f'shared_serials = ["{dev.serial}"]\n')
    assert session(tmp_path, f).bench.ports[br.resolve_port(ESP)].may_reset_on_open is True


def test_two_quiet_opens_propose_listening_as_safe_pending_the_human(tmp_path):
    events = []
    br = session(tmp_path, fixture(tmp_path), CH340, events=events)
    for _ in range(2):
        aid = br.request("serial.listen", CH340, {"seconds": 0.1})["approval_id"]
        br.decide(aid, True)
        br.request("serial.listen", CH340, {"seconds": 0.1}, approval_id=aid)
    pending = [e for e in events if e["event"] == "memory_added" and e["entry"]["status"] == "pending"]
    assert len(pending) == 1 and pending[0]["entry"]["data"] == {"listen_safe": True}
    assert "2 times" in pending[0]["entry"]["text"]


def test_a_learned_trouble_line_is_watched_for(tmp_path):
    f = fixture(tmp_path, streams={VCP: [{"after": 0.3, "text": "E (1234) app: watchdog fired\n"}]})
    br = session(tmp_path, f)
    br.remember(VCP, "interpretation", "the app prints 'watchdog fired' when its main loop hangs",
                data={"pattern": "watchdog fired", "alert": "watchdog: main loop hung"})
    events = []
    br.on_event = events.append
    br.scan()  # memory is applied on scan
    br.select([br.bench.ports[br.resolve_port(VCP)].device_id])
    end = time.monotonic() + 3
    while not any(e.get("kind") == "alert" for e in events) and time.monotonic() < end:
        time.sleep(0.05)
    assert any(e.get("kind") == "alert" and e["what"] == "watchdog: main loop hung" for e in events)
    br.end_session()


def test_memory_files_are_plain_toml_and_entries_can_be_deleted(tmp_path):
    br = session(tmp_path, fixture(tmp_path))
    res = br.remember(CH340, "config", "CH1 of the scope is on the 3V3 rail", evidence="the human said so")
    files = list((tmp_path / "memory" / "devices").glob("*.toml"))
    assert len(files) == 1
    data = tomllib.loads(files[0].read_text())
    assert data["device"] == "QinHeng USB Serial" and data["entry"][0]["text"].startswith("CH1 of the scope")
    br.memory.delete(files[0].stem, res["id"])
    assert tomllib.loads(files[0].read_text()).get("entry", []) == []
