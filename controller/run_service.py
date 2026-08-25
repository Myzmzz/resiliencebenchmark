"""Control-plane facade that keeps SQLite state and evidence artifacts aligned."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .run_contracts import (
    MutationLease,
    RunEvent,
    RunPhase,
    RunRecord,
    RunSpec,
    RunTerminalStatus,
    WorkerLease,
)
from .run_store import RunStore

RUN_ID_RE = re.compile(r"^run-[0-9]{8}t[0-9]{6}z-[a-f0-9]{8}$")


class ArtifactJournal:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def sync(self, record: RunRecord, events: list[RunEvent]) -> None:
        if not RUN_ID_RE.fullmatch(record.run_id):
            raise ValueError("invalid generated run_id")
        run_dir = self.root / record.run_id
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        spec_payload = json.dumps(
            record.spec.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        spec_path = run_dir / "run-spec.json"
        if spec_path.exists() and spec_path.read_text(encoding="utf-8") != spec_payload:
            raise RuntimeError("immutable run-spec artifact differs from stored RunSpec")
        if not spec_path.exists():
            self._atomic_write(spec_path, spec_payload)

        state_payload = json.dumps(
            record.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        self._atomic_write(run_dir / "run-state.json", state_payload)
        event_payload = "".join(
            json.dumps(
                event.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
            for event in events
        )
        self._atomic_write(run_dir / "events.jsonl", event_payload)

    def write_json_artifact(
        self,
        run_id: str,
        artifact_ref: str,
        payload: Any,
    ) -> str:
        destination = self._resolve_artifact(run_id, artifact_ref)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        self._atomic_write(destination, content)
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def read_json_artifact(self, run_id: str, artifact_ref: str) -> Any | None:
        destination = self._resolve_artifact(run_id, artifact_ref)
        if not destination.is_file():
            return None
        return json.loads(destination.read_text(encoding="utf-8"))

    def _resolve_artifact(self, run_id: str, artifact_ref: str) -> Path:
        if not RUN_ID_RE.fullmatch(run_id):
            raise ValueError("invalid generated run_id")
        if not re.fullmatch(r"^[a-z0-9][a-z0-9._/-]{0,180}\.json$", artifact_ref):
            raise ValueError("artifact_ref must be a safe relative JSON path")
        relative = Path(artifact_ref)
        if ".." in relative.parts:
            raise ValueError("artifact_ref cannot traverse directories")
        run_dir = self.root / run_id
        destination = (run_dir / relative).resolve()
        try:
            destination.relative_to(run_dir.resolve())
        except ValueError as exc:
            raise ValueError("artifact_ref escaped the run directory") from exc
        return destination

    @staticmethod
    def _atomic_write(path: Path, payload: str) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)


class RunControlService:
    def __init__(self, store: RunStore, journal: ArtifactJournal):
        self.store = store
        self.journal = journal

    @classmethod
    def create(cls, *, database_path: Path, artifacts_root: Path) -> RunControlService:
        return cls(RunStore(database_path), ArtifactJournal(artifacts_root))

    def create_run(self, spec: RunSpec) -> RunRecord:
        now = datetime.now(UTC).strftime("%Y%m%dt%H%M%Sz")
        candidate_run_id = f"run-{now}-{uuid4().hex[:8]}"
        record, _created = self.store.create_or_get(candidate_run_id, spec)
        return self._sync(record)

    def get_run(self, run_id: str) -> RunRecord:
        return self.store.get(run_id)

    def list_runs(self, *, limit: int = 100) -> list[RunRecord]:
        return self.store.list_runs(limit=limit)

    def list_events(self, run_id: str, *, after_sequence: int = 0) -> list[RunEvent]:
        return self.store.list_events(run_id, after_sequence=after_sequence)

    def advance(
        self,
        run_id: str,
        next_phase: RunPhase,
        *,
        detail: dict[str, Any] | None = None,
    ) -> RunRecord:
        return self._sync(self.store.transition(run_id, next_phase, detail=detail))

    def request_cleanup(
        self,
        run_id: str,
        *,
        terminal_status: RunTerminalStatus,
        reason: str,
    ) -> RunRecord:
        return self._sync(
            self.store.request_cleanup(
                run_id,
                terminal_status=terminal_status,
                reason=reason,
            )
        )

    def request_abort(self, run_id: str, *, reason: str) -> RunRecord:
        return self._sync(
            self.store.request_cleanup(
                run_id,
                terminal_status=RunTerminalStatus.ABORTED,
                reason=reason,
                abort_requested=True,
            )
        )

    def approve_run(self, run_id: str) -> RunRecord:
        record = self.store.get(run_id)
        if record.phase is not RunPhase.AWAITING_APPROVAL:
            raise ValueError("run is not awaiting approval")
        return self.advance(
            run_id,
            RunPhase.BASELINING,
            detail={"approval": "operator-approved"},
        )

    def finish_cleanup(
        self,
        run_id: str,
        *,
        verified: bool,
        detail: dict[str, Any] | None = None,
    ) -> RunRecord:
        return self._sync(
            self.store.finish_cleanup(run_id, verified=verified, detail=detail)
        )

    def acquire_mutation_lease(
        self,
        run_id: str,
        *,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> MutationLease:
        lease = self.store.acquire_mutation_lease(
            run_id,
            ttl_seconds=ttl_seconds,
            now=now,
        )
        self._sync(self.store.get(run_id))
        return lease

    def release_mutation_lease(self, run_id: str) -> bool:
        released = self.store.release_mutation_lease(run_id)
        if released:
            self._sync(self.store.get(run_id))
        return released

    def claim_next_run(
        self,
        worker_id: str,
        *,
        phases: tuple[RunPhase, ...] | None = None,
        ttl_seconds: int = 900,
        now: datetime | None = None,
    ) -> WorkerLease | None:
        lease = self.store.claim_next_run(
            worker_id,
            phases=phases,
            ttl_seconds=ttl_seconds,
            now=now,
        )
        if lease is not None:
            self._sync(self.store.get(lease.run_id))
        return lease

    def renew_worker_lease(
        self,
        run_id: str,
        worker_id: str,
        *,
        ttl_seconds: int = 900,
        now: datetime | None = None,
    ) -> WorkerLease:
        return self.store.renew_worker_lease(
            run_id,
            worker_id,
            ttl_seconds=ttl_seconds,
            now=now,
        )

    def release_worker_lease(self, run_id: str, worker_id: str) -> bool:
        return self.store.release_worker_lease(run_id, worker_id)

    def record_json_artifact(
        self,
        run_id: str,
        *,
        artifact_ref: str,
        payload: Any,
        event_type: str,
    ) -> RunRecord:
        self.store.get(run_id)
        digest = self.journal.write_json_artifact(run_id, artifact_ref, payload)
        existing = next(
            (
                event
                for event in self.store.list_events(run_id)
                if event.event_type == event_type
                and event.detail.get("artifact_ref") == artifact_ref
                and event.detail.get("sha256") == digest
            ),
            None,
        )
        if existing is not None:
            return self._sync(self.store.get(run_id))
        record = self.store.append_event(
            run_id,
            event_type=event_type,
            detail={"artifact_ref": artifact_ref, "sha256": digest},
        )
        return self._sync(record)

    def read_json_artifact(self, run_id: str, artifact_ref: str) -> Any | None:
        self.store.get(run_id)
        return self.journal.read_json_artifact(run_id, artifact_ref)

    def _sync(self, record: RunRecord) -> RunRecord:
        self.journal.sync(record, self.store.list_events(record.run_id))
        return record
