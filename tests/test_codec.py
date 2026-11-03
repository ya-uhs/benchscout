"""A shell's answer told apart from what the device prints by itself (design.md section 4, review R7).

Recorded on the real bench (2026-10-31, docs/notes.md 実機 15-17): the ESP32-S3 test firmware answers `status` with a
line and "ok" while its heartbeat goes on ("hb n=28 uptime_ms=491535" arrived in the same read); the Tang Nano's
bitstream answers one byte with its banner between "hb 0000025d" lines. Before, the whole read came back as one text.
"""

import json
import re
import time
from pathlib import Path

from benchscout.broker import Broker
from benchscout.codec import split
from benchscout.io import FixtureIo

REAL = Path(__file__).parent.parent / "fixtures" / "real"
STATUS = (REAL / "esp32s3-testfw-send-status.txt").read_text(encoding="utf-8")
ESP_OWN = (REAL / "esp32s3-testfw-unprompted.txt").read_text(encoding="utf-8")


def test_the_answer_is_told_from_the_heartbeat_that_arrived_with_it():
    got = split(STATUS, "status", ESP_OWN, None)
    assert got["reply"].splitlines() == [STATUS.splitlines()[0], "ok"]
    assert got["unsolicited"] == "hb n=28 uptime_ms=491535", "shaped like the heartbeat it printed before (numbers ignored)"

    ended = split(STATUS, "status", ESP_OWN, re.compile(r"^(ok|err\b)"))
    assert ended["reply"].splitlines()[-1] == "ok" and "ok" in ended["ended"]
    assert ended["unsolicited"] == "hb n=28 uptime_ms=491535", "what came after the end line is not the answer"

    unwatched = split(STATUS, "status", None, None)
    assert "hb n=28" in unwatched["reply"] and "not watched" in unwatched["how"], "nothing to compare with: said, not guessed"

    # 2026-11-03, watched through the product path: five heartbeats, then `status` sent; the read stopped at "ok" after
    # 0.2 s (it was given 5), before the next heartbeat
    watched = (REAL / "esp32s3-testfw-watched-status-2026-11-03.txt").read_text(encoding="utf-8").splitlines(keepends=True)
    live = split("".join(watched[5:]), "status", "".join(watched[:5]), re.compile(r"^(ok|err)"))
    assert live["reply"].splitlines() == [watched[5].strip(), "ok"] and "unsolicited" not in live and "ok" in live["ended"]

    tang = split((REAL / "tangnano9k-testbit-send-underscore.txt").read_text(encoding="utf-8"), "_",
                 (REAL / "tangnano9k-testbit-unprompted.txt").read_text(encoding="utf-8"), None)
    assert tang["reply"] == "benchscout-testbit tangnano9k v1 baud=115200"
    assert tang["unsolicited"].splitlines() == ["hb 0000025d", "hb 0000025e"], "hex counters are numbers too"


def test_a_watched_shell_stops_at_its_end_line_and_earlier_answers_are_not_taken_for_its_own_lines(tmp_path):
    vcp = "/dev/cu.usbmodem21403"  # a port SAFE to watch, playing the ESP32-S3 firmware's recorded output
    data = json.loads((Path(__file__).parent.parent / "fixtures" / "bench_example.json").read_text(encoding="utf-8"))
    heartbeat = [l for l in ESP_OWN.splitlines() if l.startswith("hb ")]
    data["streams"] = {vcp: [{"after": 0.02 * i, "text": l + "\n"} for i, l in enumerate(heartbeat)]}
    data["replies"] = {vcp: {"status\n": STATUS.split("hb n=28")[0]}}
    f = tmp_path / "bench.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    br = Broker(FixtureIo(f), tmp_path / "ws" / ".benchscout", approval_wait=0)
    br.scan()
    port = br.resolve_port(vcp)
    br.select([br.bench.ports[port].device_id])
    assert br.watches[port].alive
    time.sleep(0.8)  # its heartbeat goes by

    bad = br.request("serial.send", port, {"data": "status", "until": "^(ok"})
    assert bad["status"] == "invalid_args" and "regular expression" in bad["message"], "said before any approval"
    asked = br.request("serial.send", port, {"data": "status", "seconds": 5, "until": "^(ok|err)"})
    br.decide(asked["approval_id"], True, "session")
    for _ in range(2):  # the second time, the first answer's "ok" must not count as a line it prints by itself
        t0 = time.monotonic()
        done = br.request("serial.send", port, {"data": "status", "seconds": 5, "until": "^(ok|err)"})
        assert time.monotonic() - t0 < 2, "stopped at the end line, not after 5 s"
        r = done["result"]
        assert r["reply"].splitlines()[-1] == "ok" and "uptime_ms=490807" in r["reply"], r
        assert "hb n=" not in r["reply"]
        assert Path(r["saved_to"]).read_text(encoding="utf-8").startswith("uptime_ms=490807"), "the file has it all"
    br.end_session()
