"""SQLite-backed platform event ledger and notice queue for Stage-2 trials.

The ledger is deliberately platform-owned: it creates a private directory
(`0700`) and database/WAL files (`0600`), appends events in SQLite
transactions, and exposes no event update or delete API.  Notice delivery uses
an explicit lease plus acknowledgement model: claiming a notice makes it
temporarily unavailable to other workers, but it is not considered delivered
until the caller acknowledges the returned delivery id.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


DEFAULT_LEDGER_NAME = "platform-ledger.sqlite3"
_MAX_QUERY_LIMIT = 10_000


@dataclass(frozen=True)
class PlatformEvent:
    sequence: int
    trial_id: str
    event_type: str
    occurred_at: str
    recorded_at: str
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "trial_id": self.trial_id,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "recorded_at": self.recorded_at,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class Notice:
    notice_id: int
    trial_id: str
    notice_type: str
    enqueued_at: str
    payload: dict[str, Any]
    idempotency_key: str
    delivered_at: str | None
    claimed_until: str | None
    claimed_by: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "notice_id": self.notice_id,
            "trial_id": self.trial_id,
            "notice_type": self.notice_type,
            "enqueued_at": self.enqueued_at,
            "payload": dict(self.payload),
            "idempotency_key": self.idempotency_key,
            "delivered_at": self.delivered_at,
            "claimed_until": self.claimed_until,
            "claimed_by": self.claimed_by,
        }


@dataclass(frozen=True)
class NoticeDelivery:
    delivery_id: str
    notice: Notice
    claimed_at: str
    claim_deadline_at: str
    attempt: int
    claimed_by: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "notice": self.notice.as_dict(),
            "claimed_at": self.claimed_at,
            "claim_deadline_at": self.claim_deadline_at,
            "attempt": self.attempt,
            "claimed_by": self.claimed_by,
        }


class PlatformLedger:
    """Trial-scoped append-only platform evidence and notice delivery store."""

    def __init__(self, root: Path, *, database_name: str = DEFAULT_LEDGER_NAME):
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        if Path(database_name).name != database_name:
            raise ValueError("database_name must be a file name inside the ledger root")
        if not database_name.endswith((".sqlite", ".sqlite3", ".db")):
            raise ValueError("ledger database must use a sqlite/db suffix")
        self.path = (self.root / database_name).resolve()
        self.path.relative_to(self.root)
        self._initialize()

    def append(
        self,
        *,
        trial_id: str,
        event_type: str,
        occurred_at: str | datetime,
        payload: Mapping[str, Any] | None = None,
    ) -> PlatformEvent:
        """Append one platform event and return its assigned sequence."""

        trial_id = _required_text("trial_id", trial_id)
        event_type = _required_text("event_type", event_type)
        occurred = _timestamp_text(occurred_at)
        payload_text = _payload_json(payload or {})
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            recorded = _utc_now()
            cursor = connection.execute(
                """
                INSERT INTO platform_events
                    (trial_id, event_type, occurred_at, recorded_at, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (trial_id, event_type, occurred, recorded, payload_text),
            )
            sequence = int(cursor.lastrowid)
            connection.commit()
        self._chmod_sqlite_files()
        return PlatformEvent(
            sequence=sequence,
            trial_id=trial_id,
            event_type=event_type,
            occurred_at=occurred,
            recorded_at=recorded,
            payload=json.loads(payload_text),
        )

    def query(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 200,
        trial_id: str | None = None,
    ) -> list[PlatformEvent]:
        """Return events with sequence greater than ``after_sequence``."""

        after_sequence = _non_negative_int("after_sequence", after_sequence)
        limit = _limit(limit)
        filters: list[str] = ["sequence > ?"]
        args: list[Any] = [after_sequence]
        if trial_id is not None:
            filters.append("trial_id = ?")
            args.append(_required_text("trial_id", trial_id))
        sql = (
            "SELECT sequence, trial_id, event_type, occurred_at, recorded_at, payload_json "
            f"FROM platform_events WHERE {' AND '.join(filters)} "
            "ORDER BY sequence ASC LIMIT ?"
        )
        args.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, args).fetchall()
        return [_event_from_row(row) for row in rows]

    def query_dicts(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 200,
        trial_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            event.as_dict()
            for event in self.query(
                after_sequence=after_sequence,
                limit=limit,
                trial_id=trial_id,
            )
        ]

    def export_jsonl_rows(
        self,
        *,
        after_sequence: int = 0,
        limit: int = _MAX_QUERY_LIMIT,
        trial_id: str | None = None,
    ) -> Iterable[str]:
        """Yield event rows in the same JSON-object-per-line shape as exports."""

        for event in self.query(
            after_sequence=after_sequence,
            limit=limit,
            trial_id=trial_id,
        ):
            yield json.dumps(event.as_dict(), ensure_ascii=False, sort_keys=True)

    def enqueue_notice(
        self,
        *,
        trial_id: str,
        notice_type: str,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Notice:
        """Enqueue a notice once per ``(trial_id, idempotency_key)``."""

        trial_id = _required_text("trial_id", trial_id)
        notice_type = _required_text("notice_type", notice_type)
        idempotency_key = _required_text(
            "idempotency_key", idempotency_key or uuid4().hex
        )
        payload_text = _payload_json(payload or {})
        enqueued_at = _utc_now()
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT notice_id, trial_id, notice_type, enqueued_at, payload_json,
                       idempotency_key, delivered_at, claimed_until, claimed_by
                FROM notices
                WHERE trial_id = ? AND idempotency_key = ?
                """,
                (trial_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                notice = _notice_from_row(existing)
                if notice.notice_type != notice_type or notice.payload != json.loads(payload_text):
                    raise ValueError("idempotent notice replay changed notice content")
                return notice
            cursor = connection.execute(
                """
                INSERT INTO notices
                    (trial_id, notice_type, payload_json, idempotency_key, enqueued_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (trial_id, notice_type, payload_text, idempotency_key, enqueued_at),
            )
            notice_id = int(cursor.lastrowid)
            row = connection.execute(
                """
                SELECT notice_id, trial_id, notice_type, enqueued_at, payload_json,
                       idempotency_key, delivered_at, claimed_until, claimed_by
                FROM notices
                WHERE notice_id = ?
                """,
                (notice_id,),
            ).fetchone()
        self._chmod_sqlite_files()
        return _notice_from_row(row)

    def pending_notices(
        self,
        *,
        trial_id: str | None = None,
        include_claimed: bool = False,
        now: str | datetime | None = None,
        limit: int = 200,
    ) -> list[Notice]:
        """Return notices not yet acknowledged as delivered."""

        limit = _limit(limit)
        current = _timestamp_text(now or datetime.now(timezone.utc))
        filters = ["delivered_at IS NULL"]
        args: list[Any] = []
        if not include_claimed:
            filters.append("(claimed_until IS NULL OR claimed_until <= ?)")
            args.append(current)
        if trial_id is not None:
            filters.append("trial_id = ?")
            args.append(_required_text("trial_id", trial_id))
        sql = (
            "SELECT notice_id, trial_id, notice_type, enqueued_at, payload_json, "
            "idempotency_key, delivered_at, claimed_until, claimed_by "
            f"FROM notices WHERE {' AND '.join(filters)} "
            "ORDER BY notice_id ASC LIMIT ?"
        )
        args.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, args).fetchall()
        return [_notice_from_row(row) for row in rows]

    def claim_notice(
        self,
        *,
        trial_id: str | None = None,
        claimed_by: str | None = None,
        lease_seconds: int = 60,
        now: str | datetime | None = None,
    ) -> NoticeDelivery | None:
        """Claim the next undelivered notice without marking it delivered.

        If the caller loses the response or crashes before acknowledging the
        returned delivery id, the claim lease expires and the same notice can be
        claimed again with a new delivery id.
        """

        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        current_dt = _timestamp_datetime(now or datetime.now(timezone.utc))
        claimed_at = _timestamp_text(current_dt)
        deadline = _timestamp_text(current_dt + timedelta(seconds=lease_seconds))
        filters = [
            "delivered_at IS NULL",
            "(claimed_until IS NULL OR claimed_until <= ?)",
        ]
        args: list[Any] = [claimed_at]
        if trial_id is not None:
            filters.append("trial_id = ?")
            args.append(_required_text("trial_id", trial_id))
        claimed_by_text = str(claimed_by) if claimed_by is not None else None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT notice_id FROM notices "
                f"WHERE {' AND '.join(filters)} ORDER BY notice_id ASC LIMIT 1",
                args,
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            notice_id = int(row["notice_id"])
            attempt = (
                int(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM notice_deliveries WHERE notice_id = ?",
                        (notice_id,),
                    ).fetchone()["count"]
                )
                + 1
            )
            delivery_id = uuid4().hex
            connection.execute(
                """
                INSERT INTO notice_deliveries
                    (delivery_id, notice_id, claimed_at, claim_deadline_at,
                     attempt, claimed_by)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (delivery_id, notice_id, claimed_at, deadline, attempt, claimed_by_text),
            )
            connection.execute(
                """
                UPDATE notices
                SET claimed_until = ?, claimed_by = ?, latest_delivery_id = ?
                WHERE notice_id = ?
                """,
                (deadline, claimed_by_text, delivery_id, notice_id),
            )
            notice_row = connection.execute(
                """
                SELECT notice_id, trial_id, notice_type, enqueued_at, payload_json,
                       idempotency_key, delivered_at, claimed_until, claimed_by
                FROM notices
                WHERE notice_id = ?
                """,
                (notice_id,),
            ).fetchone()
            connection.execute("COMMIT")
        self._chmod_sqlite_files()
        return NoticeDelivery(
            delivery_id=delivery_id,
            notice=_notice_from_row(notice_row),
            claimed_at=claimed_at,
            claim_deadline_at=deadline,
            attempt=attempt,
            claimed_by=claimed_by_text,
        )

    def deliver_notice(
        self,
        *,
        delivery_id: str,
        trial_id: str | None = None,
        delivered_at: str | datetime | None = None,
        delivery_path: str = "poll",
    ) -> Notice:
        """Acknowledge a claimed notice as delivered, idempotently."""

        delivery_id = _required_text("delivery_id", delivery_id)
        timestamp = _timestamp_text(delivered_at or datetime.now(timezone.utc))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            delivery = connection.execute(
                """
                SELECT d.delivery_id, d.notice_id, d.delivered_at
                FROM notice_deliveries d JOIN notices n ON n.notice_id = d.notice_id
                WHERE d.delivery_id = ? AND (? IS NULL OR n.trial_id = ?)
                """,
                (delivery_id, trial_id, trial_id),
            ).fetchone()
            if delivery is None:
                connection.execute("ROLLBACK")
                raise KeyError(delivery_id)
            notice_id = int(delivery["notice_id"])
            if delivery["delivered_at"] is None:
                connection.execute(
                    "UPDATE notice_deliveries SET delivered_at = ? WHERE delivery_id = ?",
                    (timestamp, delivery_id),
                )
            existing = connection.execute(
                "SELECT delivered_at FROM notices WHERE notice_id = ?",
                (notice_id,),
            ).fetchone()
            if existing is None:
                connection.execute("ROLLBACK")
                raise KeyError(delivery_id)
            if existing["delivered_at"] is None:
                connection.execute(
                    "UPDATE notices SET delivered_at = ? WHERE notice_id = ?",
                    (timestamp, notice_id),
                )
                notice_type = connection.execute(
                    "SELECT notice_type, trial_id FROM notices WHERE notice_id = ?", (notice_id,),
                ).fetchone()
                connection.execute(
                    "INSERT INTO platform_events (trial_id,event_type,occurred_at,recorded_at,payload_json) VALUES (?,?,?,?,?)",
                    (notice_type["trial_id"], "NOTICE_DELIVERED", timestamp, _utc_now(), _payload_json({
                        "delivery_id": delivery_id, "notice_id": notice_id,
                        "notice_type": notice_type["notice_type"], "path": delivery_path,
                    })),
                )
            notice_row = connection.execute(
                """
                SELECT notice_id, trial_id, notice_type, enqueued_at, payload_json,
                       idempotency_key, delivered_at, claimed_until, claimed_by
                FROM notices
                WHERE notice_id = ?
                """,
                (notice_id,),
            ).fetchone()
            connection.execute("COMMIT")
        self._chmod_sqlite_files()
        return _notice_from_row(notice_row)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS platform_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    trial_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TRIGGER IF NOT EXISTS platform_events_no_update
                BEFORE UPDATE ON platform_events
                BEGIN
                    SELECT RAISE(ABORT, 'platform_events are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS platform_events_no_delete
                BEFORE DELETE ON platform_events
                BEGIN
                    SELECT RAISE(ABORT, 'platform_events are append-only');
                END;

                CREATE TABLE IF NOT EXISTS notices (
                    notice_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trial_id TEXT NOT NULL,
                    notice_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    enqueued_at TEXT NOT NULL,
                    claimed_until TEXT,
                    claimed_by TEXT,
                    latest_delivery_id TEXT,
                    delivered_at TEXT,
                    UNIQUE(trial_id, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS notice_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    notice_id INTEGER NOT NULL REFERENCES notices(notice_id),
                    claimed_at TEXT NOT NULL,
                    claim_deadline_at TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    claimed_by TEXT,
                    delivered_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_platform_events_trial_sequence
                    ON platform_events(trial_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_notices_pending
                    ON notices(delivered_at, claimed_until, notice_id);
                CREATE INDEX IF NOT EXISTS idx_notice_deliveries_notice
                    ON notice_deliveries(notice_id, attempt);
                """
            )
        self._chmod_sqlite_files()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=30000")
            try:
                connection.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
            connection.execute("PRAGMA synchronous=NORMAL")
            yield connection
        finally:
            connection.close()

    def _chmod_sqlite_files(self) -> None:
        # SQLite may remove WAL/SHM between a presence check and chmod when a
        # concurrent connection closes.  The main database is never optional.
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self.path}{suffix}")
            try:
                os.chmod(path, 0o600)
            except FileNotFoundError:
                if suffix:
                    continue
                raise


