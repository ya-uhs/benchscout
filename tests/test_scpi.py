"""SCPI: only what the standard fixes is judged by name; anything else asks, and what the human allows covers
the command they saw, not others.

Review 2026-11-02 (docs/notes.md review U9, U12): a list of "output" command patterns decided which SCPI writes
were DANGEROUS on top of a default whose session grant covered every command, so each miss spread. No such list is
complete. Now nothing outside the standard runs before the human has seen it, a grant ("for the session", "always
on this instrument") is bound to the command header, and only an output setting the list knows asks every time.
The property tests below throw random command lines at that.
"""

import json
import string
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from benchscout import recon
from benchscout.actions import ArgError
from benchscout.broker import Broker
from benchscout.io import FixtureIo
from benchscout.policy import Effect, classify
from benchscout.protocols.scpi import _ENERGIZING, _SCPI_SAFE, scpi_headers

# What the standards fix as status or identity queries that change nothing, written here from the standards and not
# taken from the code (audit 2026-11-07, T7: the properties below took the code's own table as the answer, so a query
# wrongly added there passed them). IEEE 488.2 common queries: identity, operation complete, status byte, the enable
# registers, options, the setup as commands, power-on status clear flag, parallel poll enable. SCPI-99: the error
# queue's count and the SCPI version. Not here: *ESR? and SYST:ERR? (reading clears them), *TST? and *CAL? (they run
# a self-test or a calibration).
STANDARD_STATUS_QUERIES = {"*IDN?", "*OPC?", "*STB?", "*ESE?", "*SRE?", "*OPT?", "*LRN?", "*PSC?", "*PRE?",
                           "SYST:ERR:COUN?", "SYST:VERS?"}

FIXTURE = Path(__file__).parent.parent / "fixtures" / "bench_example.json"
PSU = "visa:TCPIP0::192.168.1.50::INSTR"
BENCH = recon.run(FixtureIo(FIXTURE)).bench


def effect(cmd: str, bench=BENCH) -> Effect:
    return classify(bench, "scpi.query", PSU, {"command": cmd}).effect


@pytest.fixture
def broker(tmp_path):
    f = tmp_path / "bench.json"
    f.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    io = FixtureIo(f)
    br = Broker(io, tmp_path / "ws" / ".benchscout")
    br.scan()
    br.select([br.bench.ports[PSU].device_id])
    yield br, io
    br.end_session()


def test_only_what_the_standards_fix_is_judged_safe_by_name():
    assert _SCPI_SAFE <= STANDARD_STATUS_QUERIES, _SCPI_SAFE - STANDARD_STATUS_QUERIES


# ---- what each rule says, by example ---------------------------------------------------------------

@pytest.mark.parametrize("cmd,expected", [
    # the standard's status and identity queries: SAFE, in any spelling
    ("*IDN?", Effect.SAFE), (":syst:err?", Effect.DISRUPTIVE), ("SYSTem:ERRor:NEXT?", Effect.DISRUPTIVE), ("*ESR?", Effect.DISRUPTIVE), ("*IDN?;*OPC?", Effect.SAFE),
    # anything else asks; what the human allows covers that command only
    ("MEAS:CURR? CH1", Effect.DISRUPTIVE), ("READ?", Effect.DISRUPTIVE), ("*CLS", Effect.DISRUPTIVE),
    (":TRIG:MODE SING", Effect.DISRUPTIVE), ("DISP:BRIG 5", Effect.DISRUPTIVE), ("SYST:BEEP", Effect.DISRUPTIVE),
    ("INIT", Effect.DISRUPTIVE), ("C1:BSWV WVTP,SINE,AMP,5", Effect.DISRUPTIVE),  # a generator's amplitude the list misses: still asks
    # output settings, whatever the vendor syntax, and what the standard says resets or recalls everything: every time
    ("OUTP CH1,ON", Effect.DANGEROUS), (":OUTPut ON", Effect.DANGEROUS), ("CH1:VOLTage 5", Effect.DANGEROUS),
    ("INST:NSEL 1;VOLT 5", Effect.DANGEROUS), (":SOUR1:VOLT 3.3", Effect.DANGEROUS), ("APPL 5,0.1", Effect.DANGEROUS),
    ("INP ON", Effect.DANGEROUS), ("PROG:RUN", Effect.DANGEROUS), ("*RST", Effect.DANGEROUS), ("*RCL 1", Effect.DANGEROUS),
    ("*TST?", Effect.DANGEROUS), ("*IDN?;OUTP ON", Effect.DANGEROUS),
])
def test_what_each_rule_says(cmd, expected):
    """Review 2026-11-02 (docs/notes.md U9, U12): only what the standard fixes is judged by name; a list of output
    commands only adds caution on top of a default that already asks, and a grant covers the command it was for."""
    assert effect(cmd) == expected, cmd


