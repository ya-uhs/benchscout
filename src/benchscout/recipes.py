"""Workspace execution recipes store intent, never grants or running state."""

from __future__ import annotations

import json
from pathlib import Path


class Recipes:
    def __init__(self, path: Path):
        self.path = path

    def read(self) -> dict:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("recipes"), dict):
            raise ValueError("invalid recipe file; fix it before saving (existing data is preserved)")
        return data["recipes"]

    def write(self, recipes: dict) -> None:
        from .files import write_whole

        write_whole(self.path, json.dumps({"version": 1, "recipes": recipes}, ensure_ascii=False, indent=2))
