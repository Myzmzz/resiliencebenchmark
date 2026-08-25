"""Append-only campaign artifacts with atomic JSON writes."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def write(self, campaign_id: str, relative: str, payload: Any) -> str:
        if not campaign_id.startswith("campaign-"):
            raise ValueError("invalid campaign id")
        path = (self.root / campaign_id / relative).resolve()
        campaign_root = (self.root / campaign_id).resolve()
        try:
            path.relative_to(campaign_root)
        except ValueError as exc:
            raise ValueError("artifact path escaped campaign root") from exc
        if path.suffix != ".json":
            raise ValueError("stage2 artifacts must be JSON")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        return path.relative_to(self.root).as_posix()
