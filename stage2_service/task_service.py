"""Simple, persistent single-Agent Stage2 task facade for API clients."""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Mapping, Protocol
from uuid import uuid4

from pydantic import Field

from .contracts import (
    AgentVerdict,
    CampaignRequest,
    CampaignResult,
    CaseBundle,
    ContractModel,
    HarnessKind,
    Stage2CaseId,
    default_case_specs,
)
from .matrix import fixed_otel_episode_ref


TASK_CASES = (
    Stage2CaseId.C0,
    Stage2CaseId.D1,
    Stage2CaseId.D2,
    Stage2CaseId.D3,
    Stage2CaseId.D4,
)
TASK_ID = re.compile(r"^stage2-task-[a-f0-9]{16}$")
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
CONTROL_STATES = {"REQUESTED", "RUNNING"}


class Stage2TaskCreateRequest(ContractModel):
    schema_version: Literal["stage2-task-create.v1"] = "stage2-task-create.v1"
    application: Literal["otel-demo"]
    prompt: str = Field(min_length=1, max_length=12000)
    model: Literal["gpt-5.6-sol", "claude-opus-5"]
    harness: HarnessKind


class PermissionRestoreRequest(ContractModel):
    target_state: Literal["BASELINE", "REVOKED"] = "BASELINE"
    reason: str = Field(default="operator requested", min_length=1, max_length=500)


class AbortTaskRequest(ContractModel):
    reason: str = Field(default="operator requested", min_length=1, max_length=500)
    recovery_mode: Literal["FULL_RESET"] = "FULL_RESET"


class EnvironmentResetRequest(ContractModel):
    reason: str = Field(default="operator requested", min_length=1, max_length=500)
    restart_sut: Literal[True] = True


class TaskSupervisor(Protocol):
    def submit(self, request: CampaignRequest, *, event_sink=None, result_sink=None) -> str: ...

    def has(self, request_id: str) -> bool: ...

    def get(self, request_id: str) -> dict: ...

    def list_runs(self) -> list[dict]: ...

    def request_stop(self, request_id: str) -> dict: ...

    def wait_result(self, request_id: str, timeout: float | None = None) -> CampaignResult: ...


class TaskControlBackend(Protocol):
    def reset_environment(self, operation_id: str, application: str) -> Mapping[str, Any]: ...

    def restore_permissions(
        self, task_id: str, trial_id: str | None, target_state: str
    ) -> Mapping[str, Any]: ...


class TaskNotFound(LookupError):
    pass


class TaskConflict(RuntimeError):
    def __init__(self, message: str, *, active_task_id: str | None = None):
        super().__init__(message)
        self.active_task_id = active_task_id


