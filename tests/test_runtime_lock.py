from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from stage2_service.runtime_lock import RuntimeLock, RuntimeLockBusy, RuntimeLockError


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_review_entrypoint_is_updated_in_runtime_overlay():
    """The build inputs are derived from the Dockerfile now, not repeated (O18)."""
    from stage2_service.image_manifest import copied_sources

    path = "scripts/serve_stage2_matrix_review.py"
    dockerfile = (REPO_ROOT / "deploy/stage2/Dockerfile.runtime-overlay").read_text()

    assert f"COPY --chown=10001:10001 {path} /app/{path}" in dockerfile
    assert path in copied_sources(dockerfile)


def test_runtime_lock_uses_agent_exec_socket_parent(tmp_path: Path) -> None:
    socket = tmp_path / "agent-exec" / "agent.sock"

    lock = RuntimeLock.from_environment({"RESBENCH_AGENT_EXEC_SOCKET": str(socket)})

    assert lock.path == socket.parent / "stage2-active-run.lock"


def test_runtime_lock_rejects_busy_cross_process_and_releases_after_exit(tmp_path: Path) -> None:
    lock_path = tmp_path / "stage2-active-run.lock"
    ready = tmp_path / "child-ready"
    code = """
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from stage2_service.runtime_lock import RuntimeLock

with RuntimeLock(Path(sys.argv[2])).acquire(owner="qualification-cli"):
    Path(sys.argv[3]).write_text("ready", encoding="utf-8")
    time.sleep(1.5)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(REPO_ROOT), str(lock_path), str(ready)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), child.stderr.read() if child.poll() is not None else "child did not acquire lock"

        with pytest.raises(RuntimeLockBusy):
            RuntimeLock(lock_path).acquire(owner="api")
    finally:
        stdout, stderr = child.communicate(timeout=5)
    assert child.returncode == 0, stdout + stderr

    with RuntimeLock(lock_path).acquire(owner="api-after-child"):
        pass


def test_runtime_lock_releases_after_exception(tmp_path: Path) -> None:
    lock_path = tmp_path / "stage2-active-run.lock"

    with pytest.raises(ValueError):
        with RuntimeLock(lock_path).acquire(owner="failing-owner"):
            raise ValueError("boom")

    with RuntimeLock(lock_path).acquire(owner="after-exception"):
        pass


def test_runtime_lock_rejects_symlink_and_nonregular_paths(tmp_path: Path) -> None:
    target = tmp_path / "target.lock"
    link = tmp_path / "link.lock"
    link.symlink_to(target)
    directory = tmp_path / "directory.lock"
    directory.mkdir()
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(RuntimeLockError):
        RuntimeLock(link).acquire(owner="symlink")
    with pytest.raises(RuntimeLockError):
        RuntimeLock(directory).acquire(owner="directory")
    with pytest.raises(RuntimeLockError):
        RuntimeLock(linked_parent / "stage2-active-run.lock").acquire(owner="linked-parent")


def test_runtime_lock_rejects_hardlink_without_changing_target_mode_or_content(tmp_path: Path) -> None:
    target = tmp_path / "target.lock"
    target.write_text("external-content\n", encoding="utf-8")
    target.chmod(0o640)
    hardlink = tmp_path / "stage2-active-run.lock"
    hardlink.hardlink_to(target)

    with pytest.raises(RuntimeLockError, match="must not be hard-linked"):
        RuntimeLock(hardlink).acquire(owner="api")

    assert target.read_text(encoding="utf-8") == "external-content\n"
    assert stat_mode(target) == 0o640


def test_runtime_lock_busy_does_not_change_existing_metadata_or_content(tmp_path: Path) -> None:
    lock_path = tmp_path / "stage2-active-run.lock"
    lease = RuntimeLock(lock_path).acquire(owner="holder")
    try:
        lock_path.chmod(0o640)
        before = lock_path.read_text(encoding="utf-8")

        with pytest.raises(RuntimeLockBusy):
            RuntimeLock(lock_path).acquire(owner="blocked")

        assert lock_path.read_text(encoding="utf-8") == before
        assert stat_mode(lock_path) == 0o640
    finally:
        lease.release()


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