# ---- the hole that started this ------------------------------------------------------------------

@pytest.mark.parametrize("cmd", ["*IDN?\nOUTP ON", "*IDN?\r\nOUTP ON", "*IDN?\rOUTP ON", "*IDN?\x00OUTP ON",
                                 "*IDN?\x0bOUTP ON", "*IDN? OUTP ON", "*IDN?\u0085OUTP ON"])
def test_a_second_message_after_a_line_break_cannot_ride_on_a_safe_query(cmd, broker):
    """'*IDN?' + newline + 'OUTP ON' was SAFE: the check read one query, the instrument ran two messages."""
    br, io = broker
    with pytest.raises(ArgError):
        effect(cmd)
    res = br.request("scpi.query", PSU, {"command": cmd})
    assert res["status"] == "invalid_args" and "one SCPI message" in res["message"]
    assert not [c for c in io.calls if c[0].startswith("visa_") and c[2] == cmd], "nothing was sent"


def test_a_command_is_sent_without_waiting_for_an_answer_and_a_query_reads_one(broker):
    br, io = broker
    aid = br.request("scpi.write", PSU, {"command": "DISP:BRIG 5"}, wait=0)["approval_id"]
    br.decide(aid, True)
    assert br.request("scpi.write", PSU, {"command": "DISP:BRIG 5"}, approval_id=aid)["status"] == "done"
    assert ("visa_write", "TCPIP0::192.168.1.50::INSTR", "DISP:BRIG 5") in io.calls
    assert br.request("scpi.query", PSU, {"command": "*IDN?"})["status"] == "done"  # SAFE: no card
    assert ("visa_query", "TCPIP0::192.168.1.50::INSTR", "*IDN?") in io.calls


# ---- what the human allows covers the command they saw -------------------------------------------------

def test_a_session_grant_covers_that_command_with_any_values_and_nothing_else(broker):
    br, _ = broker
    first = br.request("scpi.write", PSU, {"command": "DISP:BRIG 5"}, wait=0)
    br.decide(first["approval_id"], True, "session")
    assert br.request("scpi.write", PSU, {"command": "DISP:BRIG 5"}, approval_id=first["approval_id"])["status"] == "done"
    assert br.request("scpi.write", PSU, {"command": "DISPlay:BRIGhtness 7"}, wait=0)["status"] == "done"
    assert br.request("scpi.write", PSU, {"command": "SYST:BEEP"}, wait=0)["status"] == "approval_required"
    assert br.request("scpi.write", PSU, {"command": "DISP:BRIG 5;SYST:BEEP"}, wait=0)["status"] == "approval_required"
    assert br.request("scpi.query", PSU, {"command": "DISP:BRIG?"}, wait=0)["status"] == "approval_required"


def test_always_on_this_instrument_is_remembered_as_the_humans_and_can_be_forgotten(broker, tmp_path):
    br, _ = broker
    res = br.request("scpi.query", PSU, {"command": "MEAS:VOLT? CH1"}, wait=0)
    br.decide(res["approval_id"], True, "always")
    assert br.request("scpi.query", PSU, {"command": "MEAS:VOLT? CH1"}, approval_id=res["approval_id"])["status"] == "done"
    assert effect("MEASure:VOLTage? CH2", br.bench) == Effect.SAFE, "remembered for this instrument"
    dev = br.bench.devices[br.bench.ports[PSU].device_id]
    entry = next(e for e in br.memory.entries(br._key(dev)) if e.kind == "command")
    assert entry.source == "human" and entry.status == "active" and entry.data == {"command": "MEAS:VOLT?", "effect": "safe"}
    again = Broker(FixtureIo(FIXTURE), tmp_path / "ws" / ".benchscout")  # the next time benchscout starts
    again.scan()
    assert effect("MEAS:VOLT?", again.bench) == Effect.SAFE
    br.forget(br._key(dev), entry.id)
    assert effect("MEAS:VOLT?", br.bench) == Effect.DISRUPTIVE


