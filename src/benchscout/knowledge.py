"""Knowledge tables: shortcuts for parts we already know.

Everything here is a lookup, not a guess. An entry only claims what its source
can prove (a USB descriptor tells you the bridge chip, not what is behind it).
"""

from __future__ import annotations

import re

# (vid, pid) -> what the USB descriptor alone proves.
#   role: "bridge"  USB-UART converter, target behind it is unknown
#         "native"  the chip itself enumerates (target family is known)
#         "multi"   multi-channel FTDI, meaning of channels depends on the board
#         "probe"   debug probe (JTAG/SWD) + often a virtual COM port
#         "instrument" a USBTMC/VISA instrument
USB_IDS: dict[tuple[int, int], dict] = {
    (0x10C4, 0xEA60): {"name": "Silicon Labs CP210x", "role": "bridge", "may_reset_on_open": True},
    (0x1A86, 0x7523): {"name": "WCH CH340", "role": "bridge", "may_reset_on_open": True},
    (0x1A86, 0x55D4): {"name": "WCH CH9102", "role": "bridge", "may_reset_on_open": True},
    (0x0403, 0x6001): {"name": "FTDI FT232R", "role": "bridge", "may_reset_on_open": True},
    (0x0403, 0x6015): {"name": "FTDI FT231X", "role": "bridge", "may_reset_on_open": True},
    (0x0403, 0x6010): {"name": "FTDI FT2232H", "role": "multi"},
    (0x0403, 0x6014): {"name": "FTDI FT232H", "role": "probe", "probe": "jtag"},
    (0x303A, 0x1001): {
        "name": "Espressif USB-Serial/JTAG",
        "role": "native",
        "family": "ESP32 with native USB (S3/C3/C6/H2)",
        "jtag": True,
        # Observed on a real bench (macOS): opening the CDC port reset the chip and the
        # USB device re-enumerated mid-read. Treat opening as disruptive.
        "may_reset_on_open": True,
        # Real bench 2026-10-31: dropping DTR/RTS after open reset it on every (re)open.
        "open_lines": "asserted",
    },
    (0x0483, 0x374B): {"name": "ST-LINK/V2-1", "role": "probe", "probe": "swd"},
    (0x0483, 0x374E): {"name": "STLINK-V3", "role": "probe", "probe": "swd"},
    (0x0483, 0x3753): {"name": "STLINK-V3", "role": "probe", "probe": "swd"},
    (0x2E8A, 0x000C): {"name": "Raspberry Pi Debug Probe (CMSIS-DAP)", "role": "probe", "probe": "swd"},
    (0x1AB1, 0x04CE): {"name": "Rigol DS1000Z", "role": "instrument"},
}

# Boards whose FT2232H channel wiring is fixed by design.
MULTI_CHANNEL_BOARDS = [
    {
        "match": lambda d: "digilent" in (d.get("vendor") or "").lower()
        or (d.get("serial") or "").startswith(("2102", "2103", "2104")),
        "board": "Digilent FPGA board",
        "channels": {0: "jtag-fpga", 1: "uart-fpga-fabric"},
        "source": "Digilent boards wire FT2232H channel A to FPGA JTAG, channel B to FPGA UART pins",
    }
]

MULTI_CHANNEL_BOARDS.append(
    {
        # Sipeed Tang Nano boards: the BL702 debugger emulates an FT2232 (0403:6010).
        "match": lambda d: "sipeed" in ((d.get("vendor") or "") + (d.get("name") or "")).lower(),
        "board": "Sipeed Tang Nano (Gowin FPGA)",
        "debugger": "Sipeed BL702 debugger (FT2232H-compatible)",
        "channels": {0: "jtag-fpga", 1: "uart-fpga-fabric"},
        "source": "Sipeed Tang Nano debugger: channel A = Gowin JTAG, channel B = UART to FPGA pins",
    }
)

# Serial numbers burned identically into every unit of a model: not unique per board.
SHARED_SERIALS = {"FactoryAIOT Pro"}

