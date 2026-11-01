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
from typing import Callable

SHIPPED = Path(__file__).parent / "packs" / "bench" / "data"

# Filled by load(); module-level so recon can use K.USB_IDS and friends.
USB_IDS: dict[tuple[int, int], dict] = {}
MULTI_CHANNEL_BOARDS: list[dict] = []
SHARED_SERIALS: set[str] = set()
IDCODES: dict[int, dict] = {}
LOG_SIGNATURES: list[tuple[re.Pattern, dict]] = []
INSTRUMENT_KINDS: list[tuple[re.Pattern, str]] = []
TOOLS: dict[str, dict] = {}  # external-tool actions defined as data (design.md section 4)
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
    idcodes: dict[int, dict] = {}
    logs: list[tuple[re.Pattern, dict]] = []
    instruments: list[tuple[re.Pattern, str]] = []
    tools: dict[str, dict] = {}
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
            for e in data.get("idcode", []):
                idcodes[int(e["code"]) & 0x0FFFFFFF] = {"name": e["name"], "kind": e.get("kind", "fpga")}
            for e in data.get("log", []):
                flags = (re.I if e.get("ignore_case") else 0) | (re.M if e.get("multiline") else 0)
                logs.append((re.compile(e["pattern"], flags), e))
            for e in data.get("tool", []):
                tools[e["name"]] = e  # a later layer replaces a tool of the same name
            for e in reversed(data.get("instrument", [])):
                instruments.insert(0, (re.compile(e["model"], re.I), e["kind"]))
    global USB_IDS, MULTI_CHANNEL_BOARDS, SHARED_SERIALS, IDCODES, LOG_SIGNATURES, INSTRUMENT_KINDS, SOURCES, TOOLS
    USB_IDS, MULTI_CHANNEL_BOARDS, SHARED_SERIALS = usb, boards, shared
    IDCODES, LOG_SIGNATURES, INSTRUMENT_KINDS, SOURCES, TOOLS = idcodes, logs, instruments, sources, tools


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


def _exact_name(e: dict, m: re.Match) -> str:
    name = _fill(e["exact"], m)
    if e.get("upper"):
        name = name.upper()
    for a, b in e.get("replace", []):
        name = name.replace(a, b)
    return name.strip(e.get("strip", "")) if e.get("strip") else name


def classify_log(text: str) -> dict:
    """Facts from passively observed console output. Never invents a part name: only an
    `exact` entry (a banner that proves the part) names one."""
    out: dict = {"exact": None, "family": None, "facts": [], "alerts": [], "sample": []}
    for pat, e in LOG_SIGNATURES:
        for m in pat.finditer(text):
            if e.get("exact"):
                out["exact"] = _exact_name(e, m)
            if e.get("family"):
                out["family"] = _fill(e["family"], m)
            if e.get("fact"):
                out["facts"].append(_fill(e["fact"], m))
            if e.get("alert"):
                out["alerts"].append(_fill(e["alert"], m))
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


load()
