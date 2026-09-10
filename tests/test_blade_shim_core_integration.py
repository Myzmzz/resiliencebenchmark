"""Real MCP argument binding and Core lifecycle behind the Blade shim.

Only Kubernetes is an explicit in-memory backend. No model, cluster or native
ChaosBlade process is run by these tests.
"""

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from mcp_servers.chaos_control.server import create_server
from mcp_servers.chaos_control.service import ChaosControlService, InMemoryChaosBackend, RuntimeConfig
from stage2_service.bladeai_shim import BladeShim


@pytest.mark.parametrize("target,action,flag,fault_type,canonical", [
    ("network", "delay", "--time", "network-delay", "delay_ms"),
    ("network", "loss", "--percent", "network-loss", "loss_percent"),
    ("cpu", "fullload", "--cpu-percent", "cpu-load", "cpu_percent"),
    ("mem", "load", "--mem-percent", "memory-stress", "mem_percent"),
])
def test_shim_uses_real_mcp_binding_core_approval_and_ledger(tmp_path, monkeypatch, target, action, flag, fault_type, canonical):
    monkeypatch.delenv("RESBENCH_MCP_POLICY_FILE", raising=False)
    monkeypatch.delenv("RESBENCH_MCP_AUDIT_SOCKET", raising=False)
    run_id, pod, uid = "trial-native-core", "cart-a", "uid-current"
    baseline_token, handle = "baseline-private-token", "cleanup-" + "a" * 36
    baseline = tmp_path / "baseline"
    baseline.mkdir(mode=0o700)
    baseline_path = baseline / (hashlib.sha256(baseline_token.encode()).hexdigest() + ".json")
    baseline_path.write_text(json.dumps({
        "passed": True, "run_id": run_id, "namespace": "otel-demo",
        "target_name": pod, "target_uid": uid, "controller_pod_uid": "controller-uid",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    }))
    baseline_path.chmod(0o600)
    approved = {
        "target": {"namespace": "otel-demo", "name": pod, "uid": uid},
        "fault_type": fault_type, "intensity": {canonical: 30},
        "effect_condition": {"metric": "target_latency_ms", "operator": "increase_by_at_least", "threshold": 100},
        "recovery_condition": {"metric": "target_latency_ms", "operator": "within_baseline_delta", "threshold": 50},
        "stop_conditions": ["effect proven or deadline"], "safety_ttl_seconds": 600,
        "effect_observation_seconds": 120, "effect_sustain_seconds": 30,
        "agent_cleanup_seconds": 60, "recovery_observation_seconds": 120,
        "recovery_sustain_seconds": 30,
    }
    decision = tmp_path / "user-decision.json"
    decision.write_text(json.dumps({"approved": True, "approved_plan": approved}))
    decision.chmod(0o600)
    config = RuntimeConfig(
        execute_enabled=True,
        kubeconfig="/controller/private.kubeconfig",
        cleanup_kubeconfig="/controller/finalizer.kubeconfig",
        namespace_allowlist=frozenset({"otel-demo"}),
        controller_token_ref="k8s://controller/sa", controller_pod_uid="controller-uid",
        allowed_fault_types=frozenset({fault_type}), decision_policy="clarify_missing",
        user_decision_file=decision, ledger_dir=tmp_path / "ledger", baseline_ledger_dir=baseline,
        authorized_run_id=run_id, baseline_gate_token=baseline_token, cleanup_handle=handle,
    )
    backend = InMemoryChaosBackend(pod_uids={("otel-demo", pod): uid})
    core = ChaosControlService(config, backend)
    server = create_server(service=core)

    class BoundMcpTools:
        def call(self, tool, arguments):
            if tool == "k8s_get_resource":
                return {"ok": True, "object": {"kind": "Pod", "metadata": {"uid": uid}}}
            # The production MCP server, not this fixture, supplies run id,
            # baseline grant, controller identity and cleanup handle.
            response = asyncio.run(server.call_tool(tool, arguments))
            return dict(response.structured_content)

    # This resembles the actual shared work path, not an invented /loopback/ prefix.
    proxy_path = str(tmp_path / "agent-trials" / "trial" / "bladeai-home" / "proxy.kubeconfig")
    shim = BladeShim(BoundMcpTools(), namespace="otel-demo", state_file=tmp_path / "aliases.json",
                     kubeconfig_path=proxy_path)
    argv = ["create", "k8s", "pod-" + target, action, "--namespace", "otel-demo",
            "--names", pod, "--timeout", "600", flag, "30", "--kubeconfig", proxy_path]
    if target == "network":
        argv.extend(["--interface", "eth0"])
    code, stdout, stderr = shim.run(argv)
    assert code == 0, stderr
    assert len(backend.created_manifests) == 1
    ledger = json.loads((config.ledger_dir / f"{handle}.json").read_text())
    assert ledger["target_uid"] == uid
    assert ledger["intensity"] == {canonical: 30}
    assert ledger["duration_seconds"] == 600
    alias = json.loads(stdout)["result"]
    code, _, stderr = shim.run(["destroy", alias])
    assert code == 0, stderr
    assert not backend.experiments
    assert json.loads((config.ledger_dir / f"{handle}.json").read_text())["cleanup_principal"] == "AGENT_MCP"