# JTAG IDCODE (version nibble masked) -> part
IDCODES: dict[int, dict] = {
    0x0362D093: {"name": "Xilinx XC7A35T", "kind": "fpga"},
    0x0362C093: {"name": "Xilinx XC7A50T", "kind": "fpga"},
    0x03631093: {"name": "Xilinx XC7A100T", "kind": "fpga"},
    0x03727093: {"name": "Xilinx XC7Z020", "kind": "fpga"},
    0x0BA00477: {"name": "ARM Cortex-M debug port (DAP)", "kind": "mcu"},
    # Confirmed on a real Tang Nano 9K: openFPGALoader reports 0x0100481B as GW1N(R)-9C.
    0x0100481B: {"name": "Gowin GW1N(R)-9C", "kind": "fpga"},
}

# Boot/console log signatures. Each returns facts; only "exact" ones identify a part.
LOG_SIGNATURES: list[tuple[re.Pattern, callable]] = [
    (re.compile(r"ESP-ROM:(esp32[a-z0-9]*)"), lambda m: {"exact": m.group(1).upper().replace("ESP32", "ESP32-").rstrip("-")}),
    (re.compile(r"ESP-IDF (v[\d.]+)"), lambda m: {"fact": f"ESP-IDF {m.group(1)}"}),
    (re.compile(r"Brownout detector was triggered|rst: ?brownout|BROWNOUT_RST", re.I), lambda m: {"fact": "brownout reset seen in log", "alert": True}),
    (re.compile(r"rst:0x[0-9a-f]+ \(([A-Z_]+)\)"), lambda m: {"fact": f"ESP reset reason {m.group(1)}", "family": "ESP32 family"}),
    (re.compile(r"\*\*\* Booting Zephyr OS build ([\w.\-+]+)"), lambda m: {"fact": f"Zephyr {m.group(1)}"}),
    (re.compile(r"U-Boot ([\d.]+)"), lambda m: {"fact": f"U-Boot {m.group(1)}"}),
    (re.compile(r"Guru Meditation Error: (.+)"), lambda m: {"fact": f"crash: {m.group(1).strip()}", "alert": True}),
    # Real bench 2026-10-31 (ESP32-S3, `panic` in the test firmware): abort() prints no Guru
    # Meditation line, and the ROM then reports RTC_SW_CPU_RST, so the ROM line alone hides the crash.
    (re.compile(r"abort\(\) was called at PC (0x[0-9a-fA-F]+)"), lambda m: {"fact": f"crash: abort() at PC {m.group(1)}", "alert": True}),
    (re.compile(r"^Backtrace: ", re.M), lambda m: {"fact": "crash backtrace printed", "alert": True}),
]

INSTRUMENT_KINDS = [
    (re.compile(r"^(DS|MSO|DSO|SDS|TBS|MDO|DHO)\d", re.I), "oscilloscope"),
    (re.compile(r"^(DP\d|SPD|E36|PWS|KA3)", re.I), "power supply"),
    (re.compile(r"^(DM3|DM8|SDM|344|DMM)", re.I), "multimeter"),
    (re.compile(r"^(DG\d|SDG|33[56]\d)", re.I), "function generator"),
]


def classify_log(text: str) -> dict:
    """Facts from passively observed console output. Never invents a part name."""
    out: dict = {"exact": None, "family": None, "facts": [], "alerts": [], "sample": []}
    for pat, fn in LOG_SIGNATURES:
        for m in pat.finditer(text):
            r = fn(m)
            if r.get("exact"):
                out["exact"] = r["exact"]
            if r.get("family"):
                out["family"] = r["family"]
            if r.get("fact"):
                (out["alerts"] if r.get("alert") else out["facts"]).append(r["fact"])
    lines = [l for l in text.splitlines() if l.strip()]
    out["sample"] = lines[:3]
    out["facts"] = sorted(set(out["facts"]))
    out["alerts"] = sorted(set(out["alerts"]))
    return out


def parse_idn(idn: str) -> dict:
    parts = [p.strip() for p in idn.split(",")]
    maker, model = (parts + ["", ""])[:2]
    kind = next((k for pat, k in INSTRUMENT_KINDS if pat.match(model)), "instrument")
    return {"maker": maker, "model": model, "kind": kind}
