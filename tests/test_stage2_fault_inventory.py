from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

from mcp_servers.chaos_control.service import ChaosControlService
from mcp_servers.chaos_core.service import ExperimentRecord, InMemoryChaosBackend, RuntimeConfig
from mcp_servers.chaos_mesh_control.service import ChaosMeshControlService, InMemoryChaosMeshBackend

from stage2_service.fault_inventory import (
    DualExecutorFaultInventory,
    resource_from_experiment,
    snapshot_for_trial,
)
from stage2_service.contracts import RuntimeTarget, TrialRuntimeContext
from stage2_service.condition_monitor import ConditionRecoveryMonitor
from stage2_service.runtime_factory import DirectChaosCleanup
from stage2_service.reset_policy import ResetTier, classify_reset_policy


def _record(*, name: str, run_id: str, owner: str = "chaos_control", phase: str = "Running"):
    return ExperimentRecord(
        name=name,
        namespace="otel-demo",
        run_id=run_id,
        target_name="cart-1",
        target_uid="uid-1",
        fault_type="network-delay",
        phase=phase,
        owner=owner,
        labels={"benchmark.owner": owner, "benchmark.run_id": run_id},
        raw={"kind": "NetworkChaos"},
    )


def test_unified_inventory_counts_only_exact_trial_owned_resources_for_cleanup():
    ours = resource_from_experiment(
        "chaos_mesh", _record(name="ours", run_id="trial-1"), ledger_matched=True
    )
    foreign = resource_from_experiment(
        "chaosblade", _record(name="foreign", run_id="trial-2"), ledger_matched=True
    )

    snapshot = snapshot_for_trial(
        trial_id="trial-1",
        resources=(ours, foreign),
        qualified_executors=("chaosblade", "chaos_mesh"),
    )

    assert snapshot["qualified"] is True
    assert snapshot["owned_active_count"] == 1
    assert snapshot["foreign_active_count"] == 1
    assert snapshot["owned_resources_absent"] is False
    assert snapshot["inventory_clear"] is False
    assert next(item for item in snapshot["resources"] if item["name"] == "ours")["owned_by_trial"] is True
    assert next(item for item in snapshot["resources"] if item["name"] == "foreign")["owned_by_trial"] is False


def test_inventory_read_failure_is_not_an_empty_inventory():
    snapshot = snapshot_for_trial(
        trial_id="trial-1",
        resources=(),
        qualified_executors=("chaosblade",),
        unavailable_executors=("chaos_mesh",),
    )

    assert snapshot["qualified"] is False
    assert snapshot["mode"] == "incomplete"
    assert snapshot["owned_resources_absent"] is False
    assert snapshot["inventory_clear"] is False


def test_label_without_ledger_match_is_not_owned_even_when_run_id_matches():
    resource = resource_from_experiment(
        "chaosblade", _record(name="unreconciled", run_id="trial-1"), ledger_matched=False
    )

    snapshot = snapshot_for_trial(
        trial_id="trial-1", resources=(resource,), qualified_executors=("chaosblade",)
    )

    assert snapshot["owned_active_count"] == 0
    assert snapshot["foreign_active_count"] == 1


def test_foreign_or_incomplete_inventory_blocks_reset_even_if_business_is_healthy():
    decision = classify_reset_policy(
        {
            "main_fault_ever_active": True,
            "fault_absent": True,
            "business_recovery_verified": True,
            "foreign_active_faults": True,
            "fault_inventory_qualified": True,
        }
    )

    assert decision.tier is ResetTier.T3_FULL_REINSTALL
    assert decision.verified is False
    assert "FOREIGN_OR_UNOBSERVED_FAULT" in decision.reason_codes


def test_dual_executor_inventory_lists_real_backend_records_and_marks_a_failed_crd_read():
    class Backend:
        def __init__(self, records=(), fail=False):
            self.records = records
            self.fail = fail

        async def list_experiments(self, _kubeconfig, _namespace):
            if self.fail:
                raise RuntimeError("CRD unavailable")
            return list(self.records)

    inventory = DualExecutorFaultInventory(
        services={
            "chaosblade": SimpleNamespace(backend=Backend((_record(name="blade", run_id="trial-1"),))),
            "chaos_mesh": SimpleNamespace(backend=Backend(fail=True)),
        },
        kubeconfig="/controller/kubeconfig",
        ledger_matcher=lambda executor, record: executor == "chaosblade" and record.name == "blade",
        trial_facts=lambda _trial, _records: {
            "ever_active": True,
            "resource_absent": False,
            "target_name": "cart-1",
            "target_uid": "uid-1",
            "fault_type": "network-delay",
        },
    )

    snapshot = asyncio.run(inventory.inventory_trial("trial-1", "otel-demo"))

    assert snapshot["qualified"] is False
    assert snapshot["unavailable_executors"] == ["chaos_mesh"]
    assert snapshot["owned_active_count"] == 1
    assert snapshot["trial"]["ever_active"] is True


