"""Observed-state regressions grounded in the 2026-09-06 old-cluster canary."""
import json
from pathlib import Path

import pytest

from mcp_servers.chaos_core.backends.chaos_mesh import _record_from_resource
from mcp_servers.chaos_mesh_control.service import ChaosMeshControlService, RuntimeConfig

FIXTURE = Path(__file__).parent / "fixtures/chaos_mesh/network_delay_injected.json"


def injected():
    return json.loads(FIXTURE.read_text())


def test_real_network_chaos_observation_is_running_not_desired_run():
    resource = injected()
    assert resource["status"]["experiment"]["desiredPhase"] == "Run"
    record = _record_from_resource(resource)
    assert record.phase == "Running"
    assert not record.terminal


@pytest.mark.parametrize("change", ["desired_only", "no_records", "no_condition", "zero_injections", "wrong_target", "pending_record", "paused", "deleting"])
def test_desired_state_and_incomplete_or_stale_observations_never_claim_running(change):
    resource = injected()
    status = resource["status"]
    record = status["experiment"]["containerRecords"][0]
    if change == "desired_only": resource["status"] = {"experiment": {"desiredPhase": "Run"}}
    elif change == "no_records": status["experiment"]["containerRecords"] = []
    elif change == "no_condition": status["conditions"] = []
    elif change == "zero_injections": record["injectedCount"] = 0
    elif change == "wrong_target": record["id"] = "otel-demo/another-pod"
    elif change == "pending_record": record["phase"] = "Not Injected"
    elif change == "paused": status["conditions"].append({"type": "Paused", "status": "True"})
    elif change == "deleting": resource["metadata"]["deletionTimestamp"] = "2026-09-06T04:24:55Z"
    assert _record_from_resource(resource).phase != "Running"


def test_stop_is_recovering_until_actual_recovery_records_confirm_completion():
    resource = injected()
    status = resource["status"]
    status["experiment"]["desiredPhase"] = "Stop"
    assert _record_from_resource(resource).phase == "Recovering"
    status["conditions"].append({"type": "AllRecovered", "status": "True"})
    assert not _record_from_resource(resource).terminal
    record = status["experiment"]["containerRecords"][0]
    record.update(phase="Recovered", recoveredCount=1)
    assert _record_from_resource(resource).phase == "Completed"
    assert _record_from_resource(resource).terminal


@pytest.mark.parametrize("actually_injected", [False, True])
def test_observed_mesh_phase_drives_shared_core_fault_window(monkeypatch, actually_injected):
    resource = injected()
    if not actually_injected:
        resource["status"] = {"experiment": {"desiredPhase": "Run"}}
    record = _record_from_resource(resource)
    ledger = {"run_id": record.run_id, "target_uid": record.target_uid, "namespace": record.namespace,
              "experiment_name": record.name, "cleanup_handle": "qualification-replay", "ever_active": False}
    service = ChaosMeshControlService(RuntimeConfig())
    writes = []
    monkeypatch.setattr(service, "_read_ledger", lambda handle: dict(ledger))
    monkeypatch.setattr(service, "_write_ledger", lambda handle, value: writes.append(value))
    observed = service._observe_fault_window(ledger, record)
    assert bool(observed.get("started_at")) is actually_injected
    assert observed["ever_active"] is actually_injected
    assert bool(writes) is actually_injected
