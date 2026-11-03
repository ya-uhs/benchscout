import json
from pathlib import Path

import pytest

from benchscout import recon
from benchscout.broker import Broker
from benchscout.io import FixtureIo
from benchscout.model import Confidence
from benchscout.policy import Effect, classify

FIXTURE = Path(__file__).parent.parent / "fixtures" / "bench_example.json"
REAL = Path(__file__).parent.parent / "fixtures" / "real"


@pytest.fixture
def io():
    return FixtureIo(FIXTURE)


@pytest.fixture
def r(io):
    return recon.run(io)


def target_named(b, name):
    return next(t for t in b.targets.values() if t.name == name)


def test_a_usb_device_benchscout_has_no_data_for_is_listed_apart_not_hidden(r):
    """2026-11-02: hiding them also hid programmer cables benchscout has no data for, so the agent could not see
    them. Now every non-hub USB device is listed with its raw clues; those it knows nothing about stay apart."""
    from benchscout import report

    kb = next(d for d in r.bench.devices.values() if "Keychron" in d.name)
    assert kb.other and not r.bench.ports_of(kb.id)
    text = report.render(r)
    assert f"■ {kb.name}" not in text and "Other USB devices" in text and kb.name in text.split("Other USB devices")[1]


def esp_target(b):
    return b.targets_via("serial:usbmodem1101")[0]

def test_one_chip_two_ports(r):
    b = r.bench
    esp = esp_target(b)
    kinds = sorted(p.kind for p in b.ports_reaching(esp.id))
    assert kinds == ["jtag", "serial"]
    # native USB resets on open, so recon stops at the descriptor; naming it is the agent's job
    assert esp.confidence == Confidence.UNKNOWN and esp.name is None


def test_one_usb_device_two_targets(r):
    b = r.bench
    ftdi = next(d for d in b.devices.values() if d.name == "Digilent USB Device")  # what it calls itself
    kinds = sorted(p.kind for p in b.ports_of(ftdi.id))
    assert kinds == ["jtag", "serial"]
    fabric = next(t for t in b.targets.values() if t.hosted_on)
    assert any(p.kind == "jtag" for p in b.ports_reaching(fabric.hosted_on))


def test_unknown_stays_unknown(r):
    b = r.bench
    assert "browning out" not in " | ".join(p.text for p in r.proposals)  # nothing heard yet, nothing claimed
    ch340 = next(p for p in b.ports.values() if p.path == "/dev/cu.usbserial-10")
    (t,) = b.targets_via(ch340.id)
    assert t.name is None and t.confidence == Confidence.UNKNOWN
    # FPGA part is unknown until a JTAG scan actually runs
    fpga = next(t for t in b.targets.values() if any(p.kind == "jtag" for p in b.ports_reaching(t.id)))
    assert fpga.name is None


def test_stlink_swd_and_vcp_reach_same_target(r):
    b = r.bench
    swd = next(p for p in b.ports.values() if p.kind == "swd")
    vcp = next(p for p in b.ports.values() if p.path == "/dev/cu.usbmodem21403")
    assert b.targets_via(swd.id) == b.targets_via(vcp.id)


def test_recon_runs_only_safe_probes(io, r):
    # CH340 bridge and native-USB ESP32 may reset on open -> never opened during recon
    assert ("serial_listen", "/dev/cu.usbserial-10") not in io.calls
    assert ("serial_listen", "/dev/cu.usbmodem1101") not in io.calls
    assert ("serial_listen", "/dev/cu.usbmodem21403") in io.calls  # probe VCP is safe
    for p in r.bench.ports.values():
        if p.kind == "serial" and ("serial_listen", p.path) in io.calls:
            assert classify(r.bench, "serial.listen", p.id).effect == Effect.SAFE


def test_instruments_identified_by_idn(r):
    b = r.bench
    scope = target_named(b, "Rigol DS1054Z")
    assert any(e.source == "scpi *IDN?" for e in scope.evidence)
    assert target_named(b, "Siglent SPD3303X")
    assert "oscilloscope" not in scope.facts  # what kind of instrument it is, is the agent's reading

