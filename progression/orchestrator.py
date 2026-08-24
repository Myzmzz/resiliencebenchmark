"""End-to-end orchestration seam for live multi-level harness adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from disturbances.injector import DisturbanceInjector
from disturbances.types import LifecycleEvent

from .controller import EpisodeProgressStatus, ProgressionController, TrialTicket


EventEmitter = Callable[[LifecycleEvent], list[dict[str, Any]]]


class StreamingTrialRunner(Protocol):
    """A harness adapter that emits lifecycle events while the Agent is live."""

    def __call__(
        self,
        ticket: TrialTicket,
        level: Mapping[str, Any],
        emit_event: EventEmitter,
    ) -> Mapping[str, Any]: ...


class LevelEvaluator(Protocol):
    def __call__(
        self,
        ticket: TrialTicket,
        level: Mapping[str, Any],
        trial_report: Mapping[str, Any],
        controller_records: list[dict[str, Any]],
    ) -> Mapping[str, Any]: ...


InjectorFactory = Callable[[TrialTicket, Mapping[str, Any]], DisturbanceInjector]


@dataclass(frozen=True)
class MultiLevelRunResult:
    run_id: str
    episode_id: str
    agent_id: str
    status: str
    level_results: tuple[dict[str, Any], ...]
    progression_state: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "multi-level-run.v1",
            "run_id": self.run_id,
            "episode_id": self.episode_id,
            "agent_id": self.agent_id,
            "status": self.status,
            "level_results": list(self.level_results),
            "progression_state": self.progression_state,
        }


class MultiLevelOrchestrator:
    """Coordinate trials without deriving PASS from an Agent process exit code."""

    def __init__(
        self,
        controller: ProgressionController,
        *,
        trial_runner: StreamingTrialRunner,
        level_evaluator: LevelEvaluator,
        injector_factory: InjectorFactory,
    ) -> None:
        self.controller = controller
        self.trial_runner = trial_runner
        self.level_evaluator = level_evaluator
        self.injector_factory = injector_factory

    def run(self) -> MultiLevelRunResult:
        results: list[dict[str, Any]] = []
        while self.controller.status is EpisodeProgressStatus.ACTIVE:
            ticket = self.controller.start_trial()
            level = self.controller.current_level
            assert level is not None
            injector = self.injector_factory(ticket, level)
            controller_records: list[dict[str, Any]] = []

            def emit(event: LifecycleEvent) -> list[dict[str, Any]]:
                records = injector.process_event(event)
                controller_records.extend(records)
                return records

            trial_report: Mapping[str, Any]
            try:
                trial_report = self.trial_runner(ticket, level, emit)
            except Exception as exc:  # noqa: BLE001 - turn runner failure into an auditable level result.
                trial_report = {"status": "failed", "error": str(exc)}
            finally:
                controller_records.extend(injector.cleanup_all(reason="trial_finished"))
            result = dict(
                self.level_evaluator(
                    ticket,
                    level,
                    trial_report,
                    controller_records,
                )
            )
            if result.get("level_id") != ticket.level_id or int(result.get("attempt", 0)) != ticket.attempt:
                raise ValueError("level evaluator result does not match the active trial ticket")
            if result.get("primary_status") not in {"PASS", "FAIL", "SKIP"}:
                raise ValueError("level evaluator must return primary_status PASS, FAIL, or SKIP")
            result_ref = str(result.get("result_ref") or f"level-results/{ticket.trial_id}.json")
            self.controller.record_result(
                ticket.trial_id,
                primary_status=str(result["primary_status"]),
                failure_status=(
                    str(result["failure_status"])
                    if result.get("failure_status") is not None
                    else None
                ),
                result_ref=result_ref,
            )
            results.append(result)
        return MultiLevelRunResult(
            run_id=self.controller.run_id,
            episode_id=str(self.controller.episode["episode_id"]),
            agent_id=self.controller.agent_id,
            status=self.controller.status.value,
            level_results=tuple(results),
            progression_state=self.controller.snapshot(),
        )
