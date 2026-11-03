"""Memory (design.md section 6) and naming by quoting the device (section 3)."""

import json
import time
import tomllib
from pathlib import Path

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


def test_a_port_remembered_as_safe_to_open_that_resets_when_opened_asks_again(tmp_path):
    """Memory may be stale (design.md section 6): new firmware, or another board, can reset on open where the one
    remembered did not. The newer observation wins at once, and the next session still asks."""
    events = []
    f = fixture(tmp_path, streams={ESP: [{"after": 0.3, "vanish": 0.4}, {"after": 0.9, "text": f"{BANNER}\n"}]})
    br = session(tmp_path, f, events=events)
    dev = br.bench.devices[br.bench.ports[br.resolve_port(ESP)].device_id]
    res = br.remember(ESP, "safety", "opening does not reset it", data={"listen_safe": True})
    br.decide_memory(br._key(dev), res["id"], True)
    port = br.resolve_port(ESP)
    assert br.select([dev.id])["watching"] == [port], "remembered as safe to open: watched without asking"
    for _ in range(150):  # the last step: the human is told (the port asks again before that, at once)
        if any(e["event"] == "memory_added" and e["entry"]["data"].get("listen_safe") is False for e in events):
            break
        time.sleep(0.02)
    assert br.bench.ports[port].may_reset_on_open, "it vanished 0.3 s after opening: opening asks again, now"
    status = {e.id: e.status for e in br.memory.entries(br._key(dev))}
    assert status[res["id"]] == "contradicted", "kept as history, no longer in force"
    stricter = [e for e in br.memory.entries(br._key(dev)) if e.kind == "safety" and e.data.get("listen_safe") is False]
    assert len(stricter) == 1 and stricter[0].status == "active" and "asks first" in stricter[0].text
    assert any(e["event"] == "memory_added" and e["entry"]["id"] == stricter[0].id for e in events), "the human is told"
    br.end_session()
    again = session(tmp_path, fixture(tmp_path))
    assert again.request("serial.listen", ESP, {"seconds": 0.1})["status"] == "not_selected"
    assert again.impact("serial.listen", ESP).effect.name == "DISRUPTIVE", "the next session asks too"
    again.end_session()


def test_two_quiet_opens_propose_listening_as_safe_pending_the_human(tmp_path):
    events = []
    br = session(tmp_path, fixture(tmp_path), ESP, CH340, events=events)
    for port in (ESP, CH340):
        for _ in range(2):
            aid = br.request("serial.listen", port, {"seconds": 0.1})["approval_id"]
            br.decide(aid, True)
            br.request("serial.listen", port, {"seconds": 0.1}, approval_id=aid)
    pending = [e for e in events if e["event"] == "memory_added" and e["entry"]["status"] == "pending"]
    assert len(pending) == 1 and pending[0]["entry"]["data"] == {"listen_safe": True, "seen": 2}
    assert pending[0]["entry"]["target"] == br.resolve_port(ESP) and "2 times" in pending[0]["entry"]["text"]
    # review 7 (19): behind a USB-UART bridge the port stays whether or not the MCU reset, so staying proves nothing
    ch340 = br.bench.devices[br.bench.ports[br.resolve_port(CH340)].device_id]
    assert not [e for e in br.memory.entries(br._key(ch340)) if e.kind in ("observation", "safety")]


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


# ---- firmware commands (design.md section 4) ----------------------------------------------

def _cmd_bench(tmp_path, path="/dev/cu.usbserial-10"):
    import json as _json
    from benchscout.broker import Broker
    from benchscout.io import FixtureIo

    fx = Path(__file__).parent.parent / "fixtures" / "bench_example.json"
    data = _json.loads(fx.read_text())
    data["streams"] = {path: [{"after": 0.05, "text": "boot\n"}]}
    data.setdefault("replies", {})[path] = {"status\r\n": "uptime 42s\n", "status\n": "wrong eol\n"}
    f = tmp_path / "bench.json"
    f.write_text(_json.dumps(data))
    br = Broker(FixtureIo(f), tmp_path / "ws" / ".benchscout")
    br.scan()
    br.select([br.bench.ports[br.resolve_port(path)].device_id])
    return br


UART = ESP  # opening may reset the chip (its own USB), on a unit with its own serial


def test_a_command_remembered_as_safe_waits_for_the_human_then_runs_without_asking(tmp_path):
    br = _cmd_bench(tmp_path, UART)
    port = br.resolve_port(UART)
    r = br.remember(port, "command", "prints uptime", data={"command": "status", "effect": "safe", "eol": "\r\n"})
    assert r["status"] == "pending_human"
    first = br.request("serial.command", port, {"name": "status"})
    assert first["status"] == "approval_required", "until the human confirms, it is gated like serial.send"
    assert first["impact"]["code"] == "command_unconfirmed"
    key = br._key(br.bench.devices[br.bench.ports[port].device_id])
    br.decide_memory(key, r["id"], True)
    closed = br.request("serial.command", port, {"name": "status"})
    assert closed["status"] == "approval_required" and closed["impact"]["code"] == "reset_on_open", \
        "safe once sent, but sending it opens a port that may reset the board: the open is a step of the call"
    br.decide(br.watch_offers[port], True)  # the human: watch it (held open from now on)
    for _ in range(100):
        if br._held_open(port, {}):
            break
        time.sleep(0.02)
    done = br.request("serial.command", port, {"name": "status"})
    assert done["status"] == "done" and done["impact"]["effect"] == "safe"
    assert "uptime 42s" in done["result"]["reply"], "sent with the remembered line ending"
    assert done["impact"]["code"] == "command_safe"
    assert "remembered as SAFE (confirmed by the human): prints uptime" in done["impact"]["reason"]
    br.end_session()


