from __future__ import annotations

from pathlib import Path

from scripts.run_control_worker import main


def test_worker_once_with_no_queued_run_exits_cleanly(tmp_path: Path) -> None:
    result = main(
        [
            "--once",
            "--worker-id",
            "worker-test",
            "--database",
            str(tmp_path / "runtime" / "control.sqlite3"),
            "--artifacts-root",
            str(tmp_path / "artifacts" / "runs"),
        ]
    )

    assert result == 0
