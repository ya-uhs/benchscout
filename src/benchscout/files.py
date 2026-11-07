"""Writing a file of the person's or the folder's: whole or not at all (memory, recipes, the agent's confirmed tools).
A stop in the middle leaves the old file as it was; no reader ever sees half of one."""

from __future__ import annotations

import os
import threading
from pathlib import Path


def write_whole(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
