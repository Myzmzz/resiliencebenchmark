"""Prompt loading with explicit file provenance."""

from __future__ import annotations

import hashlib
from pathlib import Path


class PromptRepository:
    def __init__(self, root: Path):
        self.root = root.resolve()
        if not self.root.is_dir():
            raise ValueError(f"prompt root does not exist: {self.root}")

    def read(self, name: str) -> str:
        if not name.endswith(".md") or "/" in name or ".." in name:
            raise ValueError(f"invalid prompt name: {name}")
        path = (self.root / name).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("prompt escaped the configured prompt root") from exc
        if not path.is_file():
            raise ValueError(f"prompt does not exist: {name}")
        value = path.read_text(encoding="utf-8").strip()
        if len(value) < 100:
            raise ValueError(f"prompt is unexpectedly short: {name}")
        return value

    def template_prompt(self, specialized: str) -> str:
        return self.read("common_system.md") + "\n\n" + self.read(specialized)

    def manifest(self, names: list[str]) -> dict[str, str]:
        return {
            name: hashlib.sha256(self.read(name).encode()).hexdigest()
            for name in sorted(set(names))
        }
