"""Knowledge: loaded from data files, never written here (design.md section 5).

Only mechanism lives in this file: reading the data, layering it, and matching against it.
What benchscout knows is layer 2 of the design (safety and the first read only) and comes
from TOML files, read in this order with later ones taking precedence:

  1. shipped with benchscout   src/benchscout/packs/bench/data/*.toml
  2. this user                 ~/.config/benchscout/knowledge/*.toml  (%APPDATA% on Windows)
  3. this workspace            <workspace>/.benchscout/knowledge/*.toml

So a user adds a board, a USB bridge or a log line by dropping a file in, without code.
Everything here is a lookup, not a guess: an entry only claims what its source can prove.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path

SHIPPED = Path(__file__).parent / "packs" / "bench" / "data"

# Filled by load(); module-level so recon can use K.USB_IDS and friends.
USB_IDS: dict[tuple[int, int], dict] = {}
MULTI_CHANNEL_BOARDS: list[dict] = []
# tool -> how it opens a USB interface itself ("libusb"): on Windows that interface needs a WinUSB-class driver
USB_ACCESS: dict[str, str] = {}
SHARED_SERIALS: set[str] = set()
LOG_SIGNATURES: list[tuple[re.Pattern, dict]] = []
TOOLS: dict[str, dict] = {}  # external-tool actions defined as data (design.md section 4)
GETS: dict[str, dict] = {}  # executable -> pinned official downloads per platform (fetch.py)
SOURCES: list[Path] = []  # files actually read, for `benchscout knowledge` / debugging


def user_dir() -> Path:
    if os.environ.get("BENCHSCOUT_KNOWLEDGE"):
        return Path(os.environ["BENCHSCOUT_KNOWLEDGE"])
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "benchscout" / "knowledge"
    return Path.home() / ".config" / "benchscout" / "knowledge"


def layers(workspace: Path | None = None) -> list[Path]:
    dirs = [SHIPPED, user_dir()]
    if workspace:
        dirs.append(Path(workspace) / ".benchscout" / "knowledge")
    return dirs


def load(workspace: Path | None = None) -> None:
    """(Re)read every layer. Later layers override earlier ones entry by entry."""
    usb: dict[tuple[int, int], dict] = {}
    boards: list[dict] = []
    shared: set[str] = set()
    logs: list[tuple[re.Pattern, dict]] = []
    tools: dict[str, dict] = {}
    access: dict[str, str] = {}
    gets: dict[str, dict] = {}
    sources: list[Path] = []
    for d in layers(workspace):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.toml")):
            data = tomllib.loads(f.read_text(encoding="utf-8"))
            sources.append(f)
            shared |= set(data.get("shared_serials", []))
            for e in data.get("usb", []):
                key = (int(e["vid"]), int(e["pid"]))
                usb[key] = {**usb.get(key, {}), **{k: v for k, v in e.items() if k not in ("vid", "pid")}}
            for e in reversed(data.get("board", [])):  # later layers are tried first
                boards.insert(0, _board(e))
            for e in data.get("log", []):
                flags = (re.I if e.get("ignore_case") else 0) | (re.M if e.get("multiline") else 0)
                logs.append((re.compile(e["pattern"], flags), e))
            for e in data.get("tool", []):
                tools[e["name"]] = e  # a later layer replaces a tool of the same name
            access.update(data.get("usb_access", {}))
            for exe, g in data.get("get", {}).items():
                gets[exe] = g  # a later layer pins its own version
    global USB_IDS, MULTI_CHANNEL_BOARDS, SHARED_SERIALS, LOG_SIGNATURES, SOURCES, TOOLS, GETS, USB_ACCESS
    USB_IDS, MULTI_CHANNEL_BOARDS, SHARED_SERIALS = usb, boards, shared
    LOG_SIGNATURES, SOURCES, TOOLS, GETS = logs, sources, tools, gets
    USB_ACCESS = access


def _board(e: dict) -> dict:
    m = e.get("match", {})
    texts = [t.lower() for t in m.get("text_contains", [])]
    prefixes = tuple(m.get("serial_prefix", []))

    def match(raw: dict) -> bool:
        text = f"{raw.get('vendor') or ''} {raw.get('name') or ''}".lower()
        return any(t in text for t in texts) or bool(prefixes and (raw.get("serial") or "").startswith(prefixes))

    return {**e, "match": match, "channels": {int(k): v for k, v in e.get("channels", {}).items()}}


def _fill(template: str, m: re.Match) -> str:
    return template.format(**{k: (v or "").strip() for k, v in m.groupdict().items()})


def classify_log(text: str) -> dict:
    """What deterministic noticing takes from console output: the alerts a log entry raises (a crash marker) and the
    first lines. Nothing else: naming a part or reading a log's meaning is the agent's (design.md section 3), and a
    name needs the device's own words, checked by bench_identify. A knowledge file's `exact`, `family` or `fact`
    (from before the 2026-10-31 revision) are ignored, so a file cannot name a part without that check."""
    alerts: set[str] = set()
    for pat, e in LOG_SIGNATURES:
        for m in pat.finditer(text):
            if e.get("alert"):
                alerts.add(_fill(e["alert"], m))
    lines = [l for l in text.splitlines() if l.strip()]
    return {"alerts": sorted(alerts), "sample": lines[:3]}


def parse_idn(idn: str) -> dict:
    """IEEE 488.2 *IDN?: maker, model, serial, firmware, comma-separated. Only what the instrument says."""
    parts = [p.strip() for p in idn.split(",")]
    maker, model = (parts + ["", ""])[:2]
    return {"maker": maker, "model": model}


load()


def tool_names() -> list[str]:
    """The external programs the data names: each tool action's program and each tool a USB entry says how to reach
    a part with. No list in code: what benchscout looks for comes from the data, the user's and the agent's."""
    names = {e["argv"][0] for e in TOOLS.values() if e.get("argv")}
    names |= {t for u in USB_IDS.values() for t in (u.get("tools") or {})}
    return sorted(names)

