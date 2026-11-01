"""Knowledge comes from data files in layers, never from code (design.md section 5)."""

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
    monkeypatch.delenv("BENCHSCOUT_KNOWLEDGE", raising=False)
    K.load()


# Names of devices, vendors and USB ids that must never decide anything in code (design.md section 5).
DEVICE_WORDS = ("ESP32", "Espressif", "ESP-ROM", "CH340", "QinHeng", "Gowin", "Xilinx", "Lattice", "Sipeed",
                "Tang Nano", "Digilent", "STLINK", "ST-LINK", "Nucleo", "Rigol", "Siglent", "Keysight", "Guru",
                "FT2232", "0x0403", "0x303A", "0x303a", "0x1A86", "0x1a86", "0x0483", "0x2E8A")


def _python_code(path: Path) -> str:
    """Code and string literals, without comments and docstrings (real-bench notes may name devices)."""
    import io
    import tokenize

    out, prev = [], tokenize.NEWLINE
    for tok in tokenize.generate_tokens(io.StringIO(path.read_text(encoding="utf-8")).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and prev in (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.NL):
            prev = tok.type
            continue  # a docstring
        if tok.type not in (tokenize.NL, tokenize.COMMENT):
            prev = tok.type
        out.append(tok.string)
    return " ".join(out)


def _ts_code(path: Path) -> str:

    text = re.sub(r"/\*.*?\*/", " ", path.read_text(encoding="utf-8"), flags=re.S)
    return "\n".join(re.sub(r"(^|\s)//.*$", " ", line) for line in text.splitlines())


def test_no_device_knowledge_in_code():
    """No device, vendor or USB id decides anything in the product code: core, relay, extension."""
    root = Path(__file__).parent.parent
    sources = [(p, _python_code(p)) for p in sorted((root / "src" / "benchscout").rglob("*.py"))]
    sources += [(p, _ts_code(p)) for p in sorted((root / "vscode" / "src").rglob("*.ts"))]
    found = [f"{p.relative_to(root)}: {w}" for p, code in sources for w in DEVICE_WORDS if w in code]
    assert not found, "device knowledge belongs in packs/bench/data or memory, not code: " + "; ".join(found)


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
    assert K.USB_IDS[(0x1A86, 0x7523)]["role"] == "bridge"  # untouched fields stay
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


def test_without_any_shipped_data_benchscout_only_gets_stricter(tmp_path, monkeypatch):
    """design.md section 3: deleting every shipped data file must only make benchscout stricter."""
    empty = tmp_path / "empty"
    empty.mkdir()
    shipped = K.SHIPPED
    K.SHIPPED = empty
    try:
        K.load()
        io = FixtureIo(FIXTURE)
        r = recon.run(io)
        serial = [p for p in r.bench.ports.values() if p.kind == "serial"]
        assert serial and all(p.may_reset_on_open for p in serial)
        assert not any(c[0] == "serial_listen" for c in io.calls), "nothing it knows nothing about is opened"
    finally:
        K.SHIPPED = shipped
        K.load()


# What crashing firmware prints on different platforms. A device benchscout has never seen should still
# wake the agent when it goes down, whatever its SDK (design.md section 17, "noticing").
CRASHES = {
    "Cortex-M (STM32Cube)": "HardFault_Handler: PC=0x08001234",
    "nRF / CMSIS": "Fault: Usage Fault, CFSR=0x00010000",
    "Zephyr": ">>> ZEPHYR FATAL ERROR 0: CPU exception on CPU 0",
    "Mbed OS": "++ MbedOS Fault Handler ++",
    "Pico SDK": "*** PANIC ***",
    "Rust": "panicked at src/main.rs:42:5:",
    "Go": "panic: runtime error: index out of range [3] with length 3",
    "Linux": "Kernel panic - not syncing: Attempted to kill init!",
    "MicroPython": "Traceback (most recent call last):",
    "newlib assert": 'assertion "len > 0" failed: file "src/app.c", line 12, function: send',
    "glibc assert": "app: main.c:7: main: Assertion `x' failed.",
    "C++": "terminate called after throwing an instance of 'std::runtime_error'",
    "FreeRTOS hook": "STACK OVERFLOW in task sensor",
    "ESP-IDF watchdog": "E (10305) task_wdt: Task watchdog got triggered.",
    "watchdog reset": "Reset cause: WDT reset",
    "brownout": "BOR: brown-out reset detected",
    "ESP-IDF": "Guru Meditation Error: Core  1 panic'ed (LoadProhibited). Exception was unhandled.",
}
CALM = ["hb 0000002a", "hb n=12 uptime_ms=12306", "[I] sensor ok", "watchdog enabled (timeout 5 s)",
        "assert checks on", "Booting Zephyr OS build v3.6.0", "boot: 0x8 (SPI_FAST_FLASH_BOOT)", "ok panic"]


def test_crashes_on_many_platforms_raise_an_alert_and_ordinary_lines_do_not():
    K.load()
    missed = [p for p, line in CRASHES.items() if not K.classify_log(line)["alerts"]]
    assert not missed, f"no alert for: {missed}"
    noisy = {line: K.classify_log(line)["alerts"] for line in CALM if K.classify_log(line)["alerts"]}
    assert not noisy, f"false alarms: {noisy}"
