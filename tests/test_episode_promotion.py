from __future__ import annotations

import json
from pathlib import Path

import pytest

from controller.run_contracts import (
    AnalysisMode,
    HarnessSelection,
    ProgressionPolicy,
    RunMode,
    RunSpec,
    ScanScope,
)
from controller.system_snapshot import (
    ObserverSnapshot,
    RuntimeSnapshot,
    RuntimeTargetSnapshot,
    SnapshotStatus,
    SystemScanner,
)
from tasks.episode_promotion import (
    EpisodePromotionError,
    PromotionQualification,
    promote_episode,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT.parent / "benchmark-sources" / "materialized"


def _spec() -> RunSpec:
    return RunSpec(
        request_id="promote-001",
        requester="benchmark-admin",
        mode=RunMode.EXECUTE,
        analysis_mode=AnalysisMode.MODEL,
        scan=ScanScope(
            application="otel-demo",
            namespace="otel-demo",
            source_lock_id="otel-demo-2.2.0",
            evidence_roots=["benchmark-app"],
        ),
        harness=HarnessSelection(
            harness_id="codex",
            model_alias="gpt-5.6",
            track="native",
        ),
        progression=ProgressionPolicy(
            profile_id="standard-l3",
            max_levels=3,
            retry_budget_per_level=1,
            total_retry_budget=3,
        ),
    )


class RuntimeAdapter:
    def scan(self, namespace: str) -> RuntimeSnapshot:
        return RuntimeSnapshot(
            status=SnapshotStatus.QUALIFIED,
            nodes_total=3,
            nodes_ready=3,
            controllers_desired=22,
            controllers_ready=22,
            pods_total=22,
            pods_ready=22,
            active_application="otel-demo",
            chaosblade_global_count=0,
            targets=[
                RuntimeTargetSnapshot(
                    kind="Pod",
                    name="checkout-abc",
                    uid="checkout-uid",
                    ready=True,
                    component="checkout",
                )
            ],
        )


class ObservationAdapter:
    def scan(self, namespace: str) -> ObserverSnapshot:
        return ObserverSnapshot(
            status=SnapshotStatus.QUALIFIED,
            prometheus_series=100,
            jaeger_trace_count=1,
            jaeger_services=["frontend", "checkout"],
            loki_streams=2,
        )


def _snapshot():
    return SystemScanner(REPO_ROOT, SOURCE_ROOT).scan(
        "run-promote",
        _spec(),
        runtime_adapter=RuntimeAdapter(),
        observation_adapter=ObservationAdapter(),
    )


def _internal_episode() -> dict:
    return {
        "episode_id": "EPI-PROMOTION-001",
        "defect_ref": "RBD-001",
        "application_snapshot": {
            "application": "otel-demo",
            "namespace": "otel-demo",
            "candidate_services": ["checkoutservice"],
            "runtime_target": {"status": "unresolved"},
        },
        "action_space": {
            "allowed_trigger_classes": ["latency"],
            "selected_actuator": None,
            "parameters": [],
        },
        "budget": {
            "max_experiments": 3,
            "max_duration_minutes": 30,
            "max_concurrent_faults": 1,
        },
        "readiness": {
            "ready_for_execution": False,
            "ready_for_lock": False,
            "execution_blockers": ["runtime target unresolved"],
        },
    }


def test_promotion_binds_exact_live_uid_and_separates_public_task_from_ground_truth() -> None:
    promoted = promote_episode(
        _internal_episode(),
        _snapshot(),
        _spec(),
        PromotionQualification(
            independent_observers_qualified=True,
            cleanup_path_qualified=True,
        ),
    )

    target = promoted.multi_level_episode["base_task"]["target"]
    main_fault = promoted.multi_level_episode["base_task"]["main_fault"]
    public_text = json.dumps(promoted.public_episode).lower()
    assert target["name"] == "checkout-abc"
    assert target["uid"] == "checkout-uid"
    assert main_fault["type"] == "network-delay"
    assert main_fault["actuator"] == "network-delay"
    assert main_fault["parameters"]["delay_ms"] == 100
    assert promoted.public_episode["budget"]["max_experiments"] == 3
    assert promoted.multi_level_episode["levels"][0]["disturbances"] == []
    assert [
        item["type"]
        for item in promoted.multi_level_episode["levels"][1]["disturbances"]
    ] == ["target_drift"]
    assert [
        item["type"]
        for item in promoted.multi_level_episode["levels"][2]["disturbances"]
    ] == ["target_drift", "metric_data_gap"]
    assert "rbd-001" not in public_text
    assert "defect_ref" not in public_text
    assert promoted.multi_level_episode["base_task"]["agent_visible_task"] == promoted.public_episode


def test_promotion_blocks_when_independent_observers_are_not_qualified() -> None:
    with pytest.raises(EpisodePromotionError, match="independent observers"):
        promote_episode(
            _internal_episode(),
            _snapshot(),
            _spec(),
            PromotionQualification(
                independent_observers_qualified=False,
                cleanup_path_qualified=True,
            ),
        )