def test_fpga_program_disturbs_fabric_uart(r):
    b = r.bench
    jtag = next(p for p in b.ports.values() if p.kind == "jtag" and p.id.startswith("jtag:2103"))
    uart = next(p for p in b.ports.values() if p.path and p.path.endswith("C7D11"))
    imp = classify(b, "fpga.program", jtag.id)
    assert imp.effect == Effect.DANGEROUS
    assert uart.id in imp.affected_ports


def test_esptool_read_is_disruptive_and_hits_jtag(r):
    b = r.bench
    imp = classify(b, "esptool.chip_id", "serial:usbmodem1101")
    assert imp.effect == Effect.DISRUPTIVE
    assert any(p.startswith("jtag:") for p in imp.affected_ports)


def session(io, tmp_path, *ports):
    """A broker whose session holds the devices behind these ports (the human selected them)."""
    br = Broker(io, tmp_path)
    br.scan()
    br.select([br.bench.ports[p].device_id for p in ports])
    return br


def test_approved_listen_hears_the_banner_but_only_a_quoted_identify_names_the_chip(io, tmp_path):
    br = session(io, tmp_path, "serial:usbmodem1101")
    aid = br.request("serial.listen", "serial:usbmodem1101")["approval_id"]
    br.decide(aid, True)
    assert br.request("serial.listen", "serial:usbmodem1101", approval_id=aid)["status"] == "done"
    esp = esp_target(br.bench)
    assert esp.name is None, "hearing a banner names nothing: the agent reads it"
    assert any(f.startswith("log: ESP-ROM:esp32s3") for f in esp.facts)
    assert br.identify("serial:usbmodem1101", "ESP32-S3", "ESP-ROM:esp32s3-20210327")["status"] == "identified"
    assert esp_target(br.bench).name == "ESP32-S3"
    texts = " | ".join(p.text for p in br.last.proposals)
    assert "usbmodem1101" not in texts  # already listened

def test_every_proposal_says_why(r):
    assert r.proposals
    for p in r.proposals:
        assert p.reason, p.text
    ch340 = next(p for p in r.proposals if "usbserial-10" in p.text)
    assert "DTR/RTS" in ch340.reason


def test_proposal_reason_matches_policy(r):
    jtag = next(p for p in r.proposals if p.action == "openFPGALoader.detect")
    assert jtag.reason == classify(r.bench, "openFPGALoader.detect", jtag.port).reason


