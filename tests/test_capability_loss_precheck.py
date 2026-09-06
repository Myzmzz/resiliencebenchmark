from __future__ import annotations

from datetime import UTC, datetime, timedelta

from stage2_service.capability_loss import CapabilityLossPrecheck, D7HistoricalSample, D8CanaryEvidence
from stage2_service.capability_loss.records import FaultRunningWindow


NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def test_d7_precheck_uses_only_existing_history_not_future_fault_window() -> None:
    result = CapabilityLossPrecheck.d7_history(
        alternative_server="coroot_ro", target_uid="pod-uid",
        samples=(D7HistoricalSample(server="coroot_ro", target_uid="pod-uid", observed_at=NOW - timedelta(seconds=1), record_ref="history-1"),), now=NOW,
    )
    assert result.valid and result.record_refs == ("history-1",)
    future = CapabilityLossPrecheck.d7_runtime_window(window=FaultRunningWindow(started_at=NOW + timedelta(seconds=1), oracle_record_ref="future"), now=NOW)
    assert not future.valid and future.reason == "fault_window_starts_in_future"


def test_d8_canary_requires_create_and_destroy_on_alternative() -> None:
    incomplete = CapabilityLossPrecheck.d8_canary(
        alternative_server="chaos_mesh_control",
        canary=D8CanaryEvidence(alternative_server="chaos_mesh_control", create_verified=True, destroy_verified=False, record_ref="canary"),
    )
    complete = CapabilityLossPrecheck.d8_canary(
        alternative_server="chaos_mesh_control",
        canary=D8CanaryEvidence(alternative_server="chaos_mesh_control", create_verified=True, destroy_verified=True, record_ref="canary-ok"),
    )
    assert not incomplete.valid and complete.valid
