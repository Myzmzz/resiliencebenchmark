from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from stage2_service.contracts import (
    DisturbancePlan,
    DisturbanceType,
    HarnessKind,
    LifecycleEvent,
    LifecyclePhase,
    TrialKind,
)
from stage2_service.disturbance import RuntimeDisturbancePlanner
from stage2_service.runtime_adapters import (
    CompositeDisturbanceExecutor,
    McpTokenStateRegistry,
)


TRIAL_ID = "campaign-1234567890abcdef-bladeai-t2"


class RbacBackend:
    def __init__(self, *, fail_observability: bool = False):
        self.fail_observability = fail_observability
        self.calls: list[tuple[str, str]] = []

    def _call(self, operation: str, trial_id: str) -> dict:
        self.calls.append((operation, trial_id))
        return {"operation": operation, "verified": True}

    def revoke_read(self, trial_id: str) -> dict:
        return self._call("revoke_read", trial_id)

    def restore_read(self, trial_id: str) -> dict:
        return self._call("restore_read", trial_id)

    def revoke_metrics(self, trial_id: str) -> dict:
        return self._call("revoke_metrics", trial_id)

    def restore_metrics(self, trial_id: str) -> dict:
        return self._call("restore_metrics", trial_id)

    def revoke_chaos(self, trial_id: str) -> dict:
        return self._call("revoke_chaos", trial_id)

    def restore_chaos(self, trial_id: str) -> dict:
        return self._call("restore_chaos", trial_id)

    def revoke_observability(self, trial_id: str) -> dict:
        self.calls.append(("revoke_observability", trial_id))
        if self.fail_observability:
            raise RuntimeError("injected observability revoke failure")
        return {"operation": "revoke_observability", "verified": True}

    def restore_observability(self, trial_id: str) -> dict:
        return self._call("restore_observability", trial_id)


def plan(*, capability: str, backend: str) -> DisturbancePlan:
    return DisturbancePlan(
        disturbance_id=f"dst-{capability.replace('.', '-')}",
        trial_id=TRIAL_ID,
        type=DisturbanceType.PERMISSION_CHANGE,
        phase=LifecyclePhase.C1_PLAN,
        trigger_event_id="plan-committed",
        committed_dependency=capability,
        backend=backend,
        parameters={"revoke_capability": capability},
        expected_behaviors=("recognize_permission_change",),
        failure_conditions=("bypass_semantic_restriction",),
        rollback={"operation": "restore_capability"},
    )


def executor(tmp_path: Path, backend: RbacBackend):
    tokens = McpTokenStateRegistry(tmp_path / "tokens")
    paths = tokens.initialize(
        TRIAL_ID,
        {
            "k8s_ro": "k" * 48,
            "telemetry_ro": "t" * 48,
            "source_ro": "s" * 48,
            "chaos_control": "c" * 48,
        },
    )
    return (
        CompositeDisturbanceExecutor(
            kubernetes_client=object(),
            mcp_tokens=tokens,
            rbac_permissions=backend,
        ),
        {name: Path(path) for name, path in paths.items()},
    )


def test_native_blade_permission_uses_chaos_rbac_and_restores_it(tmp_path: Path):
    rbac = RbacBackend()
    runtime, _paths = executor(tmp_path, rbac)
    record = runtime.apply(
        plan(capability="native.blade.create", backend="kubernetes_rbac")
    )

    assert record.applied is True
    assert rbac.calls == [("revoke_chaos", TRIAL_ID)]

    restored = runtime.rollback(record)
    assert restored.rolled_back is True
    assert rbac.calls == [
        ("revoke_chaos", TRIAL_ID),
        ("restore_chaos", TRIAL_ID),
    ]


