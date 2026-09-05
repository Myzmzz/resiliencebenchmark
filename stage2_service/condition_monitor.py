"""Independent runtime monitor for condition-triggered Agent recovery."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from threading import Event, Lock, Thread
from typing import Any, Protocol

from .condition_policy import CONDITION_POLICY, evaluate_condition


class WorkloadEvidence(Protocol):
    def current(self) -> Mapping[str, Any]: ...

    def baseline(self, trial_id: str) -> Mapping[str, Any]: ...


class ChaosCleanup(Protocol):
    def status(self, cleanup_handle: str) -> Mapping[str, Any]: ...

    def destroy(self, cleanup_handle: str) -> Mapping[str, Any]: ...


class ConditionRecoveryMonitor:
    """Observe the independent effect gate and enforce bounded fallback cleanup."""

    def __init__(
        self,
        workload: WorkloadEvidence,
        chaos: ChaosCleanup,
        *,
        poll_seconds: float = 2.0,
    ) -> None:
        self.workload = workload
        self.chaos = chaos
        self.poll_seconds = poll_seconds
        self._stop = Event()
        self._agent_cleanup = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._trial_id: str | None = None
        self._cleanup_handle: str | None = None
        self._emit: Callable[[str, Mapping[str, Any]], None] | None = None
        self._result: dict[str, Any] = {
            "schema_version": "stage2-condition-monitor.v1",
            "armed": False,
            "effect_condition_met": False,
            "effect_observation_timed_out": False,
            "agent_cleanup_requested": False,
            "agent_cleanup_timely": False,
            "controller_fallback_used": False,
        }

    def arm(
        self,
        *,
        trial_id: str,
        cleanup_handle: str,
        plan: Mapping[str, Any],
        emit: Callable[[str, Mapping[str, Any]], None],
    ) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._trial_id = trial_id
            self._cleanup_handle = cleanup_handle
            self._emit = emit
            self._result.update(
                {
                    "armed": True,
                    "trial_id": trial_id,
                    "plan": dict(plan),
                    "armed_at": _now(),
                }
            )
            self._thread = Thread(
                target=self._run,
                args=(dict(plan),),
                daemon=True,
                name=f"condition-monitor-{trial_id[-16:]}",
            )
            self._thread.start()

    def agent_cleanup_requested(self, occurred_at: datetime) -> None:
        with self._lock:
            self._result["agent_cleanup_requested"] = True
            self._result["agent_cleanup_requested_at"] = occurred_at.isoformat()
        self._agent_cleanup.set()

    def finish(self) -> dict[str, Any]:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(3.0, self.poll_seconds + 1.0))
        current = self.snapshot()
        if (
            current.get("armed") is True
            and current.get("agent_cleanup_requested") is not True
            and current.get("controller_fallback_used") is not True
        ):
            self._fallback_cleanup(reason="agent_session_ended")
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._result)

    def _run(self, plan: Mapping[str, Any]) -> None:
        if not self._wait_for_running():
            return
        baseline = dict(self.workload.baseline(str(self._trial_id)))
        condition = dict(plan.get("effect_condition") or {})
        observation_seconds = int(
            plan.get("effect_observation_seconds")
            or CONDITION_POLICY["effect_observation_seconds"]
        )
        sustain_seconds = int(
            plan.get("effect_sustain_seconds")
            or CONDITION_POLICY["effect_sustain_seconds"]
        )
        cleanup_seconds = int(
            plan.get("agent_cleanup_seconds")
            or CONDITION_POLICY["agent_cleanup_seconds"]
        )
        started = time.monotonic()
        matched_since: float | None = None
        latest: dict[str, Any] = {}
        while not self._stop.is_set() and not self._agent_cleanup.is_set():
            try:
                sample = dict(self.workload.current())
                matched, latest = evaluate_condition(
                    condition, baseline=baseline, sample=sample
                )
            except Exception as exc:  # noqa: BLE001 - a failed sample stays unverified.
                matched = False
                latest = {"error_type": type(exc).__name__, "matched": False}
            now = time.monotonic()
            matched_since = (
                matched_since or now
                if matched
                else None
            )
            if matched_since is not None and now - matched_since >= sustain_seconds:
                met_at = _now()
                with self._lock:
                    self._result.update(
                        {
                            "effect_condition_met": True,
                            "effect_condition_met_at": met_at,
                            "effect_condition_evidence": latest,
                        }
                    )
                self._notify("effect_condition_met", self.snapshot())
                self._await_agent_or_fallback(cleanup_seconds)
                return
            if now - started >= observation_seconds:
                with self._lock:
                    self._result.update(
                        {
                            "effect_observation_timed_out": True,
                            "effect_observation_timed_out_at": _now(),
                            "effect_condition_evidence": latest,
                        }
                    )
                self._notify("effect_observation_timed_out", self.snapshot())
                self._await_agent_or_fallback(cleanup_seconds)
                return
            self._stop.wait(self.poll_seconds)
        if self._agent_cleanup.is_set():
            with self._lock:
                self._result["agent_cleanup_before_effect_condition"] = True

    def _wait_for_running(self) -> bool:
        handle = str(self._cleanup_handle or "")
        while not self._stop.is_set() and not self._agent_cleanup.is_set():
            try:
                status = dict(self.chaos.status(handle))
            except Exception as exc:  # noqa: BLE001 - keep polling inside the budget.
                status = {"error_type": type(exc).__name__}
            live = status.get("live") if isinstance(status.get("live"), Mapping) else {}
            running = (
                status.get("ever_active") is True
                and status.get("resource_absent") is not True
                and (
                    str(status.get("state") or "").lower() == "active"
                    or str(status.get("phase") or "").lower() == "running"
                    or str(live.get("phase") or "").lower() == "running"
                )
            )
            if running:
                with self._lock:
                    self._result.update(
                        {
                            "fault_running_verified": True,
                            "fault_running_at": status.get("started_at") or _now(),
                        }
                    )
                self._notify("condition_monitor_fault_running", self.snapshot())
                return True
            if status.get("ever_active") is True and status.get("resource_absent") is True:
                with self._lock:
                    self._result["fault_disappeared_before_monitoring"] = True
                return False
            self._stop.wait(self.poll_seconds)
        return False

    def _await_agent_or_fallback(self, cleanup_seconds: int) -> None:
        if self._agent_cleanup.wait(cleanup_seconds):
            with self._lock:
                self._result["agent_cleanup_timely"] = True
            self._notify("agent_condition_cleanup_observed", self.snapshot())
            return
        if self._stop.is_set():
            return
        self._fallback_cleanup(reason="agent_cleanup_deadline_exceeded")

    def _fallback_cleanup(self, *, reason: str) -> None:
        handle = str(self._cleanup_handle or "")
        try:
            cleanup = dict(self.chaos.destroy(handle))
        except Exception as exc:  # noqa: BLE001 - fallback failure is evidence.
            cleanup = {"verified_absent": False, "error_type": type(exc).__name__}
        with self._lock:
            self._result.update(
                {
                    "controller_fallback_used": True,
                    "controller_fallback_at": _now(),
                    "controller_fallback_reason": reason,
                    "controller_cleanup": cleanup,
                }
            )
        self._notify("controller_condition_cleanup", self.snapshot())

    def _notify(self, kind: str, payload: Mapping[str, Any]) -> None:
        callback = self._emit
        if callback is not None:
            callback(kind, payload)


def _now() -> str:
    return datetime.now(UTC).isoformat()
