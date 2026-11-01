"""Per-device locks shared by every core of this OS user (design.md section 15 S1).

One core runs per VS Code window, so two windows could otherwise both use the same
board. Selecting a device acquires its lock, like labgrid's `acquire` on a place.
The OS drops these locks when the holding process dies (flock on POSIX, LockFile on
Windows), so a crashed core never leaves a device stuck.

`<device>.lock` is only ever locked, never read: Windows locks are mandatory, so the
holder's info lives next to it in `<device>.owner`.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import IO

from .runtime import WINDOWS, runtime_dir

if WINDOWS:
    import msvcrt

    def _try_lock(f: IO) -> bool:
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(f: IO) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(f: IO) -> bool:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def _unlock(f: IO) -> None:
        fcntl.flock(f, fcntl.LOCK_UN)


def _stem(device_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", device_id)


class DeviceLocks:
    def __init__(self, owner: dict, root: Path | None = None):
        self.owner = owner  # shown to other windows: who holds the device
        self.dir = (root or runtime_dir()) / "locks"
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._held: dict[str, IO] = {}

    def _lock_file(self, device_id: str) -> Path:
        return self.dir / f"{_stem(device_id)}.lock"

    def _owner_file(self, device_id: str) -> Path:
        return self.dir / f"{_stem(device_id)}.owner"

    def acquire(self, device_id: str) -> dict | None:
        """Take the device. Returns None on success, or the holder's info if someone else has it."""
        if device_id in self._held:
            return None
        f = open(self._lock_file(device_id), "a+")
        if not _try_lock(f):
            f.close()
            return self._read_owner(device_id)
        self._owner_file(device_id).write_text(json.dumps(self.owner), encoding="utf-8")
        self._held[device_id] = f
        return None

    def release(self, device_id: str) -> None:
        f = self._held.pop(device_id, None)
        if f:
            self._owner_file(device_id).unlink(missing_ok=True)
            _unlock(f)
            f.close()

    def release_all(self) -> None:
        for d in list(self._held):
            self.release(d)

    def holds(self, device_id: str) -> bool:
        return device_id in self._held

    def holder(self, device_id: str) -> dict | None:
        """Who else holds this device, without taking it. None if free or held by us."""
        if device_id in self._held or not self._lock_file(device_id).exists():
            return None
        with open(self._lock_file(device_id), "a+") as f:
            if _try_lock(f):
                _unlock(f)
                return None
        return self._read_owner(device_id)

    def _read_owner(self, device_id: str) -> dict:
        try:
            return json.loads(self._owner_file(device_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"pid": None}


def owner_info(workspace: Path) -> dict:
    return {"pid": os.getpid(), "workspace": str(Path(workspace).resolve())}