def test_an_output_setting_is_allowed_for_the_session_only_up_to_the_value_on_its_card(broker):
    """design.md section 12, 2026-11-08 (it was once only, so a sweep was a click per step): "for the session" lets the
    same setting run with values between 0 and the one approved; more, a keyword, or anything else beside it asks."""
    br, _ = broker
    res = br.request("scpi.write", PSU, {"command": "VOLT 5"}, wait=0)
    with pytest.raises(ValueError):
        br.decide(res["approval_id"], True, "always")
    br.decide(res["approval_id"], True, "session")
    for line in ("VOLT 5", "VOLT 3.3", "VOLT 0", "VOLTage 4.99"):
        assert br.request("scpi.write", PSU, {"command": line}, wait=0)["status"] == "done", line
    # SOUR:VOLT is another header than the VOLT on the card: a grant covers the header the human saw (it may mean the same)
    for line in ("VOLT 5.1", "VOLT -1", "VOLT MAX", "VOLT", "VOLT 3;OUTP ON", "CURR 1", "SOUR:VOLT 3"):
        assert br.request("scpi.write", PSU, {"command": line}, wait=0)["status"] == "approval_required", line


def test_beside_a_self_test_or_a_reset_an_output_setting_is_allowed_once_only(broker):
    br, _ = broker
    for line in ("VOLT 3;*TST?", "VOLT 3;*RST"):
        res = br.request("scpi.write", PSU, {"command": line}, wait=0)
        assert not br.approvals.get(res["approval_id"]).again, line
        with pytest.raises(ValueError):
            br.decide(res["approval_id"], True, "session")


# ---- memory: the agent learns, the human loosens --------------------------------------------------

def test_a_command_remembered_as_asking_less_waits_for_the_human(broker):
    br, _ = broker
    dev = br.bench.ports[PSU].device_id
    res = br.remember(dev, "command", "reads the output current; does not change the output (programming manual 4.2)",
                      data={"command": "MEAS:CURR?", "effect": "safe"})
    assert res["status"] == "pending_human" and res["port"] == PSU
    assert effect("MEAS:CURR? CH1", br.bench) == Effect.DISRUPTIVE, "not before the human confirms"
    key = br._key(br.bench.devices[dev])
    br.decide_memory(key, res["id"], True)
    assert effect("MEAS:CURR? CH1", br.bench) == Effect.SAFE
    assert effect("MEASure:CURRent? CH1", br.bench) == Effect.SAFE, "the long form is the same command"
    assert effect("MEAS:CURR?;OUTP ON", br.bench) == Effect.DANGEROUS, "it loosens that command only"
    br.forget(key, res["id"])
    assert effect("MEAS:CURR? CH1", br.bench) == Effect.DISRUPTIVE, "forgetting takes effect at once"


def test_a_command_remembered_as_stricter_applies_at_once(broker):
    br, _ = broker
    dev = br.bench.ports[PSU].device_id
    res = br.remember(dev, "command", "this SMU switches its output on to measure", data={"command": "READ?", "effect": "dangerous"})
    assert res["status"] == "remembered"
    assert effect("READ?", br.bench) == Effect.DANGEROUS
    loosen = br.remember(dev, "command", "only sets the voltage", data={"command": "SOUR:VOLT", "effect": "disruptive"})
    assert loosen["status"] == "pending_human", "DANGEROUS to DISRUPTIVE loosens too"
    assert br.remember(dev, "command", "x", data={"command": "VOLT 5;OUTP ON", "effect": "disruptive"})["status"] == "rejected"


def test_a_remembered_command_does_not_loosen_a_unit_the_instrument_may_read_another_way(broker):
    """'SOUR:VOLT 5;CURR 1': SCPI reads CURR as SOUR:CURR, an instrument keeping no path as CURR. Both must allow it."""
    br, _ = broker
    dev = br.bench.ports[PSU].device_id
    key = br._key(br.bench.devices[dev])
    for c in ("SOUR:VOLT", "SOUR:CURR"):
        r = br.remember(dev, "command", "sets a limit", data={"command": c, "effect": "disruptive"})
        br.decide_memory(key, r["id"], True)
    assert effect("SOUR:VOLT 5", br.bench) == Effect.DISRUPTIVE
    assert effect("SOUR:VOLT 5;CURR 1", br.bench) == Effect.DANGEROUS, "CURR alone is not remembered"
    assert effect("SOUR:VOLT 5;:SOUR:CURR 1", br.bench) == Effect.DISRUPTIVE


