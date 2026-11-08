"""Per-user runtime places: the agent channel address of each core, and the device locks.

POSIX: a Unix socket in a directory only this OS user can open.
Windows: a named pipe (the default pipe DACL lets other accounts read but not write). Pipe names are first come, first
served across accounts, so another account can create this name before the core does; the core and its relays
therefore prove to each other that they know this user's key (`agent_key`) before anything else is said.
Nothing here grants anything: approvals exist only in a core's memory (docs/notes.md review R3).
"""

from __future__ import annotations

import getpass
import hashlib
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

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


KEY_BYTES = 32


def agent_key() -> bytes:
    """The secret the core and every relay of this OS user prove to each other on the agent channel (the mutual challenge
    of multiprocessing.connection). It lives in the runtime directory, which only this user can read, so a process of
    another account that took the address first (audit 2026-11-07: on Windows, another account created the pipe and the
    relay took its answers) cannot prove it. Whichever side comes first makes it."""
    path = runtime_dir() / "agent.key"
    for _ in range(100):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        except FileExistsError:
            key = path.read_bytes()
            if len(key) == KEY_BYTES:
                return key
            time.sleep(0.02)  # another process is writing it now
            continue
        key = secrets.token_bytes(KEY_BYTES)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        return key
    raise OSError(f"{path} is not benchscout's key ({KEY_BYTES} bytes); delete it and start benchscout again")


class _Bounded:
    """A connection whose reads give up after a deadline: the challenge functions only send and receive bytes."""

    def __init__(self, conn, seconds: float):
        self.conn, self.deadline = conn, time.monotonic() + seconds

    def send_bytes(self, data: bytes) -> None:
        self.conn.send_bytes(data)

    def recv_bytes(self, maxlength: int | None = None) -> bytes:
        from multiprocessing import AuthenticationError

        if not self.conn.poll(max(0.0, self.deadline - time.monotonic())):
            raise AuthenticationError("no answer to the challenge in time")
        return self.conn.recv_bytes(maxlength)


def prove(conn, server: bool, seconds: float = 10.0) -> None:
    """Both ends of an agent channel connection show they know `agent_key()` (multiprocessing.connection's challenge,
    each way), within `seconds`: a peer that cannot, or says nothing, raises AuthenticationError before anything else is
    read from it."""
    from multiprocessing.connection import answer_challenge, deliver_challenge

    key = agent_key()
    bounded: Any = _Bounded(conn, seconds)  # the challenge functions only send and receive bytes on it
    if server:
        deliver_challenge(bounded, key)
        answer_challenge(bounded, key)
    else:
        answer_challenge(bounded, key)
        deliver_challenge(bounded, key)


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
