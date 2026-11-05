"""Workspace execution recipes store intent, never grants or running state."""

from __future__ import annotations

import json
import os
import tempfile
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as f:
                name = f.name
                json.dump({"version": 1, "recipes": recipes}, f, ensure_ascii=False, indent=2)
            os.replace(name, self.path)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)
