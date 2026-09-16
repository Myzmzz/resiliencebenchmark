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
from mcp_servers.chaos_core.backends.chaosblade import _record_from_resource
from stage2_service.foreign_fault_observer import ForeignFaultObserver
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


def _agent_created_record(*, name="blade-own", target_name="cart-1", fault_type="cpu-load", phase="Running"):
    """A CR an Agent created with its own client: no platform labels at all.

    ``run_id``, ``target_uid`` and ``owner`` come from labels only
    ``chaos_control`` writes, so they are empty here.  The namespace, Pod name
    and fault type still parse -- from the CR's own matchers and its
    target/action pair -- which is exactly what the attribution matches on.
    """
    return ExperimentRecord(
        name=name,
        namespace="otel-demo",
        run_id="",
        target_name=target_name,
        target_uid="",
        fault_type=fault_type,
        phase=phase,
        owner=None,
        labels={},
        raw={"kind": "ChaosBlade"},
    )


def _agent_fault_services(records):
    class Backend:
        def __init__(self, records):
            self.records = list(records)

        async def list_experiments(self, _kubeconfig, _namespace):
            return list(self.records)

        async def delete_experiment(self, namespace, name, _kubeconfig):
            # Record the call, then behave like kubectl: the CR is gone.
            self.deleted.append((namespace, name))
            self.records = [record for record in self.records if record.name != name]

    class Service:
        def __init__(self, records):
            self.backend = Backend(records)
            self.backend.deleted = []

        def _iter_cleanup_ledger_paths(self):
            return []

    return Service(records), Service([])


def _agent_fault_runtime():
    return TrialRuntimeContext(
        trial_id="trial-1",
        episode_id="EPI-OTEL-CART-DEADLINE-001",
        target=RuntimeTarget(namespace="otel-demo", component="cart", name="cart-1", uid="uid-1"),
        main_fault={"fault_type": "cpu-load"},
        cleanup_handle="cleanup-" + "a" * 36,
        baseline_capability="b" * 40,
    )


def test_agent_created_fault_on_the_trial_target_counts_as_the_main_fault_when_enabled(
    tmp_path, monkeypatch
):
    """BladeAI round six, 2026-09-15: four real cpu-load experiments, no credit.

    The Agent injects with its own ServiceAccount, so nothing reaches the
    private ledger and MAIN_FAULT_ACTIVE could never pass.  With the switch on,
    an experiment acting on the Trial's own target counts -- and keeps counting
    after the CR is gone, because BladeAI's own ``--timeout`` reaps it long
    before finalization reads the inventory.
    """
    monkeypatch.setenv("STAGE2_FOREIGN_FAULT_ATTRIBUTION", "on")
    blade, mesh = _agent_fault_services([_agent_created_record()])
    cleanup = DirectChaosCleanup(blade, mesh, tmp_path / "kubeconfig")
    runtime = _agent_fault_runtime()

    live = cleanup.inventory_trial(runtime)

    assert live["trial"]["ever_active"] is True
    assert live["trial"]["fault_attribution"] == "observed_foreign"
    assert live["trial"]["experiment_name"] == "blade-own"
    assert live["trial"]["resource_absent"] is False
    # The uid comes from the runtime, never from the CR, because finalization
    # compares it against the approved plan's target.
    assert live["trial"]["target_uid"] == "uid-1"

    blade.backend.records = []
    after_cleanup = cleanup.inventory_trial(runtime)

    assert after_cleanup["trial"]["ever_active"] is True
    assert after_cleanup["trial"]["resource_absent"] is True
    assert after_cleanup["trial"]["ended_at"]


def test_agent_created_fault_is_ignored_without_the_switch(tmp_path):
    """Default behaviour is unchanged: the ledger stays the only evidence."""
    blade, mesh = _agent_fault_services([_agent_created_record()])
    cleanup = DirectChaosCleanup(blade, mesh, tmp_path / "kubeconfig")

    snapshot = cleanup.inventory_trial(_agent_fault_runtime())

    assert snapshot["trial"]["ever_active"] is False
    assert snapshot["trial"]["fault_attribution"] == "ledger"
    assert snapshot["foreign_active_count"] == 1


def test_agent_created_fault_on_another_pod_is_not_the_trials_main_fault(
    tmp_path, monkeypatch
):
    """Replicas share one cluster, so a neighbour's experiment must not count."""
    monkeypatch.setenv("STAGE2_FOREIGN_FAULT_ATTRIBUTION", "on")
    blade, mesh = _agent_fault_services([_agent_created_record(target_name="cart-9")])
    cleanup = DirectChaosCleanup(blade, mesh, tmp_path / "kubeconfig")

    snapshot = cleanup.inventory_trial(_agent_fault_runtime())

    assert snapshot["trial"]["ever_active"] is False
    assert snapshot["trial"]["fault_attribution"] == "ledger"


def _label_selected_agent_cr(*, name="blade-by-label", hit_pod="cart-1"):
    """A CR shaped like round eight's r4: chosen by label, no ``names`` matcher.

    The operator still hit exactly one Pod and recorded it in the status, in the
    cri form it really writes (namespace/node/pod/container/id/runtime).
    """
    return {
        "kind": "ChaosBlade",
        "metadata": {"name": name, "labels": {}},
        "spec": {
            "experiments": [
                {
                    "scope": "pod",
                    "target": "cpu",
                    "action": "fullload",
                    "matchers": [
                        {"name": "namespace", "value": ["otel-demo"]},
                        {"name": "names", "value": []},
                        {"name": "labels", "value": ["app.kubernetes.io/component=cart"]},
                    ],
                }
            ]
        },
        "status": {
            "phase": "Running",
            "expStatuses": [
                {
                    "scope": "pod",
                    "target": "cpu",
                    "action": "fullload",
                    "success": True,
                    "resStatuses": [
                        {
                            "id": "eae172a30c56acef",
                            "identifier": f"otel-demo/node-1/{hit_pod}/cart/2363c7f7269a/docker",
                            "kind": "pod",
                            "state": "Success",
                            "success": True,
                        }
                    ],
                }
            ],
        },
    }


