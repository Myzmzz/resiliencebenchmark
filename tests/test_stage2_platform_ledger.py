from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from stage2_service.platform_ledger import PlatformLedger


_SUBPROCESS_APPEND = """
from datetime import datetime, timezone
from pathlib import Path
import sys
from stage2_service.platform_ledger import PlatformLedger

root = Path(sys.argv[1])
worker_id = int(sys.argv[2])
count = int(sys.argv[3])
ledger = PlatformLedger(root)
for index in range(count):
    ledger.append(
        trial_id=f"trial-{worker_id}",
        event_type="MCP_TOOL_RESULT",
        occurred_at=datetime(2026, 9, 5, 12, 0, index, tzinfo=timezone.utc),
        payload={"worker": worker_id, "index": index},
    )
"""


def test_appends_query_pages_and_exports_jsonl(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    occurred_at = "2020-09-05T10:00:00+00:00"

    first = ledger.append(
        trial_id="trial-a",
        event_type="TOOL_WITHDRAWN",
        occurred_at=occurred_at,
        payload={"tool": "telemetry_prom_query"},
    )
    second = ledger.append(
        trial_id="trial-a",
        event_type="TOOL_SUBSTITUTED",
        occurred_at=datetime(2020, 9, 5, 10, 0, 1, tzinfo=timezone.utc),
        payload={"tool": "coroot_query"},
    )
    ledger.append(
        trial_id="trial-b",
        event_type="OTHER_TRIAL",
        occurred_at=datetime(2020, 9, 5, 10, 0, 2, tzinfo=timezone.utc),
        payload={},
    )

    assert first.sequence == 1
    assert second.sequence == 2
    assert first.occurred_at == occurred_at
    assert first.recorded_at >= first.occurred_at
    assert [event.event_type for event in ledger.query(after_sequence=1, limit=1)] == [
        "TOOL_SUBSTITUTED"
    ]
    assert [event.sequence for event in ledger.query(trial_id="trial-a")] == [1, 2]
    exported = [json.loads(row) for row in ledger.export_jsonl_rows(trial_id="trial-a")]
    assert exported == [first.as_dict(), second.as_dict()]


def test_private_files_and_safe_database_scope(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "private")
    ledger.append(
        trial_id="trial-a",
        event_type="STARTED",
        occurred_at=datetime.now(timezone.utc),
        payload={},
    )

    assert oct((tmp_path / "private").stat().st_mode & 0o777) == "0o700"
    assert oct(ledger.path.stat().st_mode & 0o777) == "0o600"
    for suffix in ("-wal", "-shm"):
        path = Path(f"{ledger.path}{suffix}")
        if path.exists():
            assert oct(path.stat().st_mode & 0o777) == "0o600"

    with pytest.raises(ValueError, match="inside the ledger root"):
        PlatformLedger(tmp_path / "private", database_name="../escape.sqlite3")
    with pytest.raises(ValueError, match="sqlite/db suffix"):
        PlatformLedger(tmp_path / "private", database_name="ledger.txt")


def test_wal_sidecar_disappearing_during_permission_fix_is_safe_but_main_db_is_not(tmp_path: Path, monkeypatch) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    calls = []
    real_chmod = os.chmod

    def disappearing_sidecar(path, mode):
        calls.append(Path(path).name)
        if str(path).endswith("-shm"):
            raise FileNotFoundError(path)
        return real_chmod(path, mode)

    monkeypatch.setattr("stage2_service.platform_ledger.os.chmod", disappearing_sidecar)
    ledger._chmod_sqlite_files()
    assert any(name.endswith("-shm") for name in calls)

    def missing_main(path, mode):
        if str(path) == str(ledger.path):
            raise FileNotFoundError(path)
        return real_chmod(path, mode)

    monkeypatch.setattr("stage2_service.platform_ledger.os.chmod", missing_main)
    with pytest.raises(FileNotFoundError):
        ledger._chmod_sqlite_files()

    def permission_error(path, mode):
        raise PermissionError(path)

    monkeypatch.setattr("stage2_service.platform_ledger.os.chmod", permission_error)
    with pytest.raises(PermissionError):
        ledger._chmod_sqlite_files()


def test_process_concurrent_appends_have_unique_monotonic_sequences(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    process_count = 4
    events_per_process = 25
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _SUBPROCESS_APPEND,
                str(root),
                str(worker_id),
                str(events_per_process),
            ],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for worker_id in range(process_count)
    ]

    failures = []
    for process in processes:
        try:
            stdout, stderr = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
            failures.append({"returncode": "timeout", "stdout": stdout, "stderr": stderr})
            continue
        if process.returncode != 0:
            failures.append(
                {
                    "returncode": process.returncode,
                    "stdout": stdout,
                    "stderr": stderr,
                }
            )
    assert failures == []

    events = PlatformLedger(root).query(limit=process_count * events_per_process)
    sequences = [event.sequence for event in events]
    assert len(events) == process_count * events_per_process
    assert sequences == list(range(1, process_count * events_per_process + 1))


