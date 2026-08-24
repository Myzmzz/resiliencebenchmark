"""Event-driven disturbance injector with fail-closed safety authorization."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import random
from typing import Any, Mapping, Protocol, Sequence

from controller.disturbance import (
    DisturbanceAuthorizationDecision,
    DisturbanceAuthorizationRequest,
)

from .types import (
    DisturbanceSpec,
    DisturbanceType,
    LifecycleEvent,
    TriggerMode,
    derive_replay_seed,
)


class SafetyGate(Protocol):
    def authorize(
        self, request: DisturbanceAuthorizationRequest
    ) -> DisturbanceAuthorizationDecision: ...


class ControllerRecordSink(Protocol):
    def append(self, record: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True)
class DisturbanceExecutionRequest:
    run_id: str
    level_id: str
    spec: DisturbanceSpec
    target: Mapping[str, Any]
    seed: int
    triggered_by: LifecycleEvent


@dataclass(frozen=True)
class AdapterExecutionResult:
    outcome: Mapping[str, Any]
    cleanup_state: Mapping[str, Any] = field(default_factory=dict)
    cleanup_required: bool = False


class DisturbanceAdapter(Protocol):
    def execute(self, request: DisturbanceExecutionRequest) -> AdapterExecutionResult: ...

    def cleanup(
        self,
        request: DisturbanceExecutionRequest,
        cleanup_state: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


@dataclass
class ActiveExecution:
    request: DisturbanceExecutionRequest
    adapter: DisturbanceAdapter
    cleanup_state: Mapping[str, Any]
    cleanup_required: bool
    event_id: str


class InMemoryControllerRecordSink:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def append(self, record: Mapping[str, Any]) -> None:
        self.records.append(dict(record))


class JsonlControllerRecordSink:
    """Append-only JSONL evidence sink owned by the Controller."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, record: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(record), ensure_ascii=False, sort_keys=True) + "\n")