# ---- properties over random command lines ------------------------------------------------------------

LONG = ["MEASure", "VOLTage", "CURRent", "OUTPut", "SOURce", "SYSTem", "ERRor", "TRIGger", "INITiate", "DISPlay",
        "INPut", "POWer", "CONFigure", "SENSe", "READ", "FETCh", "CALibration", "MEMory", "NEXT", "VERSion", "CH1", "BSWV"]
node = st.sampled_from(LONG).flatmap(lambda w: st.sampled_from([w, w.upper(), w.lower(), "".join(c for c in w if c.isupper() or c.isdigit()) or w]))
common = st.sampled_from(["*IDN", "*OPC", "*ESR", "*STB", "*CLS", "*RST", "*TST", "*CAL", "*RCL", "*SAV", "*WAI", "*SRE"])
header = st.one_of(common, st.lists(node, min_size=1, max_size=4).map(":".join).flatmap(lambda h: st.sampled_from([h, ":" + h])))
arg = st.text(alphabet=string.ascii_letters + string.digits + ",.+- ", max_size=12)
unit = st.tuples(header, st.booleans(), arg).map(lambda t: t[0] + ("?" if t[1] else "") + (f" {t[2].strip()}" if t[2].strip() else ""))
line = st.lists(unit, min_size=1, max_size=4).map(";".join)


@settings(max_examples=400, deadline=None)
@given(line)
def test_safe_only_when_every_unit_is_a_status_query_the_standard_fixes(cmd):
    if effect(cmd) == Effect.SAFE:
        assert all(set(ways) <= STANDARD_STATUS_QUERIES for ways in scpi_headers(cmd)), cmd


@settings(max_examples=400, deadline=None)
@given(line)
def test_a_line_that_sets_an_output_asks_every_time(cmd):
    if any(not h.endswith("?") and _ENERGIZING.search(h) for ways in scpi_headers(cmd) for h in ways):
        assert effect(cmd) == Effect.DANGEROUS, cmd


@settings(max_examples=400, deadline=None)
@given(line)
def test_anything_outside_the_standards_status_queries_asks(cmd):
    if any(h not in STANDARD_STATUS_QUERIES for ways in scpi_headers(cmd) for h in ways):
        assert effect(cmd) >= Effect.DISRUPTIVE, cmd


@settings(max_examples=300, deadline=None)
@given(line, line)
def test_adding_a_unit_never_makes_a_line_ask_less(a, b):
    assert effect(f"{a};{b}") >= max(effect(a), effect(b))


@settings(max_examples=300, deadline=None)
@given(line)
def test_case_and_long_or_short_form_do_not_change_the_effect(cmd):
    assert effect(cmd.upper()) == effect(cmd.lower()) == effect(cmd)


@settings(max_examples=300, deadline=None)
@given(st.text(min_size=1, max_size=30))
def test_anything_but_one_printable_ascii_message_is_refused_or_judged(cmd):
    try:
        e = effect(cmd)
    except ArgError:
        assert any(not (32 <= ord(c) < 127 or c == "\t") for c in cmd) or not cmd.strip(" \t;")
        return
    assert e in (Effect.SAFE, Effect.DISRUPTIVE, Effect.DANGEROUS)
    if e == Effect.SAFE:
        assert all(set(ways) <= STANDARD_STATUS_QUERIES for ways in scpi_headers(cmd))


@settings(max_examples=150, deadline=None)
@given(st.lists(st.tuples(unit, st.sampled_from(["safe", "disruptive", "dangerous"])), max_size=5), line)
def test_memory_the_human_has_not_confirmed_never_makes_anything_ask_less(entries, cmd):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    import tempfile

    with tempfile.TemporaryDirectory() as d, pytest.MonkeyPatch.context() as mp:
        mp.setenv("BENCHSCOUT_MEMORY", str(Path(d) / "memory"))  # each example its own memory
        f = Path(d) / "bench.json"
        f.write_text(json.dumps(data), encoding="utf-8")
        br = Broker(FixtureIo(f), Path(d) / "ws" / ".benchscout")
        br.scan()
        before = effect(cmd, br.bench)
        dev = br.bench.ports[PSU].device_id
        for text, eff in entries:
            br.remember(dev, "command", "agent says so", data={"command": text, "effect": eff})
        assert effect(cmd, br.bench) >= before, "only the human loosens"


