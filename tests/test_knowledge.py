"""Knowledge comes from data files in layers, never from code (design.md section 5)."""

import re
from pathlib import Path

import pytest

from benchscout import knowledge as K
from benchscout import recon
from benchscout.broker import Broker
from benchscout.io import FixtureIo
from benchscout.policy import Effect, classify

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


def test_a_broken_knowledge_file_is_left_out_whole_and_said_not_a_stopped_core(tmp_path):
    """Windows VM, 2026-11-03: a folder copied from a Mac carried ._tools.toml (macOS's resource-fork file beside
    each copied file); the core read it as knowledge, could not decode it, and did not start at all."""
    from benchscout.report import render

    kdir = tmp_path / "ws" / ".benchscout" / "knowledge"
    kdir.mkdir(parents=True)
    (kdir / "._lab.toml").write_bytes(b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        \xa3\x00")
    (kdir / "lab.toml").write_text("[[usb]]\nvid = 0x1A86\npid = 0x7523\nmay_reset_on_open = false\n")
    (kdir / "typo.toml").write_text("[[usb]\nvid = 0x0483\n")  # a hand edit with a missing bracket
    # one good entry and one that cannot be an action: the file is not half applied
    (kdir / "half.toml").write_text('[[usb]]\nvid = 0x10C4\npid = 0xEA60\nmay_reset_on_open = false\n\n'
                                    '[[tool]]\nname = "x.do"\neffect = "safe"\n')
    br = Broker(FixtureIo(FIXTURE), tmp_path / "ws" / ".benchscout")
    r = br.scan()
    assert br.bench.ports["serial:usbserial-10"].may_reset_on_open is False, "the readable file is in effect"
    assert {Path(p.split(": ")[0]).name for p in K.PROBLEMS} == {"typo.toml", "half.toml"}, "hidden files are not knowledge"
    assert (0x10C4, 0xEA60) not in K.USB_IDS or K.USB_IDS[(0x10C4, 0xEA60)].get("may_reset_on_open") is not False
    assert "could not be read" in render(r) and "typo.toml" in render(r), "the agent and the report say so"


def test_a_user_board_is_tried_before_shipped_ones(tmp_path):
    user = tmp_path / "user-knowledge"
    user.mkdir()
    (user / "board.toml").write_text('''
[[board]]
board = "My FT2232H dev board"
source = "our schematic rev B: channel A = UART to the FPGA, channel B = JTAG"
match = { serial_prefix = ["2103"] }
channels = { 0 = "uart", 1 = "jtag" }
inside = { 0 = 1 }
''')
    K.load()
    b = recon.build(FixtureIo(FIXTURE))
    fpga = next(t for t in b.targets.values() if any(p.kind == "jtag" for p in b.ports_reaching(t.id)))
    assert any("our schematic rev B" in e.detail for e in fpga.evidence), "the user's wiring, before the shipped one"


@pytest.mark.parametrize("bench", ["bench_example.json", "bench_unseen.json"])
def test_without_any_shipped_data_benchscout_only_gets_stricter(tmp_path, monkeypatch, bench):
    """design.md section 3: deleting every shipped data file must only make benchscout stricter, on the example bench
    and on one of devices benchscout ships nothing about."""
    empty = tmp_path / "empty"
    empty.mkdir()
    shipped = K.SHIPPED
    K.SHIPPED = empty
    try:
        K.load()
        io = FixtureIo(FIXTURE.parent / bench)
        r = recon.run(io)
        serial = [p for p in r.bench.ports.values() if p.kind == "serial"]
        assert serial and all(p.may_reset_on_open for p in serial)
        assert all(classify(r.bench, "serial.listen", p.id).effect >= Effect.DISRUPTIVE for p in serial)
        assert not any(c[0] in ("serial_listen", "probe") for c in io.calls), "nothing it knows nothing about is opened"
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


def test_a_knowledge_file_cannot_name_a_part(tmp_path):
    """Review 2026-11-02: tables from before the 2026-10-31 revision were still read (a log pattern's `exact` or
    `family`, an `[[idcode]]` or `[[instrument]]` table). Nothing shipped used them, but an agent that can write a
    knowledge file could have named a part without its words being checked (bench_identify). Now they are ignored:
    names come from the device, a tool's own database, a checked quote, or the person."""
    ws = tmp_path / "ws"
    k = ws / ".benchscout" / "knowledge"
    k.mkdir(parents=True)
    (k / "names.toml").write_text('''
[[log]]
pattern = "sensor ok"
exact = "STM32F407"
family = "STM32"
fact = "it is an STM32"

[[idcode]]
code = 0x0100481B
name = "Some FPGA"

[[instrument]]
model = "SPD.*"
kind = "power supply"
''', encoding="utf-8")
    try:
        K.load(ws)
        c = K.classify_log("[I] sensor ok\n")
        assert c == {"alerts": [], "sample": ["[I] sensor ok"]}
        r = recon.run(FixtureIo(FIXTURE))
        assert not any(t.name == "STM32F407" or "STM32" in " ".join(t.facts) for t in r.bench.targets.values())
        assert not any("power supply" in " ".join(t.facts) for t in r.bench.targets.values())
    finally:
        K.load()
