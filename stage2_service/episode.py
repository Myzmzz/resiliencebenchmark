"""Immutable two-file Episode loader."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from resilience_agent.semantic_scan.episode_contracts import (
    InternalEpisode,
    PublicEpisodeTask,
)

from .contracts import FixedEpisodeRef


@dataclass(frozen=True)
class LoadedEpisode:
    ref: FixedEpisodeRef
    internal: InternalEpisode
    public: PublicEpisodeTask


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_yaml(path: Path) -> tuple[bytes, Any]:
    raw = path.read_bytes()
    return raw, yaml.safe_load(raw)


def load_fixed_episode(ref: FixedEpisodeRef, *, root: Path) -> LoadedEpisode:
    resolved_root = root.resolve()
    internal_path = (resolved_root / ref.internal_path).resolve()
    public_path = (resolved_root / ref.public_path).resolve()
    for path in (internal_path, public_path):
        try:
            path.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError("Episode path escapes the configured root") from exc
        if not path.is_file() or path.is_symlink():
            raise ValueError("Episode path must be an existing regular file")
    internal_raw, internal_data = _read_yaml(internal_path)
    public_raw, public_data = _read_yaml(public_path)
    if _digest(internal_raw) != ref.internal_sha256:
        raise ValueError("InternalEpisode SHA-256 does not match the frozen request")
    if _digest(public_raw) != ref.public_sha256:
        raise ValueError("PublicEpisodeTask SHA-256 does not match the frozen request")
    internal = InternalEpisode.model_validate(internal_data)
    public = PublicEpisodeTask.model_validate(public_data)
    if internal.identity != public.identity:
        raise ValueError("Internal and public Episode identities differ")
    if internal.identity.episode_id != ref.episode_id:
        raise ValueError("Episode id differs from the frozen campaign request")
    if "disturbances" in internal_data:
        raise ValueError("runtime disturbances must not be embedded in Episode")
    return LoadedEpisode(ref=ref, internal=internal, public=public)
