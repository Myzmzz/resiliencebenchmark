"""Private ledger file I/O and cross-process locking for controlled chaos."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import stat
import threading
from typing import Any

import fcntl

from mcp_servers.chaos_core.contracts import ChaosControlError


_LEDGER_LOCK_FILE = ".resbench-chaos-mutation.lock"
_LOCAL_LEDGER_LOCKS: dict[Path, threading.Lock] = {}
_LOCAL_LEDGER_LOCKS_GUARD = threading.Lock()


def _local_ledger_lock(directory: Path) -> threading.Lock:
    with _LOCAL_LEDGER_LOCKS_GUARD:
        return _LOCAL_LEDGER_LOCKS.setdefault(directory.resolve(), threading.Lock())


@asynccontextmanager
async def _ledger_file_lock(directory: Path):
    """Cancellable nonblocking lock acquisition owned by this coroutine.

    ``LOCK_NB`` and ``threading.Lock.acquire(False)`` never block the event
    loop.  A waiting coroutine owns no descriptor, so cancellation (including
    loop shutdown) cannot strand a worker-thread lease after the task has gone.
    """
    _ensure_private_ledger_directory(directory)
    local = _local_ledger_lock(directory)
    descriptor: int | None = None
    while descriptor is None:
        if not local.acquire(blocking=False):
            await asyncio.sleep(0.005)
            continue
        candidate: int | None = None
        try:
            path = directory / _LEDGER_LOCK_FILE
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            candidate = os.open(path, flags, 0o600)
            current = os.fstat(candidate)
            if not stat.S_ISREG(current.st_mode) or stat.S_IMODE(current.st_mode) != 0o600:
                os.close(candidate)
                candidate = None
                raise ChaosControlError(
                    "LEDGER_LOCK_UNSAFE",
                    "The Controller mutation lock is not a private regular file.",
                    next_step="Replace the private Controller ledger lock file before enabling chaos writes.",
                )
            try:
                fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(candidate)
                candidate = None
                await asyncio.sleep(0.005)
                continue
            descriptor = candidate
            candidate = None
        except BaseException:
            if candidate is not None:
                try:
                    os.close(candidate)
                except OSError:
                    pass
            raise
        finally:
            if descriptor is None:
                local.release()
    try:
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            try:
                os.close(descriptor)
            finally:
                local.release()


def _assert_private_directory(path: Path, label: str) -> None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError as exc:
        raise ChaosControlError(
            "LEDGER_DIRECTORY_MISSING",
            f"{label} directory does not exist.",
            next_step=f"Create the controller-owned {label} directory with mode 0700 before enabling writes.",
        ) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise ChaosControlError(
            "LEDGER_DIRECTORY_UNSAFE",
            f"{label} path must be a real directory, not a symlink or special file.",
            next_step=f"Replace the {label} path with a controller-owned directory with mode 0700.",
        )
    if stat.S_IMODE(mode) != 0o700:
        raise ChaosControlError(
            "LEDGER_DIRECTORY_MODE_UNSAFE",
            f"{label} directory must have mode 0700.",
            next_step=f"Fix {label} directory permissions before enabling writes.",
        )


def _ensure_private_ledger_directory(path: Path) -> None:
    """Create the Controller ledger once, then enforce its private boundary."""
    if path.exists():
        _assert_private_directory(path, "cleanup ledger")
        return
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    _assert_private_directory(path, "cleanup ledger")


def _read_private_json_file(
    path: Path,
    *,
    label: str,
    missing_code: str,
    missing_message: str,
    missing_next_step: str,
) -> dict[str, Any]:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError as exc:
        raise ChaosControlError(missing_code, missing_message, next_step=missing_next_step) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise ChaosControlError(
            "LEDGER_FILE_UNSAFE",
            f"{label} must be a regular file, not a symlink or special file.",
            next_step="Pause chaos writes and replace the ledger entry with a controller-owned regular file.",
        )
    if stat.S_IMODE(mode) != 0o600:
        raise ChaosControlError(
            "LEDGER_FILE_MODE_UNSAFE",
            f"{label} must have mode 0600.",
            next_step="Fix ledger file permissions before using this capability or cleanup handle.",
        )
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ChaosControlError(
            "LEDGER_FILE_UNREADABLE",
            f"{label} is not valid JSON.",
            next_step="Pause chaos writes and reconcile the controller ledger manually.",
        ) from exc
    if not isinstance(payload, dict):
        raise ChaosControlError(
            "LEDGER_FILE_UNREADABLE",
            f"{label} JSON must be an object.",
            next_step="Pause chaos writes and reconcile the controller ledger manually.",
        )
    return payload