class TaskValidationError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class TaskStore:
    def __init__(self, artifact_root: Path):
        self.root = (artifact_root.resolve() / "tasks")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.idempotency_root = self.root / "idempotency"
        self.idempotency_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = Lock()

    def create(
        self,
        task_id: str,
        request: Mapping[str, Any],
        campaign_request: Mapping[str, Any],
        *,
        idempotency_key: str | None,
    ) -> None:
        root = self.task_root(task_id, require=False)
        root.mkdir(mode=0o700, parents=False, exist_ok=False)
        self._write_json(root / "request.json", dict(request))
        self._write_json(root / "campaign-request.json", dict(campaign_request))
        self._write_json(
            root / "status.json",
            {
                "schema_version": "stage2-task-state.v1",
                "task_id": task_id,
                "task_status": "QUEUED",
                "current_phase": "QUEUED",
                "terminal": False,
                "created_at": utc_now(),
                "started_at": None,
                "updated_at": utc_now(),
                "finished_at": None,
                "campaign_id": None,
                "current_trial_id": None,
                "current_case": None,
                "platform_status": None,
                "control_actions": {},
            },
        )
        (root / "events.jsonl").touch(mode=0o600)
        if idempotency_key:
            self._write_json(
                self.idempotency_root / f"{idempotency_key}.json",
                {"task_id": task_id},
            )

    def find_idempotent(self, key: str | None) -> str | None:
        if not key:
            return None
        if not IDEMPOTENCY_KEY.fullmatch(key):
            raise TaskValidationError("Idempotency-Key has an invalid format")
        path = self.idempotency_root / f"{key}.json"
        if not path.is_file():
            return None
        value = self._read_json(path)
        task_id = str(value.get("task_id") or "")
        return task_id if TASK_ID.fullmatch(task_id) else None

    def task_root(self, task_id: str, *, require: bool = True) -> Path:
        if not TASK_ID.fullmatch(task_id):
            raise TaskNotFound(task_id)
        root = (self.root / task_id).resolve()
        root.relative_to(self.root.resolve())
        if require and not root.is_dir():
            raise TaskNotFound(task_id)
        return root

    def request(self, task_id: str) -> dict[str, Any]:
        return self._read_json(self.task_root(task_id) / "request.json")

    def campaign_request(self, task_id: str) -> dict[str, Any]:
        return self._read_json(self.task_root(task_id) / "campaign-request.json")

    def status(self, task_id: str) -> dict[str, Any]:
        return self._read_json(self.task_root(task_id) / "status.json")

    def update_status(self, task_id: str, **updates: Any) -> dict[str, Any]:
        with self.lock:
            path = self.task_root(task_id) / "status.json"
            state = self._read_json(path)
            state.update(updates)
            state["updated_at"] = utc_now()
            self._write_json(path, state)
            return state

    def append_event(self, task_id: str, event: Mapping[str, Any]) -> dict[str, Any]:
        with self.lock:
            root = self.task_root(task_id)
            path = root / "events.jsonl"
            sequence = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
            normalized = {"sequence": sequence, **dict(event)}
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(normalized, ensure_ascii=False, sort_keys=True) + "\n")
            return normalized

    def events(self, task_id: str) -> list[dict[str, Any]]:
        path = self.task_root(task_id) / "events.jsonl"
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def write_result(self, task_id: str, result: Mapping[str, Any]) -> None:
        self._write_json(self.task_root(task_id) / "result.json", dict(result))

    def result(self, task_id: str) -> dict[str, Any] | None:
        path = self.task_root(task_id) / "result.json"
        return self._read_json(path) if path.is_file() else None

    def has_unresolved_recovery(self) -> str | None:
        for path in self.root.glob("stage2-task-*/status.json"):
            state = self._read_json(path)
            if state.get("task_status") == "RECOVERY_FAILED":
                return str(state.get("task_id") or "")
            if any(
                isinstance(value, Mapping)
                and value.get("state") in {"REQUESTED", "RUNNING", "PARTIAL", "FAILED"}
                for value in (state.get("control_actions") or {}).values()
            ):
                return str(state.get("task_id") or "")
        return None

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"expected JSON object: {path}")
        return value

    @staticmethod
    def _write_json(path: Path, value: Mapping[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(
            json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)


class Stage2TaskService:
    def __init__(
        self,
        *,
        supervisor: TaskSupervisor,
        artifact_root: Path,
        repo_root: Path,
        preflight_provider,
        control_backend: TaskControlBackend,
    ):
        self.supervisor = supervisor
        self.artifact_root = artifact_root.resolve()
        self.repo_root = repo_root.resolve()
        self.preflight_provider = preflight_provider
        self.control_backend = control_backend
        self.store = TaskStore(self.artifact_root)
        self.control_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stage2-control")

    def create(
        self, request: Stage2TaskCreateRequest, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        existing = self.store.find_idempotent(idempotency_key)
        if existing:
            return self.created_response(existing)
        running_control = self.store.has_unresolved_recovery()
        if running_control:
            raise TaskConflict(
                "a Stage2 recovery action is active", active_task_id=running_control
            )
        active = next(
            (item for item in self.supervisor.list_runs() if item.get("status") == "RUNNING"),
            None,
        )
        if active:
            raise TaskConflict(
                "another Stage2 task is active",
                active_task_id=str(active.get("request_id") or ""),
            )
        preflight = dict(self.preflight_provider())
        available = (
            preflight.get("model_matrix", {})
            .get(request.harness.value, {})
            .get(request.model)
        )
        if available is not True:
            raise TaskValidationError(
                f"model/Harness combination is unavailable: {request.harness.value}/{request.model}"
            )
        task_id = f"stage2-task-{uuid4().hex[:16]}"
        specs = default_case_specs(TASK_CASES)
        campaign_request = CampaignRequest(
            request_id=task_id,
            episode=fixed_otel_episode_ref(self.repo_root),
            harnesses=(request.harness,),
            model_by_harness={request.harness: request.model},
            qualification_mode="diagnostic",
            case_bundle=CaseBundle(
                bundle_id=task_id,
                base_prompt=request.prompt,
                cases=specs,
            ),
            cases=TASK_CASES,
        )
        self.store.create(
            task_id,
            request.model_dump(mode="json"),
            campaign_request.model_dump(mode="json"),
            idempotency_key=idempotency_key,
        )
        self.store.append_event(
            task_id,
            self._control_event(
                actor="HARNESS",
                event_type="TASK_ACCEPTED",
                summary="single-Agent Stage2 task accepted",
                payload={
                    "application": request.application,
                    "model": request.model,
                    "harness": request.harness.value,
                    "cases": [item.value for item in TASK_CASES],
                },
            ),
        )
        try:
            self.supervisor.submit(
                campaign_request,
                event_sink=lambda event: self._on_campaign_event(task_id, event),
                result_sink=lambda result: self._on_campaign_result(task_id, result),
            )
        except RuntimeError as exc:
            self.store.update_status(
                task_id,
                task_status="FAILED",
                terminal=True,
                current_phase="REJECTED",
                finished_at=utc_now(),
                error=str(exc),
            )
            raise TaskConflict(str(exc)) from exc
        return self.created_response(task_id)

    def created_response(self, task_id: str) -> dict[str, Any]:
        state = self.store.status(task_id)
        request = self.store.request(task_id)
        return {
            "schema_version": "stage2-task-created.v1",
            "task_id": task_id,
            "task_status": state["task_status"],
            "application": request["application"],
            "model": request["model"],
            "harness": request["harness"],
            "created_at": state["created_at"],
            "poll_after_ms": 2000,
            "links": {"self": f"/api/v1/stage2/tasks/{task_id}"},
        }

    def get(
        self,
        task_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 200,
        include_raw: bool = False,
    ) -> dict[str, Any]:
        state = self.store.status(task_id)
        request = self.store.request(task_id)
        all_events = self.store.events(task_id)
        if state.get("task_status") in {"QUEUED", "RUNNING", "FINALIZING"} and not self.supervisor.has(task_id):
            state = self.store.update_status(
                task_id,
                task_status="INTERRUPTED",
                current_phase="RECOVERY_REQUIRED",
                terminal=True,
                finished_at=utc_now(),
            )
        selected_events = [
            item for item in all_events if int(item.get("sequence", -1)) >= after_sequence
        ][:limit]
        trials = self._trials(state, request, all_events, include_raw=include_raw)
        return {
            "schema_version": "stage2-task-status.v1",
            **state,
            "elapsed_seconds": self._elapsed(state),
            "input": {
                "application": request["application"],
                "prompt": request["prompt"],
                "model": request["model"],
                "harness": request["harness"],
                "cases": [item.value for item in TASK_CASES],
            },
            "suite": self._suite(state, all_events),
            "trials": trials,
            "result": self._task_result(task_id),
            "issues": self._issues(state, all_events, trials),
            "events": selected_events,
            "next_sequence": (
                int(selected_events[-1]["sequence"]) + 1
                if selected_events
                else after_sequence
            ),
        }

    def abort(self, task_id: str, request: AbortTaskRequest) -> dict[str, Any]:
        return self._start_control(
            task_id,
            action="abort",
            reason=request.reason,
            worker=lambda: self._abort_worker(task_id),
        )

    def reset_environment(
        self, task_id: str, request: EnvironmentResetRequest
    ) -> dict[str, Any]:
        return self._start_control(
            task_id,
            action="environment_reset",
            reason=request.reason,
            worker=lambda: self._reset_worker(task_id),
        )

    def restore_permissions(
        self, task_id: str, request: PermissionRestoreRequest
    ) -> dict[str, Any]:
        return self._start_control(
            task_id,
            action="permission_restore",
            reason=request.reason,
            worker=lambda: self.control_backend.restore_permissions(
                task_id,
                self.store.status(task_id).get("current_trial_id"),
                request.target_state,
            ),
            details={"target_state": request.target_state},
        )

    def _start_control(
        self,
        task_id: str,
        *,
        action: str,
        reason: str,
        worker,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self.store.status(task_id)
        actions = dict(state.get("control_actions") or {})
        active_other = next(
            (
                name
                for name, value in actions.items()
                if name != action
                and isinstance(value, Mapping)
                and value.get("state") in CONTROL_STATES
            ),
            None,
        )
        if active_other:
            raise TaskConflict(
                f"control action is already active: {active_other}",
                active_task_id=task_id,
            )
        existing = actions.get(action)
        if isinstance(existing, Mapping):
            same_details = all(existing.get(key) == value for key, value in (details or {}).items())
            if existing.get("state") in CONTROL_STATES or (
                existing.get("state") == "SUCCEEDED" and same_details
            ):
                return {"task_id": task_id, "action": action, **dict(existing)}
        action_state = {
            "state": "REQUESTED",
            "reason": reason,
            "requested_at": utc_now(),
            "started_at": None,
            "finished_at": None,
            **dict(details or {}),
        }
        actions[action] = action_state
        self.store.update_status(
            task_id,
            task_status="ABORTING" if action in {"abort", "environment_reset"} else state["task_status"],
            current_phase=action.upper(),
            control_actions=actions,
        )
        self.store.append_event(
            task_id,
            self._control_event(
                actor="CONTROLLER",
                event_type=f"{action.upper()}_REQUESTED",
                summary=f"{action} requested",
                payload={"reason": reason, **dict(details or {})},
            ),
        )
        self.control_pool.submit(self._run_control, task_id, action, worker)
        return {"task_id": task_id, "action": action, **action_state}

    def _run_control(self, task_id: str, action: str, worker) -> None:
        self._update_control(task_id, action, state="RUNNING", started_at=utc_now())
        self.store.append_event(
            task_id,
            self._control_event(
                actor="CONTROLLER",
                event_type=f"{action.upper()}_STARTED",
                summary=f"{action} started",
            ),
        )
        try:
            result = dict(worker())
            succeeded = result.get("verified") is True
            final_state = "SUCCEEDED" if succeeded else "PARTIAL"
            self._update_control(
                task_id,
                action,
                state=final_state,
                finished_at=utc_now(),
                result=result,
            )
        except Exception as exc:  # noqa: BLE001 - action status must be queryable.
            final_state = "FAILED"
            self._update_control(
                task_id,
                action,
                state=final_state,
                finished_at=utc_now(),
                error=f"{type(exc).__name__}: {str(exc)[:800]}",
            )
        self.store.append_event(
            task_id,
            self._control_event(
                actor="CONTROLLER",
                event_type=f"{action.upper()}_{final_state}",
                summary=f"{action} finished with {final_state}",
            ),
        )

    def _abort_worker(self, task_id: str) -> Mapping[str, Any]:
        stop = self._stop_and_wait(task_id)
        trial_id = self.store.status(task_id).get("current_trial_id")
        permissions = dict(
            self.control_backend.restore_permissions(
                task_id, trial_id, "REVOKED"
            )
        )
        reset = dict(self.control_backend.reset_environment(task_id, "otel-demo"))
        verified = permissions.get("verified") is True and reset.get("verified") is True
        self.store.update_status(
            task_id,
            task_status="ABORTED" if verified else "RECOVERY_FAILED",
            current_phase="DONE" if verified else "RECOVERY_FAILED",
            terminal=True,
            finished_at=utc_now(),
        )
        return {
            "verified": verified,
            "stop": stop,
            "permission_restore": permissions,
            "environment_reset": reset,
        }

    def _reset_worker(self, task_id: str) -> Mapping[str, Any]:
        stop = self._stop_and_wait(task_id)
        trial_id = self.store.status(task_id).get("current_trial_id")
        permissions = dict(
            self.control_backend.restore_permissions(
                task_id, trial_id, "REVOKED"
            )
        )
        reset = dict(self.control_backend.reset_environment(task_id, "otel-demo"))
        verified = permissions.get("verified") is True and reset.get("verified") is True
        self.store.update_status(
            task_id,
            task_status="ABORTED" if verified else "RECOVERY_FAILED",
            current_phase="DONE" if verified else "RECOVERY_FAILED",
            terminal=True,
            finished_at=utc_now(),
        )
        return {
            "verified": verified,
            "stop": stop,
            "permission_restore": permissions,
            "environment_reset": reset,
        }

    def _stop_and_wait(self, task_id: str) -> Mapping[str, Any]:
        if not self.supervisor.has(task_id):
            return {"requested": False, "already_terminal": True}
        stop = self.supervisor.request_stop(task_id)
        try:
            result = self.supervisor.wait_result(task_id, timeout=900)
            return {
                "requested": True,
                "campaign_id": result.campaign_id,
                "platform_status": result.platform_status.value,
            }
        except FutureTimeout:
            return {"requested": True, "timed_out": True}

    def _update_control(self, task_id: str, action: str, **updates: Any) -> None:
        state = self.store.status(task_id)
        actions = dict(state.get("control_actions") or {})
        value = dict(actions.get(action) or {})
        value.update(updates)
        actions[action] = value
        self.store.update_status(task_id, control_actions=actions)

    def _on_campaign_event(self, task_id: str, event: Mapping[str, Any]) -> None:
        normalized = self._normalize_campaign_event(event)
        self.store.append_event(task_id, normalized)
        updates: dict[str, Any] = {"task_status": "RUNNING", "terminal": False}
        kind = str(event.get("kind") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        if event.get("campaign_id"):
            updates["campaign_id"] = event["campaign_id"]
        if kind == "campaign_started":
            updates.update(started_at=event.get("occurred_at") or utc_now(), current_phase="PREFLIGHT")
        elif kind == "trial_started":
            updates.update(
                current_trial_id=payload.get("trial_id"),
                current_case=payload.get("case_id"),
                current_phase="AGENT_RUNNING",
            )
        elif kind == "lifecycle_event":
            event_kind = str(payload.get("event_kind") or "")
            if event_kind == "main_fault_running":
                updates["current_phase"] = "FAULT_RUNNING"
            elif event_kind in {"recovery_requested", "recovery_accepted"}:
                updates["current_phase"] = "RECOVERING"
        elif kind == "trial_finished":
            updates["current_phase"] = "FINALIZING"
        self.store.update_status(task_id, **updates)

    def _on_campaign_result(self, task_id: str, result: CampaignResult) -> None:
        self.store.write_result(task_id, result.model_dump(mode="json"))
        current = self.store.status(task_id)
        if current.get("task_status") not in {"ABORTING", "ABORTED", "RECOVERY_FAILED"}:
            self.store.update_status(
                task_id,
                task_status="COMPLETED",
                current_phase="DONE",
                terminal=True,
                campaign_id=result.campaign_id,
                platform_status=result.platform_status.value,
                finished_at=result.finished_at.isoformat(),
            )

    @staticmethod
    def _normalize_campaign_event(event: Mapping[str, Any]) -> dict[str, Any]:
        kind = str(event.get("kind") or "event")
        payload = dict(event.get("payload") or {})
        actor = {
            "campaign_started": "HARNESS",
            "d0_qualification_checked": "CONTROLLER",
            "environment_qualified": "ENVIRONMENT",
            "trial_started": "HARNESS",
            "interaction_event": "HARNESS",
            "agent_response_captured": "HARNESS",
            "lifecycle_event": "AGENT",
            "disturbance_status": "CONTROLLER",
            "disturbance_applied": "CONTROLLER",
            "trial_finished": "EVALUATOR",
            "campaign_finished": "HARNESS",
        }.get(kind, "HARNESS")
        if kind == "interaction_event":
            actor = str(payload.get("actor") or actor)
            event_type = str(payload.get("event_type") or kind).upper()
            peer = payload.get("peer")
            event_payload = payload.get("payload") or {}
        else:
            event_type = str(payload.get("event_kind") or kind).upper()
            peer = "HARNESS" if actor == "AGENT" else None
            event_payload = payload
        return {
            "occurred_at": event.get("occurred_at") or utc_now(),
            "actor": actor,
            "peer": peer,
            "phase": payload.get("phase"),
            "event_type": event_type,
            "trial_id": payload.get("trial_id"),
            "case_id": payload.get("case_id"),
            "correlation_id": payload.get("trigger_event_id"),
            "summary": event_type.replace("_", " ").lower(),
            "payload": event_payload,
            "artifact_ref": None,
        }

    @staticmethod
    def _control_event(
        *,
        actor: str,
        event_type: str,
        summary: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "occurred_at": utc_now(),
            "actor": actor,
            "peer": None,
            "phase": None,
            "event_type": event_type,
            "trial_id": None,
            "case_id": None,
            "correlation_id": None,
            "summary": summary,
            "payload": dict(payload or {}),
            "artifact_ref": None,
        }

    def _suite(self, state: Mapping[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        completed = {
            item.get("case_id")
            for item in events
            if item.get("event_type") == "TRIAL_FINISHED"
        }
        return {
            "cases": [item.value for item in TASK_CASES],
            "current_case": state.get("current_case"),
            "completed_trials": len(completed),
            "total_trials": len(TASK_CASES),
        }

    def _trials(
        self,
        state: Mapping[str, Any],
        request: Mapping[str, Any],
        events: list[dict[str, Any]],
        *,
        include_raw: bool,
    ) -> list[dict[str, Any]]:
        campaign_id = state.get("campaign_id")
        output = []
        for case in TASK_CASES:
            trial_events = [item for item in events if item.get("case_id") == case.value]
            started = next(
                (item for item in trial_events if item.get("event_type") == "TRIAL_STARTED"),
                None,
            )
            trial_id = (started or {}).get("trial_id") or (started or {}).get("payload", {}).get("trial_id")
            finished = next(
                (item for item in trial_events if item.get("event_type") == "TRIAL_FINISHED"),
                None,
            )
            trial_status = "COMPLETED" if finished else "RUNNING" if trial_id else "PENDING"
            output.append(
                self._trial_detail(
                    request=request,
                    campaign_id=str(campaign_id or ""),
                    trial_id=str(trial_id or ""),
                    case_id=case.value,
                    trial_status=trial_status,
                    events=trial_events,
                    include_raw=include_raw,
                )
            )
        return output

    def _trial_detail(
        self,
        *,
        request: Mapping[str, Any],
        campaign_id: str,
        trial_id: str,
        case_id: str,
        trial_status: str,
        events: list[dict[str, Any]],
        include_raw: bool,
    ) -> dict[str, Any]:
        croot = self.artifact_root / campaign_id if campaign_id else None
        troot = croot / "trials" / trial_id if croot and trial_id else None
        oroot = croot / trial_id if croot and trial_id else None
        runtime = self._read_optional(troot / "runtime-context.json") if troot else {}
        recovery = self._read_optional(troot / "recovery.json") if troot else {}
        attempt = self._read_optional(troot / "disturbance-attempt.json") if troot else {}
        report = self._read_optional(troot / "harness-report.json") if troot else {}
        decision = self._read_optional(troot / "evaluation-decision.json") if troot else {}
        result = self._read_optional(troot / "result.json") if troot else {}
        prompt_path = oroot / "prompt.redacted.txt" if oroot else None
        stdout_path = oroot / "stdout.txt" if oroot else None
        stderr_path = oroot / "stderr.txt" if oroot else None
        prompt_text = self._read_text(prompt_path, include_raw)
        stdout = self._read_text(stdout_path, include_raw)
        stderr = self._read_text(stderr_path, include_raw)
        main_fault = self._main_fault(events, recovery, runtime)
        disturbance = attempt or {
            "required": case_id.startswith("D"),
            "state": (
                "NOT_APPLICABLE"
                if not case_id.startswith("D")
                else "NOT_TRIGGERED"
                if trial_status == "COMPLETED"
                else "WAITING_TRIGGER"
            ),
        }
        final_output = report.get("final_output") if isinstance(report.get("final_output"), Mapping) else {}
        return {
            "trial_id": trial_id or None,
            "case_id": case_id,
            "status": trial_status,
            "agent_input": {
                "user_prompt": request["prompt"],
                "compiled_prompt_redacted": prompt_text,
            },
            "main_fault": main_fault,
            "disturbance": disturbance,
            "agent_response": {
                "stdout": stdout,
                "stderr": stderr,
                "structured": final_output.get("agent_result"),
                "output_validation": final_output.get("validation_error"),
            },
            "harness": {
                "status": report.get("status"),
                "process_succeeded": final_output.get("process_succeeded"),
                "lifecycle_event_count": len(report.get("lifecycle_events") or []),
            },
            "evaluation": decision or {
                "verdict": result.get("agent_verdict"),
                "platform_valid": result.get("platform_valid"),
                "checks": [],
            },
            "recovery": recovery,
            "runtime_target": runtime.get("target"),
            "events": events,
            "artifact_refs": result.get("artifact_refs") or [],
        }

    @staticmethod
    def _main_fault(
        events: list[dict[str, Any]],
        recovery: Mapping[str, Any],
        runtime: Mapping[str, Any],
    ) -> dict[str, Any]:
        kinds = {item.get("event_type") for item in events}
        requested = "MAIN_FAULT_REQUESTED" in kinds
        injected = recovery.get("main_fault_ever_active") is True or "MAIN_FAULT_RUNNING" in kinds
        recovered = recovery.get("fault_absent") is True and recovery.get("controller_cleanup_verified") is True
        summary_state = "RECOVERED" if recovered and injected else "INJECTED" if injected else "NOT_INJECTED"
        detail_state = (
            "RECOVERED"
            if recovered and injected
            else "EFFECT_VERIFIED"
            if recovery.get("fault_effect_verified") is True
            else "ACTIVE"
            if injected
            else "REQUESTED"
            if requested
            else "NOT_REQUESTED"
        )
        return {
            "summary_state": summary_state,
            "state": detail_state,
            "requested": requested,
            "injected": injected,
            "target_verified": recovery.get("main_fault_target_verified"),
            "effect_verified": recovery.get("fault_effect_verified"),
            "recovered": recovered,
            "contract": runtime.get("main_fault"),
        }

    def _task_result(self, task_id: str) -> dict[str, Any] | None:
        result = self.store.result(task_id)
        if result is None:
            return None
        trials = result.get("trials") or []
        verdicts: dict[str, int] = {}
        for trial in trials:
            value = str(trial.get("agent_verdict") or "")
            verdicts[value] = verdicts.get(value, 0) + 1
        return {
            "platform_status": result.get("platform_status"),
            "campaign_id": result.get("campaign_id"),
            "trial_count": len(trials),
            "verdict_counts": verdicts,
        }

    @staticmethod
    def _issues(
        state: Mapping[str, Any],
        events: list[dict[str, Any]],
        trials: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        issues = []
        if state.get("task_status") == "INTERRUPTED":
            issues.append(
                {
                    "owner": "HARNESS",
                    "code": "TASK_RUNTIME_INTERRUPTED",
                    "severity": "ERROR",
                    "message": "task runtime is no longer attached to the service process",
                    "evidence_sequences": [],
                }
            )
        for event in events:
            if event.get("event_type") == "PERMISSION_DENIED":
                issues.append(
                    {
                        "owner": "AGENT",
                        "code": "PERMISSION_DENIED_OBSERVED",
                        "severity": "INFO",
                        "message": "Agent observed a permission denial",
                        "evidence_sequences": [event.get("sequence")],
                    }
                )
        for trial in trials:
            trial_id = trial.get("trial_id")
            validation = trial.get("agent_response", {}).get("output_validation")
            if validation:
                issues.append(
                    {
                        "owner": "HARNESS",
                        "code": "OUTPUT_SCHEMA_MISMATCH",
                        "severity": "ERROR",
                        "message": str(validation),
                        "trial_id": trial_id,
                        "evidence_sequences": [],
                    }
                )
            disturbance_state = trial.get("disturbance", {}).get("state")
            if disturbance_state == "NOT_TRIGGERED":
                issues.append(
                    {
                        "owner": "HARNESS",
                        "code": "DISTURBANCE_TRIGGER_NOT_OBSERVED",
                        "severity": "ERROR",
                        "message": "the expected disturbance trigger was not observed",
                        "trial_id": trial_id,
                        "evidence_sequences": [],
                    }
                )
            elif disturbance_state in {"APPLICATION_FAILED", "ROLLBACK_FAILED"}:
                issues.append(
                    {
                        "owner": "CONTROLLER",
                        "code": disturbance_state,
                        "severity": "ERROR",
                        "message": f"disturbance ended in {disturbance_state}",
                        "trial_id": trial_id,
                        "evidence_sequences": [],
                    }
                )
            failed_checks = [
                item.get("rule_id")
                for item in trial.get("evaluation", {}).get("checks", [])
                if item.get("passed") is False
            ]
            if failed_checks and trial.get("evaluation", {}).get("platform_valid") is True:
                issues.append(
                    {
                        "owner": "AGENT",
                        "code": "EXPECTED_BEHAVIOR_NOT_MET",
                        "severity": "ERROR",
                        "message": ", ".join(str(item) for item in failed_checks),
                        "trial_id": trial_id,
                        "evidence_sequences": [],
                    }
                )
        return issues

    @staticmethod
    def _elapsed(state: Mapping[str, Any]) -> int:
        start = state.get("started_at") or state.get("created_at")
        finish = state.get("finished_at") or utc_now()
        try:
            left = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            right = datetime.fromisoformat(str(finish).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return 0
        return max(int((right - left).total_seconds()), 0)

    @staticmethod
    def _read_optional(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _read_text(path: Path | None, include_raw: bool) -> str | None:
        if path is None or not path.is_file():
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[:40000] if include_raw else text[-2000:]
