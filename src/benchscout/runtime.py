"""Per-user runtime places: the agent channel address of each core, and the device locks.

POSIX: a Unix socket in a directory only this OS user can open.
Windows: a named pipe (the default pipe DACL lets other accounts read but not write).
Nothing here grants anything: approvals exist only in a core's memory (design.md 15 R3).
"""

from __future__ import annotations

import getpass
import hashlib
import os
import sys
import tempfile
from pathlib import Path

WINDOWS = sys.platform == "win32"


def _user() -> str:
    try:
        return str(os.getuid())  # type: ignore[attr-defined]
    except AttributeError:
        return getpass.getuser()


def runtime_dir() -> Path:
    base = os.environ.get("BENCHSCOUT_RUNTIME") or os.environ.get("XDG_RUNTIME_DIR") \
        or (os.environ.get("LOCALAPPDATA") if WINDOWS else None) or tempfile.gettempdir()
    d = Path(base) / f"benchscout-{_user()}"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not WINDOWS:
        os.chmod(d, 0o700)
    return d


def _key(workspace: Path) -> str:
    return hashlib.sha256(str(Path(workspace).resolve()).encode()).hexdigest()[:12]


def agent_address(workspace: Path) -> tuple[str, str]:
    """(address, family) where the core serving `workspace` listens for agents. Derived from
    the workspace path alone, so the core and every relay for that folder agree."""
    if WINDOWS:
        return rf"\\.\pipe\benchscout-{_user()}-{_key(workspace)}", "AF_PIPE"
    return str(runtime_dir() / f"{_key(workspace)}.sock"), "AF_UNIX"


def address_family(address: str) -> str:
    return "AF_PIPE" if address.startswith("\\\\.\\pipe\\") else "AF_UNIX"
