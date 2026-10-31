"""Per-device locks shared by every core of this OS user (design.md section 15 S1).

One core runs per VS Code window, so two windows could otherwise both use the same
board. Selecting a device acquires its lock, like labgrid's `acquire` on a place.
`flock` locks die with the process, so a crashed core never leaves a device stuck.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import IO

from .runtime import runtime_dir

if sys.platform == "win32":  # pragma: no cover - Windows support is design.md R6
    fcntl = None
else:
    import fcntl


def _name(device_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", device_id) + ".lock"


class DeviceLocks:
    def __init__(self, owner: dict, root: Path | None = None):
        if fcntl is None:  # pragma: no cover
            raise NotImplementedError("device locks are not implemented on Windows yet (design.md R6)")
        self.owner = owner  # written into the lock so other windows can say who holds it
        self.dir = (root or runtime_dir()) / "locks"
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._held: dict[str, IO] = {}

    def acquire(self, device_id: str) -> dict | None:
        """Take the device. Returns None on success, or the holder's info if someone else has it."""
        if device_id in self._held:
            return None
        f = open(self.dir / _name(device_id), "a+")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.seek(0)
            holder = _parse(f.read())
            f.close()
            return holder
        f.seek(0)
        f.truncate()
        f.write(json.dumps(self.owner))
        f.flush()
        self._held[device_id] = f
        return None

    def release(self, device_id: str) -> None:
        f = self._held.pop(device_id, None)
        if f:
            f.seek(0)
            f.truncate()
            fcntl.flock(f, fcntl.LOCK_UN)
            f.close()

    def release_all(self) -> None:
        for d in list(self._held):
            self.release(d)

    def holds(self, device_id: str) -> bool:
        return device_id in self._held

    def holder(self, device_id: str) -> dict | None:
        """Who else holds this device, without taking it. None if free or held by us."""
        if device_id in self._held:
            return None
        p = self.dir / _name(device_id)
        if not p.exists():
            return None
        with open(p, "a+") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                f.seek(0)
                return _parse(f.read())
            fcntl.flock(f, fcntl.LOCK_UN)
            return None


def _parse(text: str) -> dict:
    try:
        return json.loads(text) if text.strip() else {"pid": None}
    except json.JSONDecodeError:
        return {"pid": None}


def owner_info(workspace: Path) -> dict:
    return {"pid": os.getpid(), "workspace": str(Path(workspace).resolve())}
