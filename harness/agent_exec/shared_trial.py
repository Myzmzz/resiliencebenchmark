"""Portable ownership normalization for the controller/Agent shared Trial tree."""

from __future__ import annotations

import os
import stat
from pathlib import Path


DEFAULT_SHARED_TRIAL_GID = 10004


def normalize_shared_trial_tree(
    cwd: Path,
    shared_gid: int = DEFAULT_SHARED_TRIAL_GID,
) -> None:
    """Make only regular Agent output controller-readable without following links.

    The shared Trial root is distinct from every Controller-private directory.
    A symlink or non-regular entry is rejected instead of being chased by the
    Controller later.  Call this after the Agent exits and before terminal
    evidence is accepted.
    """
    if shared_gid <= 0:
        raise RuntimeError("shared Trial group is invalid")
    entries = [cwd]
    while entries:
        current = entries.pop()
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError("Agent Trial output may not contain symlinks")
        if stat.S_ISDIR(metadata.st_mode):
            _ensure_metadata(current, metadata, shared_gid, 0o2770)
            with os.scandir(current) as children:
                entries.extend(Path(child.path) for child in children)
            continue
        if stat.S_ISREG(metadata.st_mode):
            # Keep an executable bit when an Agent generated a helper, while
            # making ordinary 0600 final reports readable to Controller group.
            owner_bits = stat.S_IMODE(metadata.st_mode) & 0o700
            _ensure_metadata(current, metadata, shared_gid, owner_bits | 0o060)
            continue
        raise RuntimeError("Agent Trial output contains an unsupported file type")


def _ensure_metadata(path: Path, metadata: os.stat_result, shared_gid: int, mode: int) -> None:
    """Avoid mutating already-normalized Agent-owned outputs from Controller."""
    current_mode = stat.S_IMODE(metadata.st_mode)
    try:
        if metadata.st_gid != shared_gid:
            os.chown(path, -1, shared_gid)
        if current_mode != mode:
            os.chmod(path, mode)
    except PermissionError as exc:
        raise RuntimeError("shared Trial output metadata is not controller-normalized") from exc
