"""Per-user runtime files: the agent socket of each core and the device locks.

Everything lives in a directory only this OS user can read and write. Nothing in it
grants anything: approvals exist only in a core's memory (design.md section 15 R3).
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path


def runtime_dir() -> Path:
    base = os.environ.get("BENCHSCOUT_RUNTIME") or os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    uid = os.getuid() if hasattr(os, "getuid") else os.environ.get("USERNAME", "user")
    d = Path(base) / f"benchscout-{uid}"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    if sys.platform != "win32":
        os.chmod(d, 0o700)
    return d


def socket_path(workspace: Path) -> Path:
    """Where the core serving `workspace` listens for agents. Derived from the workspace path
    alone, so the core and every relay started for that folder agree without a shared file."""
    key = hashlib.sha256(str(Path(workspace).resolve()).encode()).hexdigest()[:12]
    return runtime_dir() / f"{key}.sock"