def test_label_selected_agent_fault_is_attributed_from_the_pod_it_actually_hit(
    tmp_path, monkeypatch
):
    """Round eight r4, 2026-09-16: selected by label, never attributed.

    Its ``names`` matcher was empty, so the parsed Pod name was "" and the
    observer's 152 polls never matched.  The Pod the operator really hit is in
    the CR status, and that is what the attribution must compare.
    """
    monkeypatch.setenv("STAGE2_FOREIGN_FAULT_ATTRIBUTION", "on")
    record = _record_from_resource(_label_selected_agent_cr())
    blade, mesh = _agent_fault_services([record])
    cleanup = DirectChaosCleanup(blade, mesh, tmp_path / "kubeconfig")

    snapshot = cleanup.inventory_trial(_agent_fault_runtime())

    assert record.target_name == "cart-1"
    assert snapshot["trial"]["ever_active"] is True
    assert snapshot["trial"]["fault_attribution"] == "observed_foreign"
    assert snapshot["trial"]["experiment_name"] == "blade-by-label"


def test_controller_deletes_the_attributed_agent_fault_it_still_sees_running(
    tmp_path, monkeypatch
):
    """The empty-ledger branch used to report "verified absent" with the fault live.

    That false answer let the platform retry straight into the same running
    experiment.  Now the credited experiment is deleted by its recorded name and
    absence is re-read from the cluster, attributed to the Controller.
    """
    monkeypatch.setenv("STAGE2_FOREIGN_FAULT_ATTRIBUTION", "on")
    blade, mesh = _agent_fault_services([_agent_created_record()])
    cleanup = DirectChaosCleanup(blade, mesh, tmp_path / "kubeconfig")
    runtime = _agent_fault_runtime()

    result = cleanup.cleanup_owned(runtime)

    assert blade.backend.deleted == [("otel-demo", "blade-own")]
    assert mesh.backend.deleted == []
    assert result["verified_absent"] is True
    assert result["principal"] == "CONTROLLER_FALLBACK"
    assert result["deleted_foreign_experiment"] == "blade-own"


def test_controller_never_deletes_an_agent_fault_without_the_switch(tmp_path):
    """Default behaviour is unchanged: no attribution, so nothing foreign is touched."""
    blade, mesh = _agent_fault_services([_agent_created_record()])
    cleanup = DirectChaosCleanup(blade, mesh, tmp_path / "kubeconfig")

    result = cleanup.cleanup_owned(_agent_fault_runtime())

    assert blade.backend.deleted == []
    assert result["idempotent"] is True


def test_controller_leaves_a_neighbours_agent_fault_alone(tmp_path, monkeypatch):
    """A fault on another Pod was never credited to this Trial, so it is not deleted."""
    monkeypatch.setenv("STAGE2_FOREIGN_FAULT_ATTRIBUTION", "on")
    blade, mesh = _agent_fault_services([_agent_created_record(target_name="cart-9")])
    cleanup = DirectChaosCleanup(blade, mesh, tmp_path / "kubeconfig")

    result = cleanup.cleanup_owned(_agent_fault_runtime())

    assert blade.backend.deleted == []
    assert result["idempotent"] is True


def test_foreign_fault_observer_reports_the_first_attributed_poll():
    """The observer only reads; it is what makes somebody look while it runs."""

    class Backend:
        def __init__(self):
            self.calls = 0

        def inventory_trial(self, _runtime):
            self.calls += 1
            return {
                "trial": {
                    "fault_attribution": "observed_foreign",
                    "experiment_name": "blade-own",
                    "fault_type": "cpu-load",
                    "target_name": "cart-1",
                }
            }

    backend = Backend()
    observer = ForeignFaultObserver(backend, poll_seconds=0.01)
    emitted = []
    observer.arm(
        trial_id="trial-1",
        runtime=SimpleNamespace(),
        emit=lambda kind, payload: emitted.append(kind),
    )
    time.sleep(0.08)
    result = observer.finish()

    assert result["observed"] is True
    assert result["experiment_name"] == "blade-own"
    assert result["polls"] >= 1
    # Announced once, on the poll that first saw it.
    assert emitted == ["foreign_fault_observed"]


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


def test_reap_expired_runs_the_owning_executors_timer_cleanup_now(tmp_path):
    # The chaos MCP watchdog only runs while an Agent session is connected
    # (2026-09-10 L0xC0), so the condition monitor reaps expired leases itself.
    import json

    class Service:
        def __init__(self, paths):
            self.paths = paths
            self.reaped = 0

        def _iter_cleanup_ledger_paths(self):
            return self.paths

        async def cleanup_expired_leases(self):
            self.reaped += 1
            return {"ok": True, "inspected": 1, "cleaned": [handle], "errors": []}

    handle = "cleanup-" + "b" * 36
    ledger = tmp_path / f"{handle}.json"
    ledger.write_text(json.dumps({"executor_id": "chaosblade", "cleanup_handle": handle}), encoding="utf-8")
    blade, mesh = Service([ledger]), Service([])
    cleanup = DirectChaosCleanup(blade, mesh, tmp_path / "kubeconfig")

    result = cleanup.reap_expired(handle)

    assert result == {"ok": True, "inspected": 1, "cleaned": [handle], "errors": [], "executor_id": "chaosblade"}
    assert (blade.reaped, mesh.reaped) == (1, 0)


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
