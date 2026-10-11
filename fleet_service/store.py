"""SQLite state for the Fleet: configuration, slots, batches, items, audit.

Batches and their assignments survive a restart, which is the only reason this
is not an in-memory dictionary: a round takes hours and the Fleet Pod may be
rescheduled inside it.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    document TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS slots (
    slot_id TEXT PRIMARY KEY,
    slot_index INTEGER NOT NULL UNIQUE,
    namespace TEXT NOT NULL UNIQUE,
    controller_url TEXT NOT NULL,
    phase TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    request TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    batch_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    resolved TEXT NOT NULL,
    state TEXT NOT NULL,
    slot_id TEXT,
    namespace TEXT,
    run_id TEXT,
    task_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    platform_retries INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    elapsed_seconds REAL,
    failure TEXT,
    score TEXT,
    PRIMARY KEY (batch_id, item_id)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    action TEXT NOT NULL,
    namespace TEXT,
    dry_run INTEGER NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class FleetStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = RLock()
        with self._connect() as connection:
            connection.executescript(SCHEMA)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = sqlite3.connect(self.path, timeout=30)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                yield connection
                connection.commit()
            finally:
                connection.close()

    # -- configuration -----------------------------------------------------
    def write_config(self, document: Mapping[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO fleet_config (id, document, updated_at) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET document=excluded.document, updated_at=excluded.updated_at",
                (json.dumps(dict(document), ensure_ascii=False, sort_keys=True), utc_now()),
            )

    def read_config(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT document FROM fleet_config WHERE id = 1").fetchone()
        return json.loads(row["document"]) if row else None

    # -- slots -------------------------------------------------------------
    def upsert_slot(
        self, *, slot_id: str, index: int, namespace: str, controller_url: str, phase: str,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO slots (slot_id, slot_index, namespace, controller_url, phase, detail, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(slot_id) DO UPDATE SET slot_index=excluded.slot_index, "
                "namespace=excluded.namespace, controller_url=excluded.controller_url, "
                "phase=excluded.phase, detail=excluded.detail, updated_at=excluded.updated_at",
                (slot_id, index, namespace, controller_url, phase,
                 json.dumps(dict(detail or {}), ensure_ascii=False, sort_keys=True), utc_now()),
            )

    def set_slot_phase(self, slot_id: str, phase: str, detail: Mapping[str, Any] | None = None) -> None:
        with self._connect() as connection:
            if detail is None:
                connection.execute(
                    "UPDATE slots SET phase = ?, updated_at = ? WHERE slot_id = ?",
                    (phase, utc_now(), slot_id),
                )
            else:
                connection.execute(
                    "UPDATE slots SET phase = ?, detail = ?, updated_at = ? WHERE slot_id = ?",
                    (phase, json.dumps(dict(detail), ensure_ascii=False, sort_keys=True), utc_now(), slot_id),
                )

    def slots(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM slots ORDER BY slot_index").fetchall()
        return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows]

    def slot(self, slot_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM slots WHERE slot_id = ?", (slot_id,)).fetchone()
        return {**dict(row), "detail": json.loads(row["detail"])} if row else None

    def slot_by_namespace(self, namespace: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM slots WHERE namespace = ?", (namespace,)).fetchone()
        return {**dict(row), "detail": json.loads(row["detail"])} if row else None

    def delete_slot(self, slot_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM slots WHERE slot_id = ?", (slot_id,))

    # -- batches -----------------------------------------------------------
    def create_batch(self, batch_id: str, request: Mapping[str, Any], items: list[Mapping[str, Any]]) -> None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO batches (batch_id, request, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (batch_id, json.dumps(dict(request), ensure_ascii=False, sort_keys=True), "Scheduled", now, now),
            )
            connection.executemany(
                "INSERT INTO items (batch_id, item_id, resolved, state, namespace) VALUES (?, ?, ?, ?, ?)",
                [
                    (batch_id, str(item["item_id"]),
                     json.dumps(dict(item), ensure_ascii=False, sort_keys=True),
                     "Queued", item.get("namespace"))
                    for item in items
                ],
            )

    def batch(self, batch_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["request"] = json.loads(value["request"])
        return value

    def batches(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT batch_id, state, created_at, updated_at FROM batches ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def set_batch_state(self, batch_id: str, state: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE batches SET state = ?, updated_at = ? WHERE batch_id = ?",
                (state, utc_now(), batch_id),
            )

    def items(self, batch_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM items WHERE batch_id = ? ORDER BY item_id", (batch_id,)
            ).fetchall()
        return [self._item(row) for row in rows]

    def item(self, batch_id: str, item_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM items WHERE batch_id = ? AND item_id = ?", (batch_id, item_id)
            ).fetchone()
        return self._item(row) if row else None

    @staticmethod
    def _item(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["resolved"] = json.loads(value["resolved"])
        for key in ("failure", "score"):
            value[key] = json.loads(value[key]) if value[key] else None
        return value

    def update_item(self, batch_id: str, item_id: str, **updates: Any) -> None:
        if not updates:
            return
        encoded = {
            key: (json.dumps(value, ensure_ascii=False, sort_keys=True)
                  if key in {"failure", "score", "resolved"} and value is not None else value)
            for key, value in updates.items()
        }
        assignments = ", ".join(f"{key} = ?" for key in encoded)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE items SET {assignments} WHERE batch_id = ? AND item_id = ?",
                (*encoded.values(), batch_id, item_id),
            )

    def claim_slot_for_item(self, batch_id: str, item_id: str, slot_id: str, namespace: str) -> bool:
        """Assign one queued item to one idle slot, atomically."""
        with self._connect() as connection:
            busy = connection.execute(
                "SELECT COUNT(*) AS n FROM items WHERE slot_id = ? AND state IN ('Assigned', 'Running')",
                (slot_id,),
            ).fetchone()["n"]
            if busy:
                return False
            changed = connection.execute(
                "UPDATE items SET state = 'Assigned', slot_id = ?, namespace = ? "
                "WHERE batch_id = ? AND item_id = ? AND state = 'Queued'",
                (slot_id, namespace, batch_id, item_id),
            ).rowcount
            return changed == 1

    def running_count(self) -> int:
        with self._connect() as connection:
            return connection.execute(
                "SELECT COUNT(*) AS n FROM items WHERE state IN ('Assigned', 'Running')"
            ).fetchone()["n"]

    def busy_slot_ids(self) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT slot_id FROM items WHERE state IN ('Assigned', 'Running') AND slot_id IS NOT NULL"
            ).fetchall()
        return {row["slot_id"] for row in rows}

    # -- audit -------------------------------------------------------------
    def audit_event(
        self, *, action: str, namespace: str | None, dry_run: bool, actor: str,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit (occurred_at, action, namespace, dry_run, actor, detail) VALUES (?, ?, ?, ?, ?, ?)",
                (utc_now(), action, namespace, 1 if dry_run else 0, actor,
                 json.dumps(dict(detail or {}), ensure_ascii=False, sort_keys=True)),
            )

    def audit_log(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [{**dict(row), "detail": json.loads(row["detail"]), "dry_run": bool(row["dry_run"])} for row in rows]