def test_direct_cleanup_uses_exact_ledger_and_never_treats_terminal_cr_as_absent(
    tmp_path,
):
    class Backend:
        def __init__(self, records):
            self.records = records

        async def list_experiments(self, _kubeconfig, _namespace):
            return self.records

    class Service:
        def __init__(self, executor_id, records, paths):
            self.executor_id = executor_id
            self.backend = Backend(records)
            self.paths = paths
            self.destroyed = []

        def _iter_cleanup_ledger_paths(self):
            return self.paths

        async def destroy_experiment(self, *, cleanup_handle, kubeconfig, principal="AGENT_MCP"):
            self.destroyed.append((cleanup_handle, kubeconfig, principal))
            return {"verified_absent": True}

    handle = "cleanup-" + "a" * 36
    ledger = tmp_path / f"{handle}.json"
    ledger.write_text(
        __import__("json").dumps(
            {
                "executor_id": "chaosblade",
                "cleanup_handle": handle,
                "experiment_name": "ours",
                "namespace": "otel-demo",
                "run_id": "trial-1",
                "target_uid": "uid-1",
                "target_name": "cart-1",
                "fault_type": "network-delay",
                "ever_active": True,
                "state": "active",
            }
        ),
        encoding="utf-8",
    )
    blade = Service("chaosblade", [_record(name="ours", run_id="trial-1", phase="Completed")], [ledger])
    mesh = Service("chaos_mesh", [_record(name="foreign", run_id="other")], [])
    runtime = TrialRuntimeContext(
        trial_id="trial-1",
        episode_id="EPI-OTEL-CART-DEADLINE-001",
        target=RuntimeTarget(namespace="otel-demo", component="cart", name="cart-1", uid="uid-1"),
        main_fault={"fault_type": "network-delay"},
        cleanup_handle=handle,
        baseline_capability="b" * 40,
    )
    cleanup = DirectChaosCleanup(blade, mesh, tmp_path / "kubeconfig")

    before = cleanup.inventory_trial(runtime)
    result = cleanup.cleanup_owned(runtime)

    assert before["owned_present_count"] == 1
    assert before["owned_resources_absent"] is False
    assert before["foreign_active_count"] == 1
    assert blade.destroyed == [(handle, str(tmp_path / "kubeconfig"), "CONTROLLER_FALLBACK")]
    assert mesh.destroyed == []
    assert result["executor_id"] == "chaosblade"


def test_direct_cleanup_supplies_condition_monitor_and_writes_controller_principal(tmp_path):
    handle = "cleanup-" + "b" * 36
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir(mode=0o700)
    ledger_path = ledger_dir / f"{handle}.json"
    ledger_path.write_text(
        json.dumps(
            {
                "executor_id": "chaosblade", "cleanup_handle": handle,
                "experiment_name": "owned", "namespace": "otel-demo", "run_id": "trial-1",
                "target_name": "cart-1", "target_uid": "uid-1", "fault_type": "network-delay",
                "duration_seconds": 60, "intensity": {"delay_ms": 300},
                "state": "active", "ever_active": True,
            }
        ), encoding="utf-8"
    )
    ledger_path.chmod(0o600)
    report_only = tmp_path / "report-only.json"
    report_only.write_text(json.dumps({"report_only": True}), encoding="utf-8")
    config = RuntimeConfig(
        execute_enabled=True,
        kubeconfig=str(tmp_path / "kubeconfig"),
        cleanup_kubeconfig=str(tmp_path / "kubeconfig"),
        namespace_allowlist=frozenset({"otel-demo"}), ledger_dir=ledger_dir,
        user_decision_file=report_only,
    )
    backend = InMemoryChaosBackend([_record(name="owned", run_id="trial-1")])
    blade = ChaosControlService(config, backend)
    mesh = ChaosMeshControlService(config, InMemoryChaosMeshBackend())
    cleanup = DirectChaosCleanup(blade, mesh, tmp_path / "kubeconfig")

    class Workload:
        def baseline(self, _trial):
            return {"target_latency_ms": 1}

        def current(self):
            return {"target_latency_ms": 100}

    monitor = ConditionRecoveryMonitor(Workload(), cleanup, poll_seconds=0.01)
    monitor.arm(
        trial_id="trial-1", cleanup_handle=handle,
        plan={
            "effect_condition": {"metric": "target_latency_ms", "operator": "increase_by_at_least", "threshold": 1},
            "effect_observation_seconds": 1, "effect_sustain_seconds": 0,
            "agent_cleanup_seconds": 0,
        }, emit=lambda *_args: None,
    )
    deadline = time.monotonic() + 1
    while not monitor.snapshot().get("controller_fallback_used") and time.monotonic() < deadline:
        time.sleep(0.01)
    result = monitor.finish()
    written = json.loads(ledger_path.read_text(encoding="utf-8"))

    assert result["controller_fallback_used"] is True
    assert result["controller_cleanup"]["verified_absent"] is True
    assert written["cleanup_principal"] == "CONTROLLER_FALLBACK"
    assert backend.deleted == [("otel-demo", "owned")]