def test_event_table_is_append_only_and_failed_payload_rolls_back(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    event = ledger.append(
        trial_id="trial-a",
        event_type="STARTED",
        occurred_at=datetime.now(timezone.utc),
        payload={"ok": True},
    )

    with pytest.raises(ValueError, match="JSON serializable"):
        ledger.append(
            trial_id="trial-a",
            event_type="BAD_PAYLOAD",
            occurred_at=datetime.now(timezone.utc),
            payload={"bad": object()},
        )
    assert [item.sequence for item in ledger.query()] == [event.sequence]

    with sqlite3.connect(ledger.path) as connection:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute(
                "UPDATE platform_events SET event_type = 'MUTATED' WHERE sequence = ?",
                (event.sequence,),
            )
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute(
                "DELETE FROM platform_events WHERE sequence = ?",
                (event.sequence,),
            )
    assert ledger.query()[0].event_type == "STARTED"


def test_notice_queue_claim_retry_delivery_and_idempotency(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    first = ledger.enqueue_notice(
        trial_id="trial-a",
        notice_type="FACT_EVENT",
        payload={"message": "tool withdrawn"},
        idempotency_key="withdrawn-1",
    )
    replay = ledger.enqueue_notice(
        trial_id="trial-a",
        notice_type="FACT_EVENT",
        payload={"message": "tool withdrawn"},
        idempotency_key="withdrawn-1",
    )
    assert replay.notice_id == first.notice_id
    with pytest.raises(ValueError, match="changed notice content"):
        ledger.enqueue_notice(
            trial_id="trial-a",
            notice_type="FACT_EVENT",
            payload={"message": "different"},
            idempotency_key="withdrawn-1",
        )

    now = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    claimed = ledger.claim_notice(
        trial_id="trial-a",
        claimed_by="codex",
        lease_seconds=10,
        now=now,
    )
    assert claimed is not None
    assert claimed.notice.notice_id == first.notice_id
    assert claimed.attempt == 1
    assert ledger.pending_notices(trial_id="trial-a", now=now) == []
    assert len(ledger.pending_notices(trial_id="trial-a", include_claimed=True, now=now)) == 1

    retried = ledger.claim_notice(
        trial_id="trial-a",
        claimed_by="claude-code",
        lease_seconds=10,
        now=now + timedelta(seconds=11),
    )
    assert retried is not None
    assert retried.notice.notice_id == first.notice_id
    assert retried.delivery_id != claimed.delivery_id
    assert retried.attempt == 2

    delivered = ledger.deliver_notice(
        delivery_id=claimed.delivery_id,
        delivered_at=now + timedelta(seconds=12),
    )
    repeated_ack = ledger.deliver_notice(
        delivery_id=claimed.delivery_id,
        delivered_at=now + timedelta(seconds=30),
    )
    assert delivered.delivered_at == repeated_ack.delivered_at
    assert ledger.pending_notices(trial_id="trial-a", include_claimed=True) == []
    assert ledger.claim_notice(trial_id="trial-a") is None


def test_notice_pending_can_be_scoped_by_trial(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    ledger.enqueue_notice(
        trial_id="trial-a",
        notice_type="FACT_EVENT",
        payload={},
        idempotency_key="a",
    )
    ledger.enqueue_notice(
        trial_id="trial-b",
        notice_type="SEMANTIC_NUDGE",
        payload={},
        idempotency_key="b",
    )

    assert [notice.trial_id for notice in ledger.pending_notices(trial_id="trial-b")] == [
        "trial-b"
    ]
    assert [notice.trial_id for notice in ledger.pending_notices()] == [
        "trial-a",
        "trial-b",
    ]
