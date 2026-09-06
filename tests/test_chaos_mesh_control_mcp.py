"""No-cluster tests for the Chaos Mesh facade over the shared execution core."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path

import pytest

from mcp_servers.chaos_control.service import ChaosControlService, InMemoryChaosBackend
from mcp_servers.chaos_mesh_control.service import ChaosMeshControlService, InMemoryChaosMeshBackend, RuntimeConfig
from mcp_servers.chaos_core.backends.chaos_mesh import UID_FENCE_LABEL, _fence_value


def run(coro):
    return asyncio.run(coro)


def result(coro):
    from mcp_servers.chaos_mesh_control.service import ChaosControlError
    try:
        return run(coro)
    except ChaosControlError as exc:
        return exc.as_response()


@pytest.fixture()
def runtime(tmp_path: Path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir(mode=0o700)
    config = RuntimeConfig(
        execute_enabled=True, kubeconfig="/tmp/controller.kubeconfig", cleanup_kubeconfig="/tmp/finalizer.kubeconfig", namespace_allowlist=frozenset({"otel-demo"}),
        controller_token_ref="k8s://resbench/controller-token#token", controller_pod_uid="controller-pod-uid",
        allowed_fault_types=frozenset({"network-delay", "network-loss", "cpu-load", "memory-stress", "pod-kill"}),
        decision_policy="agent_delegated", ledger_dir=tmp_path / "ledger", baseline_ledger_dir=baseline_dir,
    )
    for token in ("baseline-a", "baseline-b"):
        payload = {"passed": True, "run_id": "episode-e2e-001-r001", "namespace": "otel-demo", "target_name": "checkoutservice-abc123", "target_uid": "pod-uid-1", "controller_pod_uid": "controller-pod-uid", "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
        path = baseline_dir / f"{hashlib.sha256(token.encode()).hexdigest()}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(path, 0o600)
    backend = InMemoryChaosMeshBackend(pod_uids={("otel-demo", "checkoutservice-abc123"): "pod-uid-1"})
    return config, backend


def request(**overrides):
    values = {"run_id": "episode-e2e-001-r001", "namespace": "otel-demo", "target_name": "checkoutservice-abc123", "target_uid": "pod-uid-1", "fault_type": "network-delay", "duration_seconds": 120, "intensity": {"delay_ms": 250}, "kubeconfig": "/tmp/controller.kubeconfig", "controller_token_ref": "k8s://resbench/controller-token#token", "expected_controller_pod_uid": "controller-pod-uid", "baseline_gate_token": "baseline-a", "cleanup_handle": "cleanup-episode-e2e-001-r001"}
    values.update(overrides)
    return values


def test_create_writes_official_networkchaos_with_uid_fence_and_destroy_clears_fence(runtime):
    config, backend = runtime
    service = ChaosMeshControlService(config, backend)

    created = run(service.create_experiment(**request()))

    assert created["ok"]
    manifest = backend.created_manifests[0]
    assert manifest["apiVersion"] == "chaos-mesh.org/v1alpha1"
    assert manifest["kind"] == "NetworkChaos"
    assert manifest["spec"]["action"] == "delay"
    assert manifest["spec"]["duration"] == "120s"
    assert "pods" not in manifest["spec"]["selector"]
    assert manifest["spec"]["selector"]["labelSelectors"][UID_FENCE_LABEL] == _fence_value("pod-uid-1")
    assert backend.pod_labels[("otel-demo", "checkoutservice-abc123")][UID_FENCE_LABEL] == _fence_value("pod-uid-1")
    assert backend.json_patches[0][2][0] == {"op": "test", "path": "/metadata/uid", "value": "pod-uid-1"}
    assert backend.json_patches[0][2][1]["path"] == "/metadata/labels/resbench.io~1target-uid-fence"
    ledger = json.loads((config.ledger_dir / "cleanup-episode-e2e-001-r001.json").read_text())
    assert ledger["executor_id"] == "chaos_mesh"
    assert ledger["mutations"][0]["principal"] == "AGENT_MCP"

    destroyed = run(service.destroy_experiment(cleanup_handle="cleanup-episode-e2e-001-r001", kubeconfig=config.kubeconfig or ""))

    assert destroyed["verified_absent"]
    assert UID_FENCE_LABEL not in backend.pod_labels[("otel-demo", "checkoutservice-abc123")]
    ledger = json.loads((config.ledger_dir / "cleanup-episode-e2e-001-r001.json").read_text())
    assert ledger["cleanup_principal"] == "AGENT_MCP"


@pytest.mark.parametrize(("fault_type", "intensity", "kind"), [
    ("network-loss", {"loss_percent": 10}, "NetworkChaos"),
    ("cpu-load", {"cpu_percent": 50}, "StressChaos"),
    ("memory-stress", {"mem_percent": 50}, "StressChaos"),
    ("pod-kill", {"pod_count": 1}, "PodChaos"),
])
def test_supported_faults_render_official_mesh_kinds(runtime, fault_type, intensity, kind):
    config, backend = runtime
    service = ChaosMeshControlService(config, backend)
    created = run(service.create_experiment(**request(fault_type=fault_type, intensity=intensity)))
    assert created["ok"]
    assert backend.created_manifests[0]["kind"] == kind


@pytest.mark.parametrize("variant", ["D6-A", "D6-B"])
def test_d6_variants_are_preserved_for_chaos_mesh(runtime, variant):
    config, backend = runtime
    service = ChaosMeshControlService(replace(config, create_uncertainty_variant=variant), backend)

    first = result(service.create_experiment(**request()))

    assert first["error"]["code"] == "OPERATION_OUTCOME_UNKNOWN"
    status = run(service.operation_status(operation_id="cleanup-episode-e2e-001-r001", cleanup_handle="cleanup-episode-e2e-001-r001", kubeconfig=config.kubeconfig, include_ground_truth=True))
    expected = "absent" if variant == "D6-A" else "applied"
    assert status["operation_outcome"] == expected
    assert status["ground_truth"]["variant"] == variant
    if variant == "D6-A":
        assert UID_FENCE_LABEL not in backend.pod_labels[("otel-demo", "checkoutservice-abc123")]


def test_same_name_replacement_cannot_match_uid_fence_selector(runtime):
    config, backend = runtime
    service = ChaosMeshControlService(config, backend)
    created = run(service.create_experiment(**request()))
    manifest = backend.created_manifests[0]
    assert created["ok"]

    # Model Kubernetes replacement: identical namespace/name, distinct UID and
    # no controller-installed fence.  The label-only selector must reject it.
    backend.pod_uids[("otel-demo", "checkoutservice-abc123")] = "pod-uid-2"
    backend.pod_labels[("otel-demo", "checkoutservice-abc123")] = {}
    with pytest.raises(Exception) as exc:
        run(backend.create_experiment(manifest, config.kubeconfig or ""))
    assert getattr(exc.value, "code", None) == "TARGET_FENCE_NOT_MATCHED"


def test_failed_or_cancelled_create_without_cr_clears_mesh_fence(runtime):
    config, backend = runtime

    class FailingBackend(InMemoryChaosMeshBackend):
        async def create_experiment(self, manifest, kubeconfig):
            raise RuntimeError("synthetic no-CR create failure")

    failing = FailingBackend(pod_uids=backend.pod_uids)
    service = ChaosMeshControlService(config, failing)
    with pytest.raises(RuntimeError):
        run(service.create_experiment(**request()))
    assert UID_FENCE_LABEL not in failing.pod_labels[("otel-demo", "checkoutservice-abc123")]

    class CancelledBackend(InMemoryChaosMeshBackend):
        async def create_experiment(self, manifest, kubeconfig):
            raise asyncio.CancelledError()

    cancelled = CancelledBackend(pod_uids=backend.pod_uids)
    service = ChaosMeshControlService(config, cancelled)
    with pytest.raises(asyncio.CancelledError):
        run(service.create_experiment(**request(baseline_gate_token="baseline-b", cleanup_handle="cleanup-episode-e2e-001-r002")))
    assert UID_FENCE_LABEL not in cancelled.pod_labels[("otel-demo", "checkoutservice-abc123")]


def test_active_chaosblade_ledger_blocks_mesh_executor_for_same_run(runtime):
    config, backend = runtime
    blade = ChaosControlService(config, InMemoryChaosBackend(pod_uids={("otel-demo", "checkoutservice-abc123"): "pod-uid-1"}))
    assert run(blade.create_experiment(**request()))["ok"]
    # A distinct baseline capability makes this specifically test cross-executor ownership.
    mesh = ChaosMeshControlService(config, backend)
    blocked = result(mesh.create_experiment(**request(baseline_gate_token="baseline-b", cleanup_handle="cleanup-episode-e2e-001-r002")))
    assert blocked["error"]["code"] == "EXECUTOR_CONFLICT"
    assert backend.created_manifests == []


def test_concurrent_cross_executor_creates_share_the_ledger_lock(runtime):
    config, mesh_backend = runtime

    class SlowBlade(InMemoryChaosBackend):
        async def create_experiment(self, manifest, kubeconfig):
            await asyncio.sleep(0.01)
            return await super().create_experiment(manifest, kubeconfig)

    blade_backend = SlowBlade(pod_uids={("otel-demo", "checkoutservice-abc123"): "pod-uid-1"})
    blade = ChaosControlService(config, blade_backend)
    mesh = ChaosMeshControlService(config, mesh_backend)

    async def capture(coro):
        try:
            return await coro
        except Exception as exc:  # the test asserts the controlled error below
            return exc

    async def scenario():
        return await asyncio.gather(
            capture(blade.create_experiment(**request())),
            capture(mesh.create_experiment(**request(baseline_gate_token="baseline-b", cleanup_handle="cleanup-episode-e2e-001-r002"))),
        )

    first, second = run(scenario())
    # Cross-process serialization guarantees one winner, not scheduler order.
    outcomes = (first, second)
    assert sum(isinstance(result, dict) and result.get("ok") is True for result in outcomes) == 1
    assert sum(getattr(result, "code", None) == "EXECUTOR_CONFLICT" for result in outcomes) == 1
    assert len(blade_backend.created_manifests) + len(mesh_backend.created_manifests) == 1


def test_mesh_server_exposes_the_same_tool_shape(runtime):
    from mcp_servers.chaos_mesh_control.server import create_server
    config, backend = runtime
    tools = run(create_server(service=ChaosMeshControlService(config, backend)).list_tools())
    assert {item.name for item in tools} == {"chaos_mesh_validate_plan", "chaos_mesh_inventory_run", "chaos_mesh_create_experiment", "chaos_mesh_get_experiment", "chaos_mesh_operation_status", "chaos_mesh_destroy_experiment", "chaos_mesh_recovery_status"}
