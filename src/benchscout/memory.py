"""Memory of the bench (design.md section 6).

Written automatically, without asking: what benchscout observed and what the agent learned.
The human can read and edit the files, and delete entries in the panel. Only entries that loosen
safety (kind "safety") wait for the human to confirm before they take effect.

Where:
  per device   <user dir>/memory/devices/<vid>-<pid>-<serial>.toml   (devices move between folders)
  per folder   <workspace>/.benchscout/memory/workspace.toml          (this setup: wiring, probe nets)

Each file is a list of [[entry]] tables:
  kind      identity | observation | config | interpretation | command | safety
  text      what is remembered, in words
  evidence  a quote of the device's own output, when there is one
  source    benchscout | agent | user
  target    target id (for identity), port id (for safety/observation), optional
  time      ISO time it was written
  status    active | pending (safety, waiting for the human) | rejected | superseded (a newer entry replaced it) |
            contradicted (what the device did since says otherwise; kept as history)
  data      structured part (e.g. {listen_safe = true}, {pattern = "...", alert = "..."})
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import tomllib
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

KINDS = ("identity", "observation", "config", "interpretation", "command", "safety")


def user_dir() -> Path:
    if os.environ.get("BENCHSCOUT_MEMORY"):
        return Path(os.environ["BENCHSCOUT_MEMORY"])
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "benchscout" / "memory"
    return Path.home() / ".config" / "benchscout" / "memory"


@dataclass
class Entry:
    kind: str
    text: str
    source: str
    evidence: str = ""
    target: str = ""
    time: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))
    status: str = "active"
    data: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


def device_key(vid: int | None, pid: int | None, serial: str | None) -> str:
    raw = f"{vid or 0:04x}-{pid or 0:04x}-{serial or 'noserial'}"
    return re.sub(r"[^A-Za-z0-9._-]", "_", raw)


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{json.dumps(k)} = {_toml_value(x)}" for k, x in v.items()) + " }"
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)  # a JSON string is a valid TOML basic string here


def dump(entries: list[Entry], header: dict) -> str:
    out = ["# benchscout memory. Written automatically; edit or delete entries freely.",
           "# Entries with status = \"pending\" loosen safety and take effect only after you confirm them."]
    for k, v in header.items():
        out.append(f"{k} = {_toml_value(v)}")
    for e in entries:
        out.append("\n[[entry]]")
        for k, v in asdict(e).items():
            if v in ("", {}, None) and k not in ("text", "kind"):
                continue
            out.append(f"{k} = {_toml_value(v)}")
    return "\n".join(out) + "\n"


class MemoryFile:
    def __init__(self, path: Path, header: dict | None = None):
        self.path = Path(path)
        self.header = header or {}
        self.entries: list[Entry] = []
        if self.path.exists():
            data = tomllib.loads(self.path.read_text(encoding="utf-8"))
            self.header = {k: v for k, v in data.items() if k != "entry"} or self.header
            for e in data.get("entry", []):
                self.entries.append(Entry(**{k: v for k, v in e.items() if k in Entry.__dataclass_fields__}))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(dump(self.entries, self.header), encoding="utf-8")


class Memory:
    def __init__(self, workspace: Path, root: Path | None = None):
        self.root = Path(root or user_dir())
        self.workspace_file = Path(workspace) / ".benchscout" / "memory" / "workspace.toml"
        self._lock = threading.Lock()

    def device_file(self, key: str, header: dict | None = None) -> MemoryFile:
        return MemoryFile(self.root / "devices" / f"{key}.toml", header)

    def entries(self, key: str) -> list[Entry]:
        with self._lock:
            return self.device_file(key).entries

    def add(self, key: str, entry: Entry, header: dict | None = None) -> Entry:
        """Append; an identity or safety entry for the same target replaces the older one (kept as superseded)."""
        if entry.kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        with self._lock:
            f = self.device_file(key, header)
            if header:
                f.header.update(header)
            for old in f.entries:
                if old.kind == entry.kind == "identity" and old.target == entry.target and old.status == "active":
                    old.status = "superseded"
                if old.kind == entry.kind and old.kind not in ("identity", "observation") and old.text == entry.text \
                        and old.status == entry.status:
                    return old  # the same thing again: keep one (observations are counted, so they repeat)
            f.entries.append(entry)
            f.save()
            return entry

    def set_status(self, key: str, entry_id: str, status: str) -> Entry:
        with self._lock:
            f = self.device_file(key)
            for e in f.entries:
                if e.id == entry_id:
                    e.status = status
                    f.save()
                    return e
        raise KeyError(f"no memory entry {entry_id!r} for {key}")

    def delete(self, key: str, entry_id: str) -> None:
        with self._lock:
            f = self.device_file(key)
            f.entries = [e for e in f.entries if e.id != entry_id]
            f.save()

    def all_pending(self) -> list[tuple[str, Entry]]:
        out = []
        for p in sorted((self.root / "devices").glob("*.toml")) if (self.root / "devices").is_dir() else []:
            for e in MemoryFile(p).entries:
                if e.status == "pending":
                    out.append((p.stem, e))
        return out
