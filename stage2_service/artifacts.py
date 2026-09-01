"""Append-only campaign artifacts with atomic JSON writes."""

from __future__ import annotations

import json
import hashlib
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

    def seal(self, campaign_id: str) -> str:
        campaign_root = (self.root / campaign_id).resolve()
        if not campaign_root.is_dir() or not campaign_id.startswith("campaign-"):
            raise ValueError("campaign artifact directory is missing")
        rows = []
        for path in sorted(item for item in campaign_root.rglob("*") if item.is_file()):
            if path.name == "manifest.sha256":
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            rows.append(f"{digest}  {path.relative_to(campaign_root).as_posix()}")
        manifest = campaign_root / "manifest.sha256"
        manifest.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
        os.chmod(manifest, 0o600)
        return manifest.relative_to(self.root).as_posix()
