"""SQLite persistence for the benchmark run control plane."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from .run_contracts import (
    ALLOWED_PHASE_TRANSITIONS,
    MutationLease,
    RunEvent,
    RunPhase,
    RunRecord,
    RunSpec,
    RunTerminalStatus,
    WorkerLease,
)


class RunStoreError(RuntimeError):
    pass


class RunNotFoundError(RunStoreError):
    pass


class TransitionError(RunStoreError):
    pass


class LeaseConflictError(RunStoreError):
    pass


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _canonical_spec(spec: RunSpec) -> str:
    return json.dumps(
        spec.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


class RunStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    spec_json TEXT NOT NULL,
                    spec_sha256 TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    terminal_status TEXT,
                    desired_terminal_status TEXT,
                    abort_requested INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS run_events (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    terminal_status TEXT,
                    occurred_at TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    UNIQUE(run_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS mutation_lease (
                    scope TEXT PRIMARY KEY CHECK(scope = 'cluster-mutation'),
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    acquired_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS worker_leases (
                    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                    worker_id TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                """
            )

    def create_or_get(self, run_id: str, spec: RunSpec) -> tuple[RunRecord, bool]:
        spec_json = _canonical_spec(spec)
        digest = hashlib.sha256(spec_json.encode("utf-8")).hexdigest()
        now = _utc_now()
        with self._write() as connection:
            existing = connection.execute(
                "SELECT * FROM runs WHERE request_id = ?", (spec.request_id,)
            ).fetchone()
            if existing is not None:
                if existing["spec_sha256"] != digest:
                    raise ValueError(
                        "idempotency key request_id already exists with a different RunSpec"
                    )
                return self._record(existing), False
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, request_id, spec_json, spec_sha256, phase,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    spec.request_id,
                    spec_json,
                    digest,
                    RunPhase.CREATED.value,
                    _iso(now),
                    _iso(now),
                ),
            )
            self._append_event(
                connection,
                run_id=run_id,
                event_type="RUN_CREATED",
                phase=RunPhase.CREATED,
                terminal_status=None,
                detail={"mode": spec.mode.value},
                occurred_at=now,
            )
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            assert row is not None
            return self._record(row), True

    def get(self, run_id: str) -> RunRecord:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFoundError(f"run not found: {run_id}")
        return self._record(row)

    def list_runs(self, *, limit: int = 100) -> list[RunRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._record(row) for row in rows]

    def list_events(self, run_id: str, *, after_sequence: int = 0) -> list[RunEvent]:
        self.get(run_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM run_events
                WHERE run_id = ? AND sequence > ?
                ORDER BY sequence ASC
                """,
                (run_id, after_sequence),
            ).fetchall()
        return [self._event(row) for row in rows]

    def append_event(
        self,
        run_id: str,
        *,
        event_type: str,
        detail: dict[str, Any],
    ) -> RunRecord:
        if not re.fullmatch(r"^[A-Z][A-Z0-9_]{2,63}$", event_type):
            raise ValueError("event_type must be an uppercase stable identifier")
        now = _utc_now()
        with self._write() as connection:
            row = self._require_row(connection, run_id)
            terminal = (
                RunTerminalStatus(row["terminal_status"])
                if row["terminal_status"]
                else None
            )
            self._append_event(
                connection,
                run_id=run_id,
                event_type=event_type,
                phase=RunPhase(row["phase"]),
                terminal_status=terminal,
                detail=detail,
                occurred_at=now,
            )
            connection.execute(
                "UPDATE runs SET updated_at = ? WHERE run_id = ?",
                (_iso(now), run_id),
            )
            return self._record(self._require_row(connection, run_id))

    def transition(
        self,
        run_id: str,
        next_phase: RunPhase,
        *,
        detail: dict[str, Any] | None = None,
    ) -> RunRecord:
        now = _utc_now()
        with self._write() as connection:
            row = self._require_row(connection, run_id)
            if row["terminal_status"] is not None:
                raise TransitionError("terminal run cannot transition")
            current = RunPhase(row["phase"])
            if next_phase not in ALLOWED_PHASE_TRANSITIONS[current]:
                raise TransitionError(
                    f"cannot transition from {current.value} to {next_phase.value}"
                )
            connection.execute(
                """
                UPDATE runs
                SET phase = ?, revision = revision + 1, updated_at = ?
                WHERE run_id = ?
                """,
                (next_phase.value, _iso(now), run_id),
            )
            self._append_event(
                connection,
                run_id=run_id,
                event_type="PHASE_TRANSITIONED",
                phase=next_phase,
                terminal_status=None,
                detail=detail or {},
                occurred_at=now,
            )
            return self._record(self._require_row(connection, run_id))

    def request_cleanup(
        self,
        run_id: str,
        *,
        terminal_status: RunTerminalStatus,
        reason: str,
        abort_requested: bool = False,
    ) -> RunRecord:
        now = _utc_now()
        with self._write() as connection:
            row = self._require_row(connection, run_id)
            if row["terminal_status"] is not None:
                return self._record(row)
            current = RunPhase(row["phase"])
            existing_desired = row["desired_terminal_status"]
            if current is RunPhase.CLEANING_UP:
                if existing_desired != terminal_status.value:
                    raise TransitionError(
                        "cleanup already requested with a different terminal status"
                    )
                if abort_requested and not bool(row["abort_requested"]):
                    connection.execute(
                        "UPDATE runs SET abort_requested = 1, updated_at = ? WHERE run_id = ?",
                        (_iso(now), run_id),
                    )
                return self._record(self._require_row(connection, run_id))

            connection.execute(
                """
                UPDATE runs
                SET phase = ?, desired_terminal_status = ?, abort_requested = ?,
                    revision = revision + 1, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    RunPhase.CLEANING_UP.value,
                    terminal_status.value,
                    int(abort_requested or bool(row["abort_requested"])),
                    _iso(now),
                    run_id,
                ),
            )
            self._append_event(
                connection,
                run_id=run_id,
                event_type="ABORT_REQUESTED" if abort_requested else "CLEANUP_REQUESTED",
                phase=RunPhase.CLEANING_UP,
                terminal_status=None,
                detail={"reason": reason, "desired_terminal_status": terminal_status.value},
                occurred_at=now,
            )
            return self._record(self._require_row(connection, run_id))

    def finish_cleanup(
        self,
        run_id: str,
        *,
        verified: bool,
        detail: dict[str, Any] | None = None,
    ) -> RunRecord:
        now = _utc_now()
        with self._write() as connection:
            row = self._require_row(connection, run_id)
            if row["terminal_status"] is not None:
                return self._record(row)
            if RunPhase(row["phase"]) is not RunPhase.CLEANING_UP:
                raise TransitionError("terminal status can only be set after cleanup was requested")
            desired = row["desired_terminal_status"]
            if desired is None:
                raise TransitionError("cleanup has no desired terminal status")
            terminal = (
                RunTerminalStatus(desired)
                if verified
                else RunTerminalStatus.RESET_FAILED
            )
            last_error = None if verified else "cleanup verification failed"
            connection.execute(
                """
                UPDATE runs
                SET terminal_status = ?, revision = revision + 1, updated_at = ?, last_error = ?
                WHERE run_id = ?
                """,
                (terminal.value, _iso(now), last_error, run_id),
            )
            self._append_event(
                connection,
                run_id=run_id,
                event_type="RUN_TERMINATED",
                phase=RunPhase.CLEANING_UP,
                terminal_status=terminal,
                detail={"cleanup_verified": verified, **(detail or {})},
                occurred_at=now,
            )
            connection.execute(
                "DELETE FROM mutation_lease WHERE scope = 'cluster-mutation' AND run_id = ?",
                (run_id,),
            )
            return self._record(self._require_row(connection, run_id))

    def acquire_mutation_lease(
        self,
        run_id: str,
        *,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> MutationLease:
        if not 5 <= ttl_seconds <= 600:
            raise ValueError("mutation lease ttl_seconds must be between 5 and 600")
        current_time = _as_utc(now or _utc_now())
        expires_at = current_time + timedelta(seconds=ttl_seconds)
        with self._write() as connection:
            run = self._require_row(connection, run_id)
            if run["terminal_status"] is not None:
                raise LeaseConflictError("terminal run cannot acquire mutation lease")
            row = connection.execute(
                "SELECT * FROM mutation_lease WHERE scope = 'cluster-mutation'"
            ).fetchone()
            if row is not None:
                owner = str(row["run_id"])
                expiry = datetime.fromisoformat(str(row["expires_at"]))
                if owner != run_id and expiry > current_time:
                    raise LeaseConflictError(f"mutation lease is held by {owner}")
                acquired_at = (
                    datetime.fromisoformat(str(row["acquired_at"]))
                    if owner == run_id and expiry > current_time
                    else current_time
                )
            else:
                acquired_at = current_time
            connection.execute(
                """
                INSERT INTO mutation_lease (
                    scope, run_id, acquired_at, heartbeat_at, expires_at
                ) VALUES ('cluster-mutation', ?, ?, ?, ?)
                ON CONFLICT(scope) DO UPDATE SET
                    run_id = excluded.run_id,
                    acquired_at = excluded.acquired_at,
                    heartbeat_at = excluded.heartbeat_at,
                    expires_at = excluded.expires_at
                """,
                (run_id, _iso(acquired_at), _iso(current_time), _iso(expires_at)),
            )
            self._append_event(
                connection,
                run_id=run_id,
                event_type="MUTATION_LEASE_ACQUIRED",
                phase=RunPhase(run["phase"]),
                terminal_status=None,
                detail={"expires_at": _iso(expires_at)},
                occurred_at=current_time,
            )
            return MutationLease(
                run_id=run_id,
                acquired_at=acquired_at,
                heartbeat_at=current_time,
                expires_at=expires_at,
            )

    def get_mutation_lease(self) -> MutationLease | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM mutation_lease WHERE scope = 'cluster-mutation'"
            ).fetchone()
        if row is None:
            return None
        return MutationLease(
            run_id=str(row["run_id"]),
            acquired_at=datetime.fromisoformat(str(row["acquired_at"])),
            heartbeat_at=datetime.fromisoformat(str(row["heartbeat_at"])),
            expires_at=datetime.fromisoformat(str(row["expires_at"])),
        )

    def release_mutation_lease(self, run_id: str) -> bool:
        now = _utc_now()
        with self._write() as connection:
            run = self._require_row(connection, run_id)
            cursor = connection.execute(
                "DELETE FROM mutation_lease WHERE scope = 'cluster-mutation' AND run_id = ?",
                (run_id,),
            )
            if cursor.rowcount:
                self._append_event(
                    connection,
                    run_id=run_id,
                    event_type="MUTATION_LEASE_RELEASED",
                    phase=RunPhase(run["phase"]),
                    terminal_status=(
                        RunTerminalStatus(run["terminal_status"])
                        if run["terminal_status"]
                        else None
                    ),
                    detail={},
                    occurred_at=now,
                )
                return True
            return False

    def claim_next_run(
        self,
        worker_id: str,
        *,
        phases: tuple[RunPhase, ...] | None = None,
        ttl_seconds: int = 900,
        now: datetime | None = None,
    ) -> WorkerLease | None:
        if not re.fullmatch(r"^[a-z0-9][a-z0-9._-]{2,63}$", worker_id):
            raise ValueError("worker_id must be a stable safe identifier")
        if not 30 <= ttl_seconds <= 3600:
            raise ValueError("worker lease ttl_seconds must be between 30 and 3600")
        current_time = _as_utc(now or _utc_now())
        expires_at = current_time + timedelta(seconds=ttl_seconds)
        selected_phases = phases or tuple(RunPhase)
        if not selected_phases:
            raise ValueError("worker claim phases cannot be empty")
        placeholders = ",".join("?" for _ in selected_phases)
        with self._write() as connection:
            connection.execute(
                "DELETE FROM worker_leases WHERE expires_at <= ?", (_iso(current_time),)
            )
            row = connection.execute(
                f"""
                SELECT runs.*
                FROM runs
                LEFT JOIN worker_leases ON worker_leases.run_id = runs.run_id
                WHERE runs.terminal_status IS NULL
                  AND worker_leases.run_id IS NULL
                  AND runs.phase IN ({placeholders})
                ORDER BY runs.created_at ASC
                LIMIT 1
                """,
                tuple(phase.value for phase in selected_phases),
            ).fetchone()
            if row is None:
                return None
            run_id = str(row["run_id"])
            connection.execute(
                """
                INSERT INTO worker_leases (
                    run_id, worker_id, acquired_at, heartbeat_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    worker_id,
                    _iso(current_time),
                    _iso(current_time),
                    _iso(expires_at),
                ),
            )
            self._append_event(
                connection,
                run_id=run_id,
                event_type="WORKER_CLAIMED",
                phase=RunPhase(row["phase"]),
                terminal_status=None,
                detail={"worker_id": worker_id, "expires_at": _iso(expires_at)},
                occurred_at=current_time,
            )
            return WorkerLease(
                run_id=run_id,
                worker_id=worker_id,
                acquired_at=current_time,
                heartbeat_at=current_time,
                expires_at=expires_at,
            )

    def renew_worker_lease(
        self,
        run_id: str,
        worker_id: str,
        *,
        ttl_seconds: int = 900,
        now: datetime | None = None,
    ) -> WorkerLease:
        current_time = _as_utc(now or _utc_now())
        expires_at = current_time + timedelta(seconds=ttl_seconds)
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM worker_leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None or row["worker_id"] != worker_id:
                raise LeaseConflictError("worker does not own this run lease")
            if datetime.fromisoformat(str(row["expires_at"])) <= current_time:
                raise LeaseConflictError("worker lease has expired")
            connection.execute(
                """
                UPDATE worker_leases
                SET heartbeat_at = ?, expires_at = ?
                WHERE run_id = ? AND worker_id = ?
                """,
                (_iso(current_time), _iso(expires_at), run_id, worker_id),
            )
            return WorkerLease(
                run_id=run_id,
                worker_id=worker_id,
                acquired_at=datetime.fromisoformat(str(row["acquired_at"])),
                heartbeat_at=current_time,
                expires_at=expires_at,
            )

    def release_worker_lease(self, run_id: str, worker_id: str) -> bool:
        with self._write() as connection:
            self._require_row(connection, run_id)
            cursor = connection.execute(
                "DELETE FROM worker_leases WHERE run_id = ? AND worker_id = ?",
                (run_id, worker_id),
            )
            return bool(cursor.rowcount)

    def _require_row(self, connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFoundError(f"run not found: {run_id}")
        return row

    def _append_event(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        event_type: str,
        phase: RunPhase,
        terminal_status: RunTerminalStatus | None,
        detail: dict[str, Any],
        occurred_at: datetime,
    ) -> None:
        sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM run_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO run_events (
                event_id, run_id, sequence, event_type, phase,
                terminal_status, occurred_at, detail_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                run_id,
                sequence,
                event_type,
                phase.value,
                terminal_status.value if terminal_status else None,
                _iso(occurred_at),
                json.dumps(detail, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            ),
        )

    @staticmethod
    def _record(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=str(row["run_id"]),
            spec=RunSpec.model_validate_json(str(row["spec_json"])),
            spec_sha256=str(row["spec_sha256"]),
            phase=RunPhase(row["phase"]),
            terminal_status=(
                RunTerminalStatus(row["terminal_status"])
                if row["terminal_status"]
                else None
            ),
            desired_terminal_status=(
                RunTerminalStatus(row["desired_terminal_status"])
                if row["desired_terminal_status"]
                else None
            ),
            abort_requested=bool(row["abort_requested"]),
            revision=int(row["revision"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            last_error=str(row["last_error"]) if row["last_error"] else None,
        )

    @staticmethod
    def _event(row: sqlite3.Row) -> RunEvent:
        return RunEvent(
            event_id=str(row["event_id"]),
            run_id=str(row["run_id"]),
            sequence=int(row["sequence"]),
            event_type=str(row["event_type"]),
            phase=RunPhase(row["phase"]),
            terminal_status=(
                RunTerminalStatus(row["terminal_status"])
                if row["terminal_status"]
                else None
            ),
            occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
            detail=json.loads(str(row["detail_json"])),
        )
