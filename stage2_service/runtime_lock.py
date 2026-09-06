"""Single-host Stage-2 AgentExec/MCP runtime lock."""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock


LOCK_FILE_NAME = "stage2-active-run.lock"
DEFAULT_LOCK_DIR = Path("/run/resbench")


class RuntimeLockError(RuntimeError):
    """Base error for Stage-2 runtime lock failures."""


class RuntimeLockBusy(RuntimeLockError):
    """Raised when this AgentExec/MCP runtime is already held."""


@dataclass
class RuntimeLockLease:
    """An acquired non-blocking flock held by an open file descriptor."""

    path: Path
    owner: str
    _fd: int | None
    _guard: Lock

    def release(self) -> None:
        with self._guard:
            if self._fd is None:
                return
            fd = self._fd
            self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> RuntimeLockLease:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


class RuntimeLock:
    """Non-blocking file lock for one local Stage-2 runtime instance.

    The lock protects the local AgentExec/MCP loopback runtime only.  It is not a
    distributed Kubernetes or SUT mutation lease, and releasing it does not prove
    fault cleanup or environment recovery.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    @classmethod
    def from_environment(cls, environ: dict[str, str] | None = None) -> RuntimeLock:
        env = environ if environ is not None else os.environ
        socket = str(env.get("RESBENCH_AGENT_EXEC_SOCKET") or "").strip()
        directory = Path(socket).expanduser().parent if socket else DEFAULT_LOCK_DIR
        return cls(directory / LOCK_FILE_NAME)

    def acquire(self, *, owner: str) -> RuntimeLockLease:
        lock_path = self.path.expanduser()
        _reject_symlink_path(lock_path)
        parent = lock_path.parent
        if parent.exists():
            if parent.is_symlink() or not parent.is_dir():
                raise RuntimeLockError(f"runtime lock parent must be a directory: {parent}")
        else:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(parent, 0o700)
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise RuntimeLockError(f"runtime lock path must not be a symlink: {lock_path}") from exc
            raise
        try:
            stat_result = os.fstat(fd)
            if not stat.S_ISREG(stat_result.st_mode):
                raise RuntimeLockError(f"runtime lock path must be a regular file: {lock_path}")
            if stat_result.st_uid != os.getuid():
                raise RuntimeLockError(f"runtime lock path must be owned by the current user: {lock_path}")
            if stat_result.st_nlink != 1:
                raise RuntimeLockError(f"runtime lock path must not be hard-linked: {lock_path}")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeLockBusy(f"Stage-2 runtime is already active: {lock_path}") from exc
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                    raise RuntimeLockBusy(f"Stage-2 runtime is already active: {lock_path}") from exc
                raise
            os.fchmod(fd, 0o600)
            os.ftruncate(fd, 0)
            payload = (
                f"owner={owner}\n"
                f"acquired_at={datetime.now(UTC).isoformat()}\n"
                "scope=local-agentexec-mcp-runtime\n"
            )
            os.write(fd, payload.encode("utf-8"))
            return RuntimeLockLease(
                path=lock_path.resolve(),
                owner=owner,
                _fd=fd,
                _guard=Lock(),
            )
        except Exception:
            os.close(fd)
            raise


def _reject_symlink_path(path: Path) -> None:
    candidate = path if path.is_absolute() else Path.cwd() / path
    current = candidate
    existing: list[Path] = []
    while True:
        if current.exists() or current.is_symlink():
            existing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for item in reversed(existing):
        if item.is_symlink():
            raise RuntimeLockError(f"runtime lock path must not contain symlinks: {item}")
    if candidate.exists() and not candidate.is_file():
        raise RuntimeLockError(f"runtime lock path must be a regular file: {candidate}")
