"""D7/D8 qualification checks with an explicit pre-fault/run-time boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .records import D7HistoricalSample, D8CanaryEvidence, FaultRunningWindow


@dataclass(frozen=True)
class PrecheckResult:
    valid: bool
    reason: str | None
    record_refs: tuple[str, ...]


class CapabilityLossPrecheck:
    """Validate prerequisites without claiming an as-yet-uncreated fault window."""

    @staticmethod
    def d7_history(
        *, alternative_server: str, target_uid: str, samples: tuple[D7HistoricalSample, ...], now: datetime,
    ) -> PrecheckResult:
        matching = tuple(
            sample for sample in samples
            if sample.server == alternative_server and sample.target_uid == target_uid and sample.observed_at <= now
        )
        if not matching:
            return PrecheckResult(False, "alternative_has_no_historical_target_sample", ())
        return PrecheckResult(True, None, tuple(sample.record_ref for sample in matching))

    @staticmethod
    def d7_runtime_window(*, window: FaultRunningWindow, now: datetime) -> PrecheckResult:
        """Only after fault-running fact exists may the actual window be checked."""

        if window.started_at > now:
            return PrecheckResult(False, "fault_window_starts_in_future", (window.oracle_record_ref,))
        return PrecheckResult(True, None, (window.oracle_record_ref,))

    @staticmethod
    def d8_canary(*, alternative_server: str, canary: D8CanaryEvidence) -> PrecheckResult:
        if canary.alternative_server != alternative_server:
            return PrecheckResult(False, "canary_server_mismatch", (canary.record_ref,))
        if not canary.create_verified or not canary.destroy_verified:
            return PrecheckResult(False, "alternative_canary_incomplete", (canary.record_ref,))
        return PrecheckResult(True, None, (canary.record_ref,))
