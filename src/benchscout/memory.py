"""Memory of the bench (design.md section 6).

Written automatically, without asking: what benchscout observed and what the agent learned.
The human can read and edit the files, and delete entries in the panel. Only entries that loosen
safety (kind "safety") wait for the human to confirm before they take effect.

Where:
  per device   <user dir>/memory/devices/<vid>-<pid>-<serial>-<tag>.toml   (devices move between folders; device_key)
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

import hashlib
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
    device: str = ""  # in the folder's memory only: whose entry it is (the device file's key)


def device_key(vid: int | None, pid: int | None, serial: str | None) -> str:
    """A unit's memory file name: readable (the serial, with what a file name cannot hold replaced) and one per serial
    string: a tag from the serial as given keeps "A:B" apart from "A_B", and "ab" from "AB" where file names ignore
    case (macOS, Windows), so one unit's memory never lands in another's file."""
    readable = re.sub(r"[^A-Za-z0-9._-]", "_", serial or "noserial")
    tag = hashlib.sha256((serial or "").encode("utf-8")).hexdigest()[:12]
    return f"{vid or 0:04x}-{pid or 0:04x}-{readable}-{tag}"


def _key_of(header: dict) -> str | None:
    """The key of the unit a memory file's header names (None: not a unit's header)."""
    try:
        return device_key(int(header["vid"], 16), int(header["pid"], 16), header["serial"] or None)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


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
        values = asdict(e)
        # TOML has no null. Empty tables represent unrestricted argument slots;
        # decode them back to None before policy compares old and new limits.
        if e.kind == "command" and isinstance(e.data.get("limits"), list):
            values["data"]["limits"] = [{} if x is None else x for x in e.data["limits"]]
        for k, v in values.items():
            if v in ("", {}, None) and k not in ("text", "kind"):
                continue
            out.append(f"{k} = {_toml_value(v)}")
    return "\n".join(out) + "\n"


# memory files that could not be read (a hand edit gone wrong): left as they are, never written over, and said
PROBLEMS: dict[str, str] = {}


class MemoryFile:
    def __init__(self, path: Path, header: dict | None = None):
        self.path = Path(path)
        self.header = header or {}
        self.entries: list[Entry] = []
        self.broken: str | None = None
        if self.path.exists():
            try:
                data = tomllib.loads(self.path.read_text(encoding="utf-8"))
                entries = [Entry(**{k: v for k, v in e.items() if k in Entry.__dataclass_fields__}) for e in data.get("entry", [])]
            except Exception as e:  # not TOML, not UTF-8, an entry without its fields
                self.broken = f"{type(e).__name__}: {e}"
                PROBLEMS[str(self.path)] = self.broken
                return
            PROBLEMS.pop(str(self.path), None)
            self.header = {k: v for k, v in data.items() if k != "entry"} or self.header
            for entry in entries:
                if entry.kind == "command" and isinstance(entry.data.get("limits"), list):
                    # "None" was written by older versions; keep those files usable.
                    entry.data["limits"] = [None if x == {} or x == "None" else x for x in entry.data["limits"]]
            self.entries = entries

    def save(self) -> None:
        if self.broken:  # the person's file as they left it: fixing it is theirs, losing it is not an option
            raise ValueError(f"memory file {self.path} could not be read ({self.broken}); fix or delete it first")
        from .files import write_whole

        write_whole(self.path, dump(self.entries, self.header))


class _FileLock:
    """Between processes: one core per VS Code window, and device memory is shared by all of them (user-wide)."""

    def __init__(self, path: Path):
        self.path = path.with_name(f".{path.name}.lock")

    def __enter__(self):
        from .locks import _try_lock

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, "a+")
        end = time.monotonic() + 10
        while not _try_lock(self.f):
            if time.monotonic() > end:
                self.f.close()
                raise TimeoutError(f"memory is busy; not writing without its lock: {self.path}")
            time.sleep(0.01)
        return self

    def __exit__(self, *exc):
        from .locks import _unlock

        try:
            _unlock(self.f)
        except OSError:
            pass
        self.f.close()


class Memory:
    def __init__(self, workspace: Path | None, root: Path | None = None):
        """`workspace`: the folder whose setup is remembered with it; None in a folder VS Code does not trust (its files
        are not read, as its knowledge is not: design.md section 3, S2)."""
        self.root = Path(root or user_dir())
        self.workspace_file = Path(workspace) / ".benchscout" / "memory" / "workspace.toml" if workspace else None
        self._lock = threading.Lock()

    def _files(self, key: str) -> list[Path]:
        return [self._path(key)] + ([self.workspace_file] if self.workspace_file else [])

    def _path(self, key: str) -> Path:
        path = self.root / "devices" / f"{key}.toml"
        if path.exists():
            return path
        # a file named before 2026-11-16 (no tag) stays where it is, and is this unit's only if its header names this
        # very serial: that name was shared by "A:B" and "A_B" (and by "ab" and "AB" where names ignore case)
        old = path.with_name(key.rsplit("-", 1)[0] + ".toml")
        return old if old.exists() and _key_of(MemoryFile(old).header) == key else path

    def device_file(self, key: str, header: dict | None = None) -> MemoryFile:
        return MemoryFile(self._path(key), header)

    def entries(self, key: str) -> list[Entry]:
        """What is remembered about this device: its own file, then what this folder remembers about it (wiring, probe
        nets: true of this setup, not of the device wherever it goes; design.md section 6)."""
        with self._lock:
            mine = self.device_file(key).entries
            # the folder's file holds this setup (kind config) and nothing else: whatever else is written there is not
            # memory benchscout or the human put there, and loosens nothing
            aliases = {key, self._path(key).stem}
            here = [e for e in MemoryFile(self.workspace_file).entries if e.device in aliases and e.kind == "config"] \
                if self.workspace_file else []
            return mine + here

    def in_folder(self, entry: Entry) -> bool:
        return bool(entry.device)

    def add(self, key: str, entry: Entry, header: dict | None = None) -> Entry:
        """Append; an identity or safety entry for the same target replaces the older one (kept as superseded). A config
        entry (this setup's wiring) goes to the folder's memory, not the device's."""
        if entry.kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        if entry.kind == "config":
            if self.workspace_file is None:
                raise ValueError("this folder is not trusted in VS Code, so benchscout keeps no memory of its setup here")
            entry.device = key
            with self._lock, _FileLock(self.workspace_file):
                f = MemoryFile(self.workspace_file, {"memory": "this folder's setup (wiring, probe nets)"})
                for old in f.entries:
                    if old.device == key and old.kind == "config" and old.text == entry.text and old.target == entry.target \
                            and old.data == entry.data and old.status == entry.status:
                        return old
                f.entries.append(entry)
                f.save()
                return entry
        with self._lock, _FileLock(self._path(key)):
            f = self.device_file(key, header)
            if header:
                f.header.update(header)
            for old in f.entries:
                if old.kind == entry.kind == "identity" and old.target == entry.target and old.status == "active":
                    old.status = "superseded"
                if old.kind == entry.kind and old.kind not in ("identity", "observation") and old.text == entry.text \
                        and old.status == entry.status and old.target == entry.target and old.data == entry.data:
                    return old  # the same thing again: keep one (observations are counted, so they repeat)
            f.entries.append(entry)
            f.save()
            return entry

    def set_status(self, key: str, entry_id: str, status: str) -> Entry:
        for path in self._files(key):
            with self._lock, _FileLock(path):
                f = MemoryFile(path)
                for e in f.entries:
                    if e.id == entry_id and (path != self.workspace_file or e.device in {key, self._path(key).stem}):
                        e.status = status
                        f.save()
                        return e
        raise KeyError(f"no memory entry {entry_id!r} for {key}")

    def delete(self, key: str, entry_id: str) -> None:
        for path in self._files(key):
            with self._lock, _FileLock(path):
                f = MemoryFile(path)
                if any(e.id == entry_id for e in f.entries):
                    f.entries = [e for e in f.entries if e.id != entry_id]
                    f.save()
                    return

    def all_pending(self) -> list[tuple[str, Entry]]:
        out = []
        for p in sorted((self.root / "devices").glob("*.toml")) if (self.root / "devices").is_dir() else []:
            for e in MemoryFile(p).entries:
                if e.status == "pending":
                    out.append((p.stem, e))
        return out