@pytest.mark.parametrize(
    "explicit,env,expected",
    [
        ("ja", {}, "Japanese"),
        (None, {"BENCHSCOUT_LANG": "en"}, "English"),
        (None, {"LANG": "ja_JP.UTF-8"}, "Japanese"),
        (None, {"LANG": "C"}, "English"),
        ("pt-BR", {}, "pt"),
    ],
)
def test_user_language(monkeypatch, explicit, env, expected):
    from benchscout.cli import user_language

    for k in ("BENCHSCOUT_LANG", "LC_ALL", "LANG"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert user_language(explicit) == expected


def test_approval_gate(io, tmp_path):
    br = session(io, tmp_path, "serial:usbserial-10")
    res = br.request("serial.listen", "serial:usbserial-10")
    assert res["status"] == "approval_required"
    aid = res["approval_id"]
    assert ("serial_listen", "/dev/cu.usbserial-10") not in io.calls

    assert br.request("serial.listen", "serial:usbserial-10", approval_id=aid)["status"] == "approval_required"
    br.decide(aid, True)
    # an approval cannot be reused for a different action
    assert br.request("serial.listen", "serial:usbserial-10", {"seconds": 1}, approval_id=aid)["status"] == "approval_mismatch"
    done = br.request("serial.listen", "serial:usbserial-10", approval_id=aid)
    assert done["status"] == "done"
    assert Path(done["result"]["saved_to"]).read_text().startswith("[I] sensor ok")
    # single use
    assert br.request("serial.listen", "serial:usbserial-10", approval_id=aid)["status"] == "used"


def test_visa_serial_resources_are_never_sent_idn(tmp_path):
    data = json.loads(FIXTURE.read_text())
    data["visa"]["ASRL/dev/cu.usbserial-10::INSTR"] = {"*IDN?": "should never be asked"}
    f = tmp_path / "bench.json"
    f.write_text(json.dumps(data))
    io = FixtureIo(f)
    r = recon.run(io)
    assert not any(c[0] == "visa_query" and c[1].startswith("ASRL") for c in io.calls)
    assert not any(p.id.startswith("visa:ASRL") for p in r.bench.ports.values())


def _variant(tmp_path, mutate):
    data = json.loads(FIXTURE.read_text())
    mutate(data)
    f = tmp_path / "bench.json"
    f.write_text(json.dumps(data))
    return FixtureIo(f)


def test_port_vanishing_mid_read_is_a_fact_not_a_crash(tmp_path):
    io = _variant(tmp_path, lambda d: d["listen"].update({"/dev/cu.usbmodem21403": {"error": "Device not configured"}}))
    r = recon.run(io)
    vcp = next(p for p in r.bench.ports.values() if p.path == "/dev/cu.usbmodem21403")
    (t,) = r.bench.targets_via(vcp.id)
    assert any("port disappeared" in f for f in t.facts)


def test_tang_nano_9k(tmp_path):
    def mutate(d):
        d["usb"] = [{"name": "JTAG Debugger", "vendor": "SIPEED", "vid": "0x0403", "pid": "0x6010", "serial": "FactoryAIOT Pro", "location": "0x01100000"}]
        d["serial"] = [
            {"device": "/dev/cu.usbserial-FactoryAIOT0", "vid": "0x0403", "pid": "0x6010", "serial_number": "FactoryAIOT Pro", "channel": 0},
            {"device": "/dev/cu.usbserial-FactoryAIOT1", "vid": "0x0403", "pid": "0x6010", "serial_number": "FactoryAIOT Pro", "channel": 1},
        ]
        d["visa"] = {}
        d["tools"] = ["openFPGALoader"]
        # raw output captured from a real Tang Nano 9K (openFPGALoader v1.2.0)
        d["probes"] = {"FactoryAIOT Pro": "No cable or board specified: using direct ft2232 interface\n"
                     "Jtag frequency : requested 6.00MHz    -> real 6.00MHz   \nindex 0:\n\tidcode 0x100481b\n"
                     "\tmanufacturer Gowin\n\tfamily GW1N\n\tmodel  GW1N(R)-9C\n\tirlength 8\n"}
    io = _variant(tmp_path, mutate)
    r = recon.run(io)
    fpga = target_named(r.bench, "Gowin GW1N(R)-9C")
    assert any("openFPGALoader's database" in e.detail for e in fpga.evidence), "the evidence says which database decided"
    assert fpga.confidence == Confidence.IDENTIFIED
    fabric = next(t for t in r.bench.targets.values() if t.hosted_on)
    assert fabric.hosted_on == fpga.id
    (dev,) = r.bench.devices.values()
    assert dev.name == "SIPEED JTAG Debugger"  # what the BL702 debugger calls itself
    assert all(" " not in pid for pid in r.bench.ports)


def test_idcode_without_a_tool_database_stays_unknown(tmp_path):
    """Without openFPGALoader's model line there is no name: benchscout keeps no IDCODE table;
    the raw IDCODE is the clue for the agent."""
    io = _variant(tmp_path, lambda d: (d["tools"].append("openFPGALoader"), d["probes"].update({"210319A8C7D1": "index 0:\n\tidcode 0x0362D093\n"})))
    r = recon.run(io)
    fpga = next(t for t in r.bench.targets.values() if any(p.kind == "jtag" for p in r.bench.ports_reaching(t.id)))
    assert fpga.name is None
    assert any("0x0362D093" in e.detail for e in fpga.evidence)

def test_each_part_on_a_jtag_chain_is_its_own_target(tmp_path):
    """Review 7: with two parts on the chain, the second one's IDCODE was written over the first's target."""
    io = _variant(tmp_path, lambda d: (d["tools"].append("openFPGALoader"),
                                       d["probes"].update({"210319A8C7D1": "index 0:\n\tidcode 0x0362D093\nindex 1:\n\tidcode 0x4BA00477\n"})))
    r = recon.run(io)
    port = next(p for p in r.bench.ports.values() if p.kind == "jtag" and "210319A8C7D1" in p.id)
    codes = {t.id: [e.detail for e in t.evidence if e.source == "jtag idcode"] for t in r.bench.targets_via(port.id)}
    assert sum(1 for v in codes.values() if v) == 2 and all(len(v) <= 1 for v in codes.values()), codes


def test_openfpgaloader_only_reaches_probes_the_usb_data_says_how_to(tmp_path):
    io = _variant(tmp_path, lambda d: d["tools"].append("openFPGALoader"))
    recon.run(io)
    scanned = [c[1][1:-1] for c in io.calls if c[0] == "probe"]
    # the FT2232H board, with the cable options from usb.toml; not the ESP32's USB-JTAG (no entry for it)
    assert scanned == [("-c", "ft2232", "--ftdi-serial", "210319A8C7D1", "--ftdi-channel", "0")]


def test_port_that_vanishes_and_comes_back_still_yields_the_boot_log(tmp_path):
    """Real bench, 2026-10-31: opening the ESP32's native USB port reset it; the port vanished
    and came back about a second later. Reading on catches the boot banner."""
    boot = "ESP-ROM:esp32s3-20210327\nrst:0x15 (USB_UART_CHIP_RESET),boot:0x8 (SPI_FAST_FLASH_BOOT)\n"
    io = _variant(tmp_path, lambda d: d["listen"].update({"/dev/cu.usbmodem1101": {"then": boot}}))
    br = Broker(io, tmp_path / "s")
    br.scan()
    br.select([br.bench.ports["serial:usbmodem1101"].device_id])
    aid = br.request("serial.listen", "serial:usbmodem1101")["approval_id"]
    br.decide(aid, True)
    assert br.request("serial.listen", "serial:usbmodem1101", approval_id=aid)["status"] == "done"
    esp = esp_target(br.bench)
    assert any("ESP-ROM:esp32s3" in f for f in esp.facts)
    assert any("vanished" in f and "came back" in f for f in esp.facts)
    assert not any("benchscout]" in f for f in esp.facts)  # our marker is not reported as device output
    assert io.lines[-1] == "asserted"  # dropping DTR/RTS reset this chip on every open


def test_what_was_learned_survives_a_rescan(tmp_path):
    """Real bench, 2026-10-31: a reboot printed the ESP-ROM banner, then bench_scan rebuilt the
    graph and the ESP32 was back to unnamed. Now the agent names it with a quote and memory keeps it."""
    boot = "ok\nESP-ROM:esp32s3-20210327\nrst:0xc (RTC_SW_CPU_RST),boot:0x28 (SPI_FAST_FLASH_BOOT)\n"
    data = json.loads(FIXTURE.read_text())
    data["replies"] = {"/dev/cu.usbmodem1101": {"reboot\n": boot}}
    f = tmp_path / "bench.json"
    f.write_text(json.dumps(data))
    br = Broker(FixtureIo(f), tmp_path / "s")
    br.scan()
    br.select([br.bench.ports["serial:usbmodem1101"].device_id])
    aid = br.request("serial.send", "serial:usbmodem1101", {"data": "reboot"})["approval_id"]
    br.decide(aid, True)
    br.request("serial.send", "serial:usbmodem1101", {"data": "reboot"}, approval_id=aid)
    assert br.identify("serial:usbmodem1101", "ESP32-S3", "ESP-ROM:esp32s3-20210327")["status"] == "identified"
    br.scan()
    esp = esp_target(br.bench)
    assert esp.name == "ESP32-S3" and esp.confidence == Confidence.IDENTIFIED
    assert any(e.source == "learned" and "ESP-ROM:esp32s3" in e.detail for e in esp.evidence)
    assert not any(f.startswith("log:") for f in esp.facts)  # old lines are labelled as earlier

def test_crash_without_guru_meditation_is_an_alert_that_survives_a_quiet_answer(tmp_path):
    """Real bench 2026-10-31, ESP32-S3 test firmware: `panic` printed abort() + backtrace, then the
    ROM banner with RTC_SW_CPU_RST. Then `hang 3` answered with nothing alarming."""
    data = json.loads(FIXTURE.read_text())
    data["replies"] = {"/dev/cu.usbmodem1101": {"panic\n": (REAL / "esp32s3-testfw-panic.txt").read_text(),
                                                 "hang 3\n": (REAL / "esp32s3-testfw-hang.txt").read_text()}}
    f = tmp_path / "bench.json"
    f.write_text(json.dumps(data))
    br = Broker(FixtureIo(f), tmp_path / "s")
    br.scan()
    br.select([br.bench.ports["serial:usbmodem1101"].device_id])
    for cmd in ("panic", "hang 3"):
        aid = br.request("serial.send", "serial:usbmodem1101", {"data": cmd})["approval_id"]
        br.decide(aid, True)
        br.request("serial.send", "serial:usbmodem1101", {"data": cmd}, approval_id=aid)
    br.scan()
    esp = esp_target(br.bench)
    assert any(f.startswith("ALERT: crash: abort() at PC 0x42001c85") for f in esp.facts)


def test_every_reason_has_a_code_the_panel_can_translate(tmp_path):
    """The panel says reasons in the person's language by code; the English reason stays for the agent."""
    from benchscout.policy import classify

    b = recon.run(FixtureIo(FIXTURE)).bench
    assert classify(b, "serial.listen", "serial:usbserial-10").code == "reset_on_open"
    assert classify(b, "openFPGALoader.detect", next(p for p in b.ports if p.startswith("jtag:"))).code == "action:openFPGALoader.detect"
    assert classify(b, "no.such.action", "serial:usbserial-10").code == "unknown_action"
    scpi = "visa:TCPIP0::192.168.1.50::INSTR"  # the PSU on the LAN
    assert classify(b, "scpi.query", scpi, {"command": "OUTP CH1,ON"}).code == "energizing"
    assert classify(b, "scpi.query", scpi, {"command": "DISP:BRIG 5"}).code == "scpi_command"
    assert classify(b, "scpi.query", scpi, {"command": "MEAS:VOLT?"}).code == "scpi_query"
    assert classify(b, "scpi.query", scpi, {"command": "*IDN?"}).code == "scpi_status"
    assert classify(b, "scpi.query", scpi, {"command": "*TST?"}).code == "scpi_procedure"


def test_every_serial_port_keeps_its_evidence_and_a_valid_open_mode():
    """Review 2026-11-02 (mypy): a positional argument had slid into open_lines, so bridge ports lost
    their evidence and carried a list where "low"/"asserted" belongs."""
    b = recon.run(FixtureIo(FIXTURE)).bench
    for p in b.ports.values():
        if p.kind == "serial":
            assert p.open_lines in ("low", "asserted"), p.id
            assert any(e.source == "serial-port" for e in p.evidence), p.id


def test_an_instrument_found_through_visa_is_bench_hardware_even_without_data_for_its_usb_id(r):
    """Found by the e2e (2026-11-02): the Rigol scope's USB entry was set apart as 'other' before its USBTMC port
    came from the VISA list, and it vanished from the first question."""
    scope = next(d for d in r.bench.devices.values() if d.vid == 0x1AB1)
    assert not scope.other and any(p.kind == "usbtmc" for p in r.bench.ports_of(scope.id))