class KubernetesDisturbanceAdapter:
    """Execute exact-target Kubernetes disturbances through an injected client.

    The client is expected to use a Controller identity, not Agent credentials.
    It must implement ``restart_exact_pod``, ``read_resource_quota``,
    ``patch_resource_quota`` and ``restore_resource_quota`` as applicable.
    """

    def __init__(self, client: Any) -> None:
        self.client = client

    def execute(self, request: DisturbanceExecutionRequest) -> AdapterExecutionResult:
        target = request.target
        parameters = dict(request.spec.parameters)
        if request.spec.type is DisturbanceType.TARGET_DRIFT:
            replacement = self.client.restart_exact_pod(
                namespace=str(target["namespace"]),
                name=str(target["name"]),
                expected_uid=str(target["uid"]),
                timeout_seconds=int(parameters.get("replacement_timeout_seconds", 120)),
                labels={"benchmark.run_id": request.run_id, "benchmark.level_id": request.level_id},
            )
            replacement_uid = str(replacement.get("uid") or "")
            if not replacement_uid or replacement_uid == str(target["uid"]):
                raise RuntimeError("target drift did not produce a distinct replacement Pod UID")
            return AdapterExecutionResult(
                outcome={
                    "old_uid": str(target["uid"]),
                    "replacement_name": str(replacement.get("name") or target["name"]),
                    "replacement_uid": replacement_uid,
                }
            )
        if request.spec.type is DisturbanceType.RESOURCE_QUOTA_REDUCTION:
            namespace = str(target["namespace"])
            quota_name = str(parameters["quota_name"])
            previous = self.client.read_resource_quota(namespace=namespace, name=quota_name)
            applied = self.client.patch_resource_quota(
                namespace=namespace,
                name=quota_name,
                cpu_percent=float(parameters["cpu_limit_percent_of_original"]),
                memory_percent=float(parameters["memory_limit_percent_of_original"]),
                labels={"benchmark.run_id": request.run_id, "benchmark.level_id": request.level_id},
            )
            return AdapterExecutionResult(
                outcome={"quota_name": quota_name, "applied": applied},
                cleanup_state={"namespace": namespace, "quota_name": quota_name, "previous": previous},
                cleanup_required=True,
            )
        raise ValueError(f"kubernetes adapter does not support {request.spec.type.value}")

    def cleanup(
        self,
        request: DisturbanceExecutionRequest,
        cleanup_state: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if request.spec.type is DisturbanceType.RESOURCE_QUOTA_REDUCTION:
            restored = self.client.restore_resource_quota(
                namespace=str(cleanup_state["namespace"]),
                name=str(cleanup_state["quota_name"]),
                previous=cleanup_state["previous"],
            )
            return {"restored": restored}
        return {"cleanup": "not_required"}


class TelemetryInterceptorAdapter:
    """Install deterministic MCP telemetry response rules through a proxy client."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def execute(self, request: DisturbanceExecutionRequest) -> AdapterExecutionResult:
        parameters = dict(request.spec.parameters)
        if request.spec.type is DisturbanceType.TELEMETRY_INSTABILITY:
            schedule = _selected_slots(
                request.seed,
                int(parameters.get("schedule_slots", 10)),
                int(parameters.get("failure_slots", 3)),
            )
            rule = {
                "kind": "telemetry_failure_schedule",
                "tool": request.spec.trigger.tool or "telemetry_prom_metric_range",
                "slots": schedule,
                "response_modes": list(request.spec.action.get("response_modes", ["http_503"])),
                "timeout_milliseconds": int(parameters.get("timeout_milliseconds", 1500)),
            }
        elif request.spec.type is DisturbanceType.METRIC_DATA_GAP:
            schedule = _selected_slots(
                request.seed,
                int(parameters.get("schedule_slots", 12)),
                int(parameters.get("missing_slots", 3)),
            )
            rule = {
                "kind": "metric_data_gap",
                "tool": request.spec.trigger.tool or "telemetry_prom_metric_range",
                "missing_slots": schedule,
            }
        else:
            raise ValueError(f"telemetry adapter does not support {request.spec.type.value}")
        rule_id = str(
            self.client.register_rule(
                run_id=request.run_id,
                level_id=request.level_id,
                disturbance_id=request.spec.disturbance_id,
                rule=rule,
            )
        )
        if not rule_id:
            raise RuntimeError("telemetry interceptor did not return a rule id")
        return AdapterExecutionResult(
            outcome={"rule_id": rule_id, "rule": rule},
            cleanup_state={"rule_id": rule_id},
            cleanup_required=True,
        )

    def cleanup(
        self,
        request: DisturbanceExecutionRequest,
        cleanup_state: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        rule_id = str(cleanup_state["rule_id"])
        removed = self.client.remove_rule(rule_id)
        events_method = getattr(self.client, "events", None)
        interceptor_events = []
        if callable(events_method):
            interceptor_events = [
                item
                for item in events_method()
                if isinstance(item, Mapping) and item.get("rule_id") == rule_id
            ]
        return {
            "rule_id": rule_id,
            "removed": bool(removed),
            "interceptor_events": interceptor_events,
        }


class DisturbanceInjector:
    """Match lifecycle events, authorize, execute, record, and clean disturbances."""

    MUTATING_BACKENDS = frozenset(
        {"kubernetes", "chaos_effect_proxy", "workload_interceptor", "safety_pressure"}
    )

    def __init__(
        self,
        *,
        run_id: str,
        level_id: str,
        trial_id: str | None = None,
        attempt: int = 1,
        specs: Sequence[DisturbanceSpec],
        target: Mapping[str, Any],
        safety_gate: SafetyGate,
        adapters: Mapping[str, DisturbanceAdapter],
        record_sink: ControllerRecordSink,
    ) -> None:
        self.run_id = run_id
        self.level_id = level_id
        self.attempt = int(attempt)
        if self.attempt < 1:
            raise ValueError("attempt must be positive")
        self.trial_id = trial_id or f"{run_id}-{level_id}-a{self.attempt}"
        self.specs = tuple(specs)
        self.target = dict(target)
        self.safety_gate = safety_gate
        self.adapters = dict(adapters)
        self.record_sink = record_sink
        self._triggered: set[str] = set()
        self._tool_occurrences: dict[str, int] = {}
        self._active: list[ActiveExecution] = []

    def process_event(self, event: LifecycleEvent) -> list[dict[str, Any]]:
        if event.run_id != self.run_id or event.level_id != self.level_id:
            raise ValueError("lifecycle event does not belong to this injector run and level")
        if event.kind == "tool_call" and event.tool:
            self._tool_occurrences[event.tool] = self._tool_occurrences.get(event.tool, 0) + 1
        records: list[dict[str, Any]] = []
        for spec in self.specs:
            if spec.disturbance_id in self._triggered or not self._matches(spec, event):
                continue
            self._triggered.add(spec.disturbance_id)
            records.extend(self._execute(spec, event))
        return records

    def tick(
        self,
        *,
        phase: Any,
        elapsed_seconds: float,
        occurred_at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        return self.process_event(
            LifecycleEvent(
                run_id=self.run_id,
                level_id=self.level_id,
                phase=phase,
                kind="timer",
                occurred_at=occurred_at or datetime.now(timezone.utc),
                elapsed_seconds=elapsed_seconds,
            )
        )

    def cleanup_all(self, *, reason: str = "level_finished") -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for active in reversed(self._active):
            if not active.cleanup_required:
                continue
            now = _utc_now()
            try:
                outcome = active.adapter.cleanup(active.request, active.cleanup_state)
                status = "cleaned"
            except Exception as exc:  # noqa: BLE001 - evidence must retain cleanup failure.
                outcome = {"error": str(exc)}
                status = "cleanup_failed"
            record = self._record(
                active.request.spec,
                active.request.triggered_by,
                event_id=active.event_id,
                status=status,
                triggered_at=_utc_iso(active.request.triggered_by.occurred_at),
                completed_at=now,
                outcome={"reason": reason, **dict(outcome)},
            )
            records.append(record)
        self._active.clear()
        return records

    @property
    def triggered_ids(self) -> frozenset[str]:
        return frozenset(self._triggered)

    def _matches(self, spec: DisturbanceSpec, event: LifecycleEvent) -> bool:
        trigger = spec.trigger
        if event.phase is not trigger.phase:
            return False
        if trigger.mode is TriggerMode.LIFECYCLE_EVENT:
            return event.kind == trigger.event
        if trigger.mode is TriggerMode.TOOL_CALL_SEQUENCE:
            return (
                event.kind == "tool_call"
                and event.tool == trigger.tool
                and self._tool_occurrences.get(str(trigger.tool), 0) == trigger.occurrence
            )
        if trigger.mode is TriggerMode.TIME_OFFSET:
            return (
                event.kind == "timer"
                and event.elapsed_seconds is not None
                and event.elapsed_seconds >= float(trigger.offset_seconds or 0)
            )
        return False

    def _execute(self, spec: DisturbanceSpec, event: LifecycleEvent) -> list[dict[str, Any]]:
        event_id = f"dst-evt-{derive_replay_seed(self.trial_id, spec.disturbance_id):016x}"
        triggered_at = _utc_iso(event.occurred_at)
        request = DisturbanceExecutionRequest(
            run_id=self.run_id,
            level_id=self.level_id,
            spec=spec,
            target=self.target,
            seed=(
                spec.replay_seed
                if spec.replay_seed is not None
                else derive_replay_seed(self.run_id, self.level_id, spec.disturbance_id)
            ),
            triggered_by=event,
        )
        authorization = self.safety_gate.authorize(
            DisturbanceAuthorizationRequest(
                run_id=self.run_id,
                level_id=self.level_id,
                disturbance_id=spec.disturbance_id,
                disturbance_type=spec.type.value,
                backend=spec.backend,
                target=self.target,
                parameters=spec.parameters,
                labels={"benchmark.run_id": self.run_id, "benchmark.level_id": self.level_id},
                active_mutating_disturbances=sum(
                    1
                    for item in self._active
                    if item.cleanup_required and item.request.spec.backend in self.MUTATING_BACKENDS
                ),
            )
        )
        if not authorization.allowed:
            return [
                self._record(
                    spec,
                    event,
                    event_id=event_id,
                    status="rejected",
                    triggered_at=triggered_at,
                    completed_at=_utc_now(),
                    outcome={"safety_reasons": list(authorization.reasons)},
                )
            ]
        started = self._record(
            spec,
            event,
            event_id=event_id,
            status="triggered",
            triggered_at=triggered_at,
            outcome={"replay_seed": request.seed},
        )
        adapter = self.adapters.get(spec.backend)
        if adapter is None:
            failed = self._record(
                spec,
                event,
                event_id=event_id,
                status="failed",
                triggered_at=triggered_at,
                completed_at=_utc_now(),
                outcome={"error": f"no adapter registered for backend {spec.backend}"},
            )
            return [started, failed]
        try:
            result = adapter.execute(request)
        except Exception as exc:  # noqa: BLE001 - failure is recorded for the evaluator.
            failed = self._record(
                spec,
                event,
                event_id=event_id,
                status="failed",
                triggered_at=triggered_at,
                completed_at=_utc_now(),
                outcome={"error": str(exc)},
            )
            return [started, failed]
        self._active.append(
            ActiveExecution(
                request=request,
                adapter=adapter,
                cleanup_state=dict(result.cleanup_state),
                cleanup_required=result.cleanup_required,
                event_id=event_id,
            )
        )
        completed = self._record(
            spec,
            event,
            event_id=event_id,
            status="completed",
            triggered_at=triggered_at,
            completed_at=_utc_now(),
            outcome=dict(result.outcome),
        )
        return [started, completed]

    def _record(
        self,
        spec: DisturbanceSpec,
        event: LifecycleEvent,
        *,
        event_id: str,
        status: str,
        triggered_at: str,
        outcome: Mapping[str, Any],
        completed_at: str | None = None,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "schema_version": "disturbance-event.v1",
            "disturbance_event_id": event_id,
            "disturbance_id": spec.disturbance_id,
            "run_id": self.run_id,
            "trial_id": self.trial_id,
            "level_id": self.level_id,
            "attempt": self.attempt,
            "type": spec.type.value,
            "phase": spec.phase.value,
            "status": status,
            "triggered_at": triggered_at,
            "trigger": {"spec": spec.trigger.as_dict(), "event": event.as_dict()},
            "action": {
                "backend": spec.backend,
                "operation": spec.action.get("operation"),
                "parameters": dict(spec.parameters),
            },
            "outcome": dict(outcome),
            "evidence_refs": [
                f"controller_record://{self.run_id}/{self.level_id}/{self.trial_id}/{event_id}"
            ],
        }
        if completed_at:
            record["completed_at"] = completed_at
        self.record_sink.append(record)
        return record


def _selected_slots(seed: int, total: int, selected: int) -> list[int]:
    if total < 1 or selected < 0 or selected > total:
        raise ValueError("deterministic schedule requires 0 <= selected <= total and total >= 1")
    return sorted(random.Random(seed).sample(range(1, total + 1), selected))


def _utc_now() -> str:
    return _utc_iso(datetime.now(timezone.utc))


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