# ---- a screen capture or a waveform, saved in the workspace -----------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 12
SCOPE = "visa:USB0::0x1AB1::0x04CE::DS1ZA201804321::INSTR"


def test_a_capture_is_saved_as_a_file_without_its_block_header(tmp_path):
    """design.md section 2: a scope's screen (PNG) and waveform (CSV) are saved in the workspace; the command that
    gives them is the instrument's (its manual), so benchscout only takes the IEEE 488.2 block apart."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    res = "USB0::0x1AB1::0x04CE::DS1ZA201804321::INSTR"
    data["visa_raw"] = {res: {":DISP:DATA? ON,OFF,PNG": (b"#9%09d" % len(PNG) + PNG + b"\n").hex(),
                              ":WAV:DATA?": (b"#9000000015" + b"0.1,0.2,0.3,0.4" + b"\n").hex()}}
    f = tmp_path / "bench.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    br = Broker(FixtureIo(f), tmp_path / "ws" / ".benchscout", approval_wait=0)
    br.scan()
    br.select([br.bench.ports[SCOPE].device_id])
    ask = br.request("scpi.query", SCOPE, {"command": ":DISP:DATA? ON,OFF,PNG", "save": "png"})
    assert ask["status"] == "approval_required", "a query outside the standard asks, saved or not"
    br.decide(ask["approval_id"], True, "always")
    shot = br.request("scpi.query", SCOPE, {"command": ":DISP:DATA? ON,OFF,PNG", "save": "png"}, approval_id=ask["approval_id"])
    saved = Path(shot["result"]["saved_to"])
    assert saved.read_bytes() == PNG and saved.suffix == ".png" and saved.is_relative_to(tmp_path / "ws")
    wave = br.request("scpi.query", SCOPE, {"command": ":WAV:DATA?", "save": "csv"})
    if wave["status"] == "approval_required":
        br.decide(wave["approval_id"], True)
        wave = br.request("scpi.query", SCOPE, {"command": ":WAV:DATA?", "save": "csv"}, approval_id=wave["approval_id"])
    assert wave["result"]["preview"] == "0.1,0.2,0.3,0.4"
    assert br.request("scpi.query", SCOPE, {"command": ":DISP:DATA? ON,OFF,PNG", "save": "gif"})["result"]["error"].startswith("save must be")
    br.end_session()


def test_a_capture_cut_short_says_so(tmp_path):
    """Audit 2026-11-07 (15): a block shorter than its header says (a time-out, a cut connection) was saved as whole."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    res = "USB0::0x1AB1::0x04CE::DS1ZA201804321::INSTR"
    data["visa_raw"] = {res: {":DISP:DATA? ON,OFF,PNG": (b"#9%09d" % (len(PNG) + 100) + PNG).hex()}}
    f = tmp_path / "bench.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    br = Broker(FixtureIo(f), tmp_path / "ws" / ".benchscout", approval_wait=0)
    br.scan()
    br.select([br.bench.ports[SCOPE].device_id])
    ask = br.request("scpi.query", SCOPE, {"command": ":DISP:DATA? ON,OFF,PNG", "save": "png"})
    br.decide(ask["approval_id"], True)
    shot = br.request("scpi.query", SCOPE, {"command": ":DISP:DATA? ON,OFF,PNG", "save": "png"}, approval_id=ask["approval_id"])
    assert "incomplete" in shot["result"]["warning"]
    br.end_session()


def test_an_ieee_block_is_taken_apart_and_anything_else_is_kept():
    from benchscout.protocols.scpi import ieee_block

    assert ieee_block(b"#15hello\n") == b"hello"
    assert ieee_block(b"#0raw bytes to the end\n") == b"raw bytes to the end"
    assert ieee_block(b"1.23E-3\n") == b"1.23E-3\n", "not a block: as it came"
    assert ieee_block(b"#x") == b"#x" and ieee_block(b"#9123") == b"#9123"