def test_a_unit_without_its_own_serial_is_not_loosened_by_memory(tmp_path):
    """Review 7 (9): memory is kept by model and serial; a board with no serial shares it with every other unit of its
    model, so what loosens safety (a command confirmed as safe) must not carry over to whichever one is plugged in."""
    br = _cmd_bench(tmp_path)  # the CH340: no serial number
    port = br.resolve_port("/dev/cu.usbserial-10")
    r = br.remember(port, "command", "prints uptime", data={"command": "status", "effect": "safe", "eol": "\r\n"})
    br.decide_memory(br._key(br.bench.devices[br.bench.ports[port].device_id]), r["id"], True)
    assert br.impact("serial.command", port, {"name": "status"}).effect.name != "SAFE"
    br.end_session()


def test_unknown_or_disruptive_commands_are_not_loosened(tmp_path):
    br = _cmd_bench(tmp_path)
    bad = br.request("serial.command", "serial:usbserial-10", {"name": "reboot"})
    assert bad["status"] == "invalid_args" and "none yet" in bad["message"]
    r = br.remember("serial:usbserial-10", "command", "restarts the firmware", data={"command": "reboot", "effect": "disruptive"})
    assert r["status"] == "remembered"
    assert br.request("serial.command", "serial:usbserial-10", {"name": "reboot"})["status"] == "approval_required"
    assert br.remember("serial:usbserial-10", "command", "x", data={"effect": "safe"})["status"] == "rejected"
    assert br.remember("serial:usbserial-10", "command", "x", data={"command": "a\nb"})["status"] == "rejected"
    # a later scan still knows the commands, and shows them to the agent
    br.scan()
    assert "reboot" in br.bench.ports["serial:usbserial-10"].commands
    facts = [f for t in br.bench.targets_via("serial:usbserial-10") for f in t.facts]
    assert any("command `reboot`" in f for f in facts)
    br.end_session()



def test_what_waits_for_the_human_is_shown_again_after_a_restart_but_only_once(tmp_path):
    """Real bench 2026-11-01: a "treat listening as safe?" card was lost when the window reloaded."""
    from benchscout.memory import Entry

    fx = Path(__file__).parent.parent / "fixtures" / "bench_example.json"
    first = Broker(FixtureIo(fx), tmp_path / "ws" / ".benchscout")
    first.scan()
    dev = first.bench.devices[first.bench.ports["serial:usbserial-10"].device_id]
    key = first._key(dev)
    first.memory.add(key, Entry(kind="safety", source="benchscout", target="serial:usbserial-10", status="pending",
                                text="treat listening as SAFE?", data={"listen_safe": True}), first._header(dev))
    first.memory.add("ffff-ffff-not-connected", Entry(kind="safety", source="benchscout", target="x", status="pending",
                                                      text="a device that is not plugged in", data={"listen_safe": True}))
    later = Broker(FixtureIo(fx), tmp_path / "ws" / ".benchscout")  # the next session
    events = []
    later.on_event = events.append
    later.scan()
    later.scan()
    shown = [e["entry"]["text"] for e in events if e["event"] == "memory_added"]
    assert shown == ["treat listening as SAFE?"], "once, and nothing about devices that are not connected"



def test_memory_in_japanese_round_trips(tmp_path):
    """Files are UTF-8 on every OS (Windows' default code page cannot hold what the agent writes in Japanese)."""
    from benchscout.memory import Entry, Memory

    m = Memory(tmp_path / "ws", root=tmp_path / "mem")
    m.add("k", Entry(kind="observation", source="agent", text="UART のカウンタは続いている(リセットなし)", evidence="hb 0000002a"))
    again = Memory(tmp_path / "ws", root=tmp_path / "mem")
    assert again.entries("k")[0].text == "UART のカウンタは続いている(リセットなし)"


def test_the_inbox_lists_what_is_remembered_per_device_with_its_file(tmp_path):
    """design.md section 6: the person sees, fixes (in the file) and deletes what was remembered."""
    br = Broker(FixtureIo(FIXTURE), tmp_path / "ws" / ".benchscout")
    events = []
    br.on_event = events.append
    br.scan()
    dev = br.bench.ports["serial:usbserial-10"].device_id
    a = br.remember(dev, "observation", "prints [I] sensor ok every second")
    br.remember(dev, "config", "TX goes to the sensor board")
    (group,) = [g for g in br.memory_view() if g["device"] == dev]
    assert [e["text"] for e in group["entries"]] == ["prints [I] sensor ok every second", "TX goes to the sensor board"]
    assert Path(group["file"]).is_file() and group["key"] in group["file"]
    br.forget(group["key"], a["id"])
    assert any(e["event"] == "memory_forgotten" and e["id"] == a["id"] for e in events), "the inbox hears it at once"
    assert [e["text"] for g in br.memory_view() if g["device"] == dev for e in g["entries"]] == ["TX goes to the sensor board"]