def _event_from_row(row: sqlite3.Row) -> PlatformEvent:
    return PlatformEvent(
        sequence=int(row["sequence"]),
        trial_id=str(row["trial_id"]),
        event_type=str(row["event_type"]),
        occurred_at=str(row["occurred_at"]),
        recorded_at=str(row["recorded_at"]),
        payload=json.loads(str(row["payload_json"])),
    )


def _notice_from_row(row: sqlite3.Row) -> Notice:
    return Notice(
        notice_id=int(row["notice_id"]),
        trial_id=str(row["trial_id"]),
        notice_type=str(row["notice_type"]),
        enqueued_at=str(row["enqueued_at"]),
        payload=json.loads(str(row["payload_json"])),
        idempotency_key=str(row["idempotency_key"]),
        delivered_at=row["delivered_at"],
        claimed_until=row["claimed_until"],
        claimed_by=row["claimed_by"],
    )


def _required_text(name: str, value: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _non_negative_int(name: str, value: int) -> int:
    integer = int(value)
    if integer < 0:
        raise ValueError(f"{name} must be non-negative")
    return integer


def _limit(value: int) -> int:
    limit = int(value)
    if limit < 1 or limit > _MAX_QUERY_LIMIT:
        raise ValueError(f"limit must be between 1 and {_MAX_QUERY_LIMIT}")
    return limit


def _payload_json(payload: Mapping[str, Any]) -> str:
    try:
        return json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)
    except TypeError as exc:
        raise ValueError("payload must be JSON serializable") from exc


def _timestamp_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return (
            value.astimezone(timezone.utc)
            if value.tzinfo is not None
            else value.replace(tzinfo=timezone.utc)
        )
    text = _required_text("timestamp", value)
    normalized = text.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    return (
        parsed.astimezone(timezone.utc)
        if parsed.tzinfo is not None
        else parsed.replace(tzinfo=timezone.utc)
    )


def _timestamp_text(value: str | datetime) -> str:
    return _timestamp_datetime(value).isoformat()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
