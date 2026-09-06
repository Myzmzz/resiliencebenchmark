"""Read bounded, Controller-private evidence produced by the actual gateway."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

MAX_AUDIT_BYTES = 1_000_000
MAX_ROW_BYTES = 4096
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$")


def read_gateway_requests(
    root: Path, *, trial_id: str, harness: str, model_alias: str,
    config_sha256: str, request_ids: set[str],
) -> list[dict[str, Any]] | None:
    """Return matched proxy receipts, not claims of successful model completion."""
    if not request_ids or not _SAFE_COMPONENT.fullmatch(trial_id):
        return None
    if not all(isinstance(value, str) and value for value in request_ids):
        return None
    root = Path(root)
    if root.is_symlink():
        return None
    try:
        root = root.resolve(strict=True)
        path = root / f"{trial_id}.jsonl"
        # Do not resolve the file first: that would erase an in-directory symlink.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH)
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o022 or info.st_size > MAX_AUDIT_BYTES):
                return None
            raw = handle.read(MAX_AUDIT_BYTES + 1)
    except (OSError, ValueError):
        return None
    if not raw or len(raw) > MAX_AUDIT_BYTES or not raw.endswith(b"\n"):
        return None
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line or len(line) > MAX_ROW_BYTES:
            return None
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            return None
        rows.append(row)
    return _validate_rows(
        rows, trial_id=trial_id, harness=harness, model_alias=model_alias,
        config_sha256=config_sha256, request_ids=request_ids,
    )


def read_gateway_artifact(
    path: Path, *, trial_id: str, harness: str, model_alias: str,
    config_sha256: str, request_ids: set[str],
) -> list[dict[str, Any]] | None:
    """Revalidate a durable JSON receipt array when consuming qualification data."""
    path = Path(path)
    if not request_ids or not _SAFE_COMPONENT.fullmatch(trial_id):
        return None
    # Do not resolve away symbolic links before rejecting them. The caller also
    # confines this reference to its campaign artifact root.
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        return None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH)
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022
                    or info.st_size > MAX_AUDIT_BYTES):
                return None
            raw = handle.read(MAX_AUDIT_BYTES + 1)
        if len(raw) > MAX_AUDIT_BYTES:
            return None
        rows = json.loads(raw)
    except (OSError, ValueError, UnicodeError):
        return None
    return _validate_rows(
        rows, trial_id=trial_id, harness=harness, model_alias=model_alias,
        config_sha256=config_sha256, request_ids=request_ids,
    )


def _validate_rows(
    rows: Any, *, trial_id: str, harness: str, model_alias: str,
    config_sha256: str, request_ids: set[str],
) -> list[dict[str, Any]] | None:
    if (not isinstance(rows, list) or not rows or not request_ids
            or not all(isinstance(value, str) and _SAFE_COMPONENT.fullmatch(value)
                       for value in request_ids)):
        return None
    found: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or len(json.dumps(row).encode()) > MAX_ROW_BYTES:
            return None
        if (row.get("trial_id") != trial_id or row.get("harness") != harness
                or row.get("model_alias") != model_alias
                or row.get("gateway_config_sha256") != config_sha256
                or row.get("outcome") != "received"):
            return None
        request_id = row.get("request_id")
        if not isinstance(request_id, str) or request_id not in request_ids or request_id in found:
            return None
        # Only permit bounded metadata in the durable copy, even if a file was
        # written by an incorrect logger configuration.
        allowed = {
            "schema_version", "trial_id", "harness", "model_alias", "request_id",
            "gateway_config_sha256", "outcome", "status", "recorded_at",
            "started_at", "finished_at", "server_call_id", "path",
        }
        if not set(row) <= allowed:
            return None
        if any(value is not None and not isinstance(value, (str, int, float, bool))
               for value in row.values()):
            return None
        found.add(request_id)
    return rows if found == request_ids else None


def verify_gateway_requests(
    root: Path, *, trial_id: str, harness: str, model_alias: str,
    config_sha256: str, request_ids: set[str],
) -> bool:
    """Verify all IDs without treating an empty set or partial log as success."""
    return read_gateway_requests(
        root, trial_id=trial_id, harness=harness, model_alias=model_alias,
        config_sha256=config_sha256, request_ids=request_ids,
    ) is not None
