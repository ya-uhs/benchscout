"""Knowledge comes from data files in layers, never from code (design.md section 5)."""

import json
import re
from pathlib import Path

import pytest

from benchscout import knowledge as K
from benchscout import recon
from benchscout.broker import Broker
from benchscout.io import FixtureIo

FIXTURE = Path(__file__).parent.parent / "fixtures" / "bench_example.json"
SRC = Path(K.__file__)


@pytest.fixture(autouse=True)
def clean_user_layer(tmp_path, monkeypatch):
    monkeypatch.setenv("BENCHSCOUT_KNOWLEDGE", str(tmp_path / "user-knowledge"))
    yield
    monkeypatch.delenv("BENCHSCOUT_KNOWLEDGE")
    K.load()


def test_no_device_knowledge_in_code():
    code = SRC.read_text()
    for needle in ("ESP32", "CH340", "Gowin", "Xilinx", "Sipeed", "Digilent", "0x0403", "0x303A", "Guru"):
        assert needle not in code, f"{needle} belongs in packs/bench/data, not knowledge.py"


def test_a_user_adds_a_bridge_and_a_log_alert_without_code(tmp_path):
    user = tmp_path / "user-knowledge"
    user.mkdir()
    (user / "mine.toml").write_text('''
[[usb]]
vid = 0x067B
pid = 0x2303
name = "Prolific PL2303"
role = "bridge"
may_reset_on_open = true

[[log]]
pattern = 'WDT timeout on (?P<what>\\w+)'
alert = "watchdog: {what}"
''')
    K.load()
    assert K.USB_IDS[(0x067B, 0x2303)]["name"] == "Prolific PL2303"
    assert K.classify_log("E: WDT timeout on main\n")["alerts"] == ["watchdog: main"]
    assert any(p.name == "mine.toml" for p in K.SOURCES)


def test_workspace_overrides_shipped_entry_by_entry(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".benchscout" / "knowledge").mkdir(parents=True)
    # this lab's CH340 boards have no auto-reset circuit
    (ws / ".benchscout" / "knowledge" / "lab.toml").write_text("[[usb]]\nvid = 0x1A86\npid = 0x7523\nmay_reset_on_open = false\n")
    br = Broker(FixtureIo(FIXTURE), ws / ".benchscout")
    br.scan()
    assert K.USB_IDS[(0x1A86, 0x7523)]["name"] == "WCH CH340"  # untouched fields stay
    ch340 = br.bench.ports["serial:usbserial-10"]
    assert ch340.may_reset_on_open is False


def test_a_user_board_is_tried_before_shipped_ones(tmp_path):
    user = tmp_path / "user-knowledge"
    user.mkdir()
    (user / "board.toml").write_text('''
[[board]]
board = "My FT2232H dev board"
source = "our schematic rev B: channel A = UART to the FPGA, channel B = JTAG"
match = { serial_prefix = ["2103"] }
channels = { 0 = "uart-fpga-fabric", 1 = "jtag-fpga" }
''')
    K.load()
    b = recon.build(FixtureIo(FIXTURE))
    fpga = next(t for t in b.targets.values() if t.kind == "fpga")
    assert "My FT2232H dev board" in fpga.facts[0]