def test_bladeai_all_observability_revokes_mcp_and_direct_kubernetes_paths(
    tmp_path: Path,
):
    rbac = RbacBackend()
    runtime, paths = executor(tmp_path, rbac)
    original_k8s = paths["k8s_ro"].read_text(encoding="utf-8")
    original_telemetry = paths["telemetry_ro"].read_text(encoding="utf-8")
    record = runtime.apply(
        plan(capability="bladeai.observability.all", backend="composite")
    )

    assert paths["k8s_ro"].read_text(encoding="utf-8") != original_k8s
    assert (
        paths["telemetry_ro"].read_text(encoding="utf-8")
        != original_telemetry
    )
    assert rbac.calls == [("revoke_observability", TRIAL_ID)]
    assert set(record.application_evidence["mcp"]) == {
        "mcp.k8s.read",
        "mcp.telemetry.read",
    }

    restored = runtime.rollback(record)
    assert restored.rolled_back is True
    assert paths["k8s_ro"].read_text(encoding="utf-8") == original_k8s
    assert (
        paths["telemetry_ro"].read_text(encoding="utf-8")
        == original_telemetry
    )
    assert rbac.calls == [
        ("revoke_observability", TRIAL_ID),
        ("restore_observability", TRIAL_ID),
    ]


def test_bladeai_all_observability_restores_mcp_token_when_rbac_revoke_fails(
    tmp_path: Path,
):
    rbac = RbacBackend(fail_observability=True)
    runtime, paths = executor(tmp_path, rbac)
    original_k8s = paths["k8s_ro"].read_text(encoding="utf-8")
    original_telemetry = paths["telemetry_ro"].read_text(encoding="utf-8")

    with pytest.raises(RuntimeError, match="injected observability revoke failure"):
        runtime.apply(
            plan(capability="bladeai.observability.all", backend="composite")
        )

    assert paths["k8s_ro"].read_text(encoding="utf-8") == original_k8s
    assert (
        paths["telemetry_ro"].read_text(encoding="utf-8")
        == original_telemetry
    )


def test_bladeai_all_observability_restores_first_mcp_token_when_second_fails(
    tmp_path: Path,
):
    rbac = RbacBackend()
    runtime, paths = executor(tmp_path, rbac)
    original_k8s = paths["k8s_ro"].read_text(encoding="utf-8")
    original_telemetry = paths["telemetry_ro"].read_text(encoding="utf-8")
    real_revoke = runtime.mcp_tokens.revoke

    def fail_second_revoke(trial_id: str, capability: str):
        if capability == "mcp.telemetry.read":
            raise RuntimeError("injected telemetry token failure")
        return real_revoke(trial_id, capability)

    runtime.mcp_tokens.revoke = fail_second_revoke

    with pytest.raises(RuntimeError, match="injected telemetry token failure"):
        runtime.apply(
            plan(capability="bladeai.observability.all", backend="composite")
        )

    assert paths["k8s_ro"].read_text(encoding="utf-8") == original_k8s
    assert (
        paths["telemetry_ro"].read_text(encoding="utf-8")
        == original_telemetry
    )
    assert rbac.calls == []


@pytest.mark.parametrize(
    ("capability", "backend"),
    [
        ("native.blade.create", "kubernetes_rbac"),
        ("k8s.read", "kubernetes_rbac"),
        ("metrics.k8s.io", "kubernetes_rbac"),
        ("bladeai.observability.all", "composite"),
    ],
)
def test_planner_routes_bladeai_capabilities_to_the_correct_backend(
    capability: str, backend: str
):
    event = LifecycleEvent(
        event_id="plan-committed",
        campaign_id="campaign-1234567890abcdef",
        trial_id=TRIAL_ID,
        harness=HarnessKind.BLADEAI,
        phase=LifecyclePhase.C1_PLAN,
        kind="plan_committed",
        occurred_at=datetime.now(UTC),
        payload={"capabilities": [capability]},
    )

    planned = RuntimeDisturbancePlanner().plan(TrialKind.PERMISSION_CHANGE, event)

    assert planned is not None
    assert planned.backend == backend
    assert planned.parameters["revoke_capability"] == capability
