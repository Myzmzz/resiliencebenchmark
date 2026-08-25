from __future__ import annotations

from pathlib import Path

import pytest

from controller.disturbance_runtime import RuntimeDisturbanceInjectorFactory
from controller.trial_preparation import TrialRuntimeContextStore
from disturbances.types import DisturbanceType
from progression.controller import TrialTicket


def _ticket() -> TrialTicket:
    return TrialTicket(
        trial_id="run-1-L3-a1",
        run_id="run-1",
        episode_id="EPI-1",
        level_id="L3",
        attempt=1,
    )


def _context(tmp_path: Path) -> TrialRuntimeContextStore:
    store = TrialRuntimeContextStore(tmp_path / "private" / "contexts")
    store.save(
        "run-1-L3-a1",
        {
            "target": {
                "namespace": "otel-demo",
                "kind": "Pod",
                "name": "frontend-abc",
                "uid": "pod-uid",
                "component": "frontend",
            }
        },
    )
    return store


def _disturbance(kind: str, backend: str) -> dict:
    return {
        "disturbance_id": f"L3-D-{kind}",
        "type": kind,
        "phase": "execution" if kind == "target_drift" else "observation",
        "backend": backend,
        "trigger": {
            "mode": "lifecycle_event" if kind == "target_drift" else "tool_call_sequence",
            "phase": "execution" if kind == "target_drift" else "observation",
            **(
                {"event": "main_fault_applied"}
                if kind == "target_drift"
                else {"tool": "telemetry_ro.telemetry_prom_metric_range", "occurrence": 2}
            ),
        },
        "action": {"operation": "restart_exact_pod" if kind == "target_drift" else "remove_points"},
        "parameters": (
            {"replacement_timeout_seconds": 120}
            if kind == "target_drift"
            else {"schedule_slots": 12, "missing_slots": 3}
        ),
        "expected_behaviors": ["requery_target_identity"],
        "verification": ["controller evidence exists"],
        "replay_seed": 123,
    }


def test_factory_uses_per_trial_target_and_only_qualified_adapters(tmp_path: Path) -> None:
    factory = RuntimeDisturbanceInjectorFactory(
        context_store=_context(tmp_path),
        kubernetes_client=object(),
        telemetry_rule_client=object(),
        controller_record_root=tmp_path / "artifacts",
        namespace_allowlist={"otel-demo"},
        allowed_types={DisturbanceType.TARGET_DRIFT, DisturbanceType.METRIC_DATA_GAP},
    )

    injector = factory(
        _ticket(),
        {
            "level_id": "L3",
            "disturbances": [
                _disturbance("target_drift", "kubernetes"),
                _disturbance("metric_data_gap", "telemetry_interceptor"),
            ],
        },
    )

    assert injector.target["uid"] == "pod-uid"
    assert set(injector.adapters) == {"kubernetes", "telemetry_interceptor"}


def test_factory_rejects_contract_only_disturbance_backend(tmp_path: Path) -> None:
    factory = RuntimeDisturbanceInjectorFactory(
        context_store=_context(tmp_path),
        kubernetes_client=object(),
        telemetry_rule_client=object(),
        controller_record_root=tmp_path / "artifacts",
        namespace_allowlist={"otel-demo"},
        allowed_types={DisturbanceType.TARGET_DRIFT, DisturbanceType.METRIC_DATA_GAP},
    )
    unsupported = _disturbance("target_drift", "kubernetes")
    unsupported.update(
        {
            "disturbance_id": "L3-D-effect",
            "type": "fault_effect_deviation",
            "phase": "verification",
            "backend": "chaos_effect_proxy",
            "trigger": {
                "mode": "lifecycle_event",
                "phase": "verification",
                "event": "fault_effect_check_started",
            },
        }
    )

    with pytest.raises(RuntimeError, match="non-qualified"):
        factory(_ticket(), {"level_id": "L3", "disturbances": [unsupported]})
