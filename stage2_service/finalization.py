"""Agent recovery scoring plus Controller-owned fallback cleanup."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from .contracts import HarnessReport, RecoveryResult, TrialRuntimeContext


class ChaosCleanupBackend(Protocol):
    def destroy(self, cleanup_handle: str) -> Mapping[str, Any]: ...

    def status(self, cleanup_handle: str) -> Mapping[str, Any]: ...

    def inventory(self, namespace: str) -> Mapping[str, Any]: ...


class RecoveryEvidenceProvider(Protocol):
    def current(self) -> Mapping[str, Any]: ...

    def effect_since(self, trial_id: str) -> Mapping[str, Any]: ...

    def reset_and_wait_healthy(
        self,
        *,
        timeout_seconds: int = 300,
        minimum_requests: int = 20,
        stability_samples: int = 3,
    ) -> Mapping[str, Any]: ...


class Stage2Finalizer:
    def __init__(
        self,
        chaos: ChaosCleanupBackend,
        recovery_evidence: RecoveryEvidenceProvider,
        recovery_timeout_seconds: int = 180,
        poll_seconds: int = 5,
        sleep=time.sleep,
    ):
        self.chaos = chaos
        self.recovery_evidence = recovery_evidence
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self.poll_seconds = poll_seconds
        self.sleep = sleep

    def finalize(
        self,
        trial_id: str,
        episode,
        runtime: TrialRuntimeContext,
        report: HarnessReport,
    ) -> RecoveryResult:
        del episode
        agent_attempted = any(
            event.kind == "recovery_requested" for event in report.lifecycle_events
        )
        pre_status = self._safe(self.chaos.status, runtime.cleanup_handle)
        pre_absent = pre_status.get("resource_absent") is True
        ever_active = pre_status.get("ever_active") is True
        target_verified = (
            ever_active
            and pre_status.get("target_uid") == runtime.target.uid
            and pre_status.get("fault_type") == runtime.main_fault.get("fault_type")
        )
        timeout_recovery_observed = False
        timeout_wait_seconds = 0.0
        if ever_active and not pre_absent and not agent_attempted:
            timeout_wait_seconds = self._remaining_fault_seconds(
                pre_status, runtime
            )
            checks = max(1, math.ceil(timeout_wait_seconds / self.poll_seconds))
            for _ in range(checks):
                self.sleep(min(self.poll_seconds, timeout_wait_seconds or self.poll_seconds))
                observed = self._safe(self.chaos.status, runtime.cleanup_handle)
                if observed.get("resource_absent") is True:
                    pre_status = observed
                    pre_absent = True
                    timeout_recovery_observed = True
                    break
        effect = dict(self.recovery_evidence.effect_since(trial_id))
        effect["timeout_recovery_observed"] = timeout_recovery_observed
        effect["timeout_wait_seconds"] = round(timeout_wait_seconds, 3)
        destroy = self._safe(self.chaos.destroy, runtime.cleanup_handle)
        status = self._safe(self.chaos.status, runtime.cleanup_handle)
        inventory = self._safe(self.chaos.inventory, runtime.target.namespace)
        fault_absent = (
            pre_absent
            or destroy.get("verified_absent") is True
            or status.get("resource_absent") is True
            or (
                inventory.get("global_chaosblade_count") == 0
                and inventory.get("active_owned_count") == 0
            )
        )
        try:
            evidence = dict(
                self.recovery_evidence.reset_and_wait_healthy(
                    timeout_seconds=self.recovery_timeout_seconds,
                    minimum_requests=10,
                    stability_samples=2,
                )
            )
        except Exception as exc:  # noqa: BLE001
            evidence = {
                "business_healthy": False,
                "error_type": type(exc).__name__,
            }
        business_recovered = (
            evidence.get("application_owned") is True
            and evidence.get("load_generator_ready") is True
            and evidence.get("traffic_observed") is True
            and evidence.get("business_healthy") is True
        )
        agent_recovery_verified = agent_attempted and pre_absent and business_recovered
        controller_cleanup_verified = fault_absent and business_recovered
        return RecoveryResult(
            agent_attempted=agent_attempted,
            agent_recovery_verified=agent_recovery_verified,
            controller_cleanup_verified=controller_cleanup_verified,
            fault_absent=fault_absent,
            business_recovery_verified=business_recovered,
            main_fault_ever_active=ever_active,
            main_fault_target_verified=target_verified,
            fault_effect_verified=effect.get("verified") is True,
            fault_effect_evidence=effect,
            evidence_refs=(
                "controller://chaos-cleanup",
                "application://builtin-load-generator/cart-delta",
                "application://builtin-load-generator/recovery",
            ),
        )

    @staticmethod
    def _remaining_fault_seconds(
        status: Mapping[str, Any], runtime: TrialRuntimeContext
    ) -> float:
        raw_deadline = status.get("deadline_at")
        if raw_deadline:
            try:
                deadline = datetime.fromisoformat(
                    str(raw_deadline).replace("Z", "+00:00")
                )
                return max(0.0, (deadline - datetime.now(UTC)).total_seconds()) + 10
            except ValueError:
                pass
        return float(runtime.main_fault.get("duration_seconds") or 0) + 10

    @staticmethod
    def _safe(operation, *args) -> dict[str, Any]:
        try:
            return dict(operation(*args))
        except Exception as exc:  # noqa: BLE001 - fallback continues and records absence conservatively.
            return {"error_type": type(exc).__name__}
