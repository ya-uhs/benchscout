"""Per-user runtime places: the agent channel address of each core, and the device locks.

POSIX: a Unix socket in a directory only this OS user can open.
Windows: a named pipe (the default pipe DACL lets other accounts read but not write).
Nothing here grants anything: approvals exist only in a core's memory (docs/notes.md review R3).
"""

from __future__ import annotations

import getpass
import hashlib
import os
import sys
from pathlib import Path

WINDOWS = sys.platform == "win32"


def _user() -> str:
    try:
        return str(os.getuid())  # type: ignore[attr-defined]
    except AttributeError:
        return getpass.getuser()


def runtime_dir() -> Path:
    """Must come out the same in the core and in every relay. MCP clients start servers with a
    trimmed environment (no TMPDIR, often no XDG_RUNTIME_DIR), so this depends only on the
    user's home, never on temp-dir variables. Real bench, 2026-10-31: the core put its socket
    under $TMPDIR and the relay, lacking TMPDIR, looked in /tmp."""
    if os.environ.get("BENCHSCOUT_RUNTIME"):  # tests; set it for both sides or neither
        base = Path(os.environ["BENCHSCOUT_RUNTIME"])
    elif WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path.home() / ".cache"
    d = base / f"benchscout-{_user()}"
    if not WINDOWS and len(str(d)) > 80:  # keep socket paths under the ~104-byte AF_UNIX limit
        d = Path("/tmp") / f"benchscout-{_user()}"
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
