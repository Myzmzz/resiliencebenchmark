from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from stage2_service.capability_loss.factory import (
    CapabilityLossRuntimeFactory,
    OracleTarget,
    QUALIFICATION_SCHEMA,
    metric_result_covers_window,
    oracle_target,
    read_oracle_window,
)
from stage2_service.capability_loss.records import CapabilityLossCase, CapabilityLossVariant, FaultRunningWindow
from stage2_service.capability_policy import CapabilityPolicyRegistry
from stage2_service.contracts import BladeAINativePermissions, PermissionProfile, RuntimeTarget, TrialRuntimeContext
from stage2_service.harness_adapters.base import ToolCall, ToolResult
from stage2_service.platform_ledger import PlatformLedger


NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


class Cleanup:
    def __init__(self, fault_type: str = "network-delay", target_name: str | None = None) -> None:
        self.fault_type = fault_type
        self.target_name = target_name

    def inventory_trial(self, _runtime):
        trial = {
            "ledger_match_count": 1,
            "ever_active": True,
            "target_uid": "uid-actual",
            "fault_type": self.fault_type,
            "started_at": (NOW - timedelta(seconds=30)).isoformat(),
            "ended_at": (NOW - timedelta(seconds=5)).isoformat(),
        }
        if self.target_name is not None:
            # What the ledger records for the executed fault's Pod.
            trial.update({"namespace": "otel-demo", "target_name": self.target_name})
        return {"qualified": True, "trial": trial}


def _context(*, fault_type: str | None = "network-delay") -> TrialRuntimeContext:
    main_fault = {"selection_mode": "agent_strategy"}
    if fault_type is not None:
        main_fault["fault_type"] = fault_type
    return TrialRuntimeContext(
        trial_id="trial-1", episode_id="EPI-OTEL-CART-DEADLINE-001",
        target=RuntimeTarget(namespace="otel-demo", component="agent-selected", name="unbound", uid="unbound"),
        main_fault=main_fault,
        cleanup_handle="cleanup-" + "a" * 36, baseline_capability="b" * 40,
    )


def _qualification(path: Path, *, expires_at=NOW + timedelta(minutes=1)) -> None:
    path.write_text(json.dumps({
        "schema_version": QUALIFICATION_SCHEMA,
        "scope": {"application": "otel-demo", "namespace": "otel-demo", "issued_at": (NOW - timedelta(minutes=1)).isoformat(), "expires_at": expires_at.isoformat()},
        "d7_historical_samples": [
            {"server": "coroot_ro", "target_uid": "uid-actual", "observed_at": (NOW - timedelta(seconds=1)).isoformat(), "record_ref": "private://history/coroot"},
            {"server": "telemetry_ro", "target_uid": "uid-actual", "observed_at": (NOW - timedelta(seconds=1)).isoformat(), "record_ref": "private://history/telemetry"},
        ],
        "d8_canaries": [{"alternative_server": "chaos_mesh_control", "create_verified": True, "destroy_verified": True, "record_ref": "private://canary"}],
    }), encoding="utf-8")
    os.chmod(path, 0o600)


def _factory(tmp_path: Path, *, cleanup_fault_type: str = "network-delay", target_name: str | None = None):
    qualification = tmp_path / "qualification.json"
    _qualification(qualification)
    ledger = PlatformLedger(tmp_path / "ledger")
    policy = CapabilityPolicyRegistry(tmp_path / "policy", ledger=ledger)
    policy.initialize("trial-1", PermissionProfile(profile_id="p0", mcp_servers=("telemetry_ro", "coroot_ro"), bladeai_native=BladeAINativePermissions()))
    cleanup = Cleanup(cleanup_fault_type, target_name=target_name)
    return CapabilityLossRuntimeFactory(cleanup_backend=cleanup, qualification_path=qualification, evidence_root=tmp_path / "evidence", now=lambda: NOW), policy, ledger


def _metric_payload(*, uid: str = "uid-actual", metric: str = "http_server_duration_milliseconds_bucket", values=None) -> dict:
    # A baseline before the fault (window NOW-30s..NOW-5s), one sample while
    # it ran, and one after it.
    values = values if values is not None else [
        [(NOW - timedelta(seconds=40)).timestamp(), "1.0"],
        [(NOW - timedelta(seconds=20)).timestamp(), "3.0"],
        [NOW.timestamp(), "2.0"],
    ]
    return {
        "ok": True,
        "metric": metric,
        "data": {"resultType": "matrix", "result": [
            {"metric": {"__name__": metric, "pod_uid": uid}, "values": values},
        ]},
    }


def _activate_d7(runtime) -> ToolCall:
    primary = ToolCall(
        call_id="primary", tool="telemetry_ro.telemetry_prom_metric_range",
        arguments={"start": int((NOW - timedelta(minutes=1)).timestamp()), "end": int(NOW.timestamp()), "labels": {"pod_uid": "uid-actual"}},
        occurred_at=NOW,
    )
    assert runtime.before_call(primary).allowed is False
    alternative = ToolCall(
        call_id="alternative", tool="coroot_ro.coroot_metrics_range",
        arguments={
            "metric": "http_server_duration_milliseconds_bucket",
            "start": int((NOW - timedelta(minutes=1)).timestamp()),
            "end": int(NOW.timestamp()),
            "labels": {"pod_uid": "uid-actual"},
        },
        occurred_at=NOW,
    )
    assert runtime.before_call(alternative).allowed is True
    return alternative


def test_factory_uses_private_qualified_oracle_uid_and_real_query_scope_for_d7(tmp_path: Path):
    factory, policy, ledger = _factory(tmp_path)
    context = _context()
    runtime = factory.build("trial-1", "D7", "A", context, policy, ledger)
    alternative = _activate_d7(runtime)
    runtime.after_result(alternative, ToolResult(call_id="alternative", status="completed", payload=_metric_payload(), occurred_at=NOW))

    inputs = factory.finish_inputs(runtime, context, {"fault_effect_verified": True, "evidence_refs": ["oracle://effect"]})

    assert inputs["oracle"]["evidence_covers_fault_window"] is True
    assert inputs["oracle"]["fault_window"] is not None
    assert (tmp_path / "evidence" / "trial-1" / "oracle-fault-window.json").is_file()


def test_d7_accepts_cpu_metric_family_from_trusted_executed_fault_inventory(tmp_path: Path):
    factory, policy, ledger = _factory(tmp_path, cleanup_fault_type="cpu-load")
    context = _context(fault_type=None)
    runtime = factory.build("trial-1", "D7", "A", context, policy, ledger)
    primary = ToolCall(call_id="primary", tool="telemetry_ro.telemetry_prom_metric_range", arguments={}, occurred_at=NOW)
    assert runtime.before_call(primary).allowed is False
    alternative = ToolCall(
        call_id="alternative", tool="coroot_ro.coroot_metrics_range",
        arguments={"metric": "container_cpu_usage_seconds_total"},
        occurred_at=NOW,
    )
    assert runtime.before_call(alternative).allowed is True
    runtime.after_result(
        alternative,
        ToolResult(
            call_id="alternative", status="completed",
            payload=_metric_payload(metric="container_cpu_usage_seconds_total"),
            occurred_at=NOW,
        ),
    )

    inputs = factory.finish_inputs(runtime, context, {"fault_effect_verified": True})

    assert inputs["oracle"]["evidence_covers_fault_window"] is True


def test_d7_accepts_telemetry_ro_prometheus_matrix_shape_when_it_is_the_alternative(tmp_path: Path):
    factory, policy, ledger = _factory(tmp_path)
    context = _context()
    runtime = factory.build("trial-1", "D7", "A", context, policy, ledger)
    primary = ToolCall(call_id="primary", tool="coroot_ro.coroot_metrics_range", arguments={}, occurred_at=NOW)
    assert runtime.before_call(primary).allowed is False
    alternative = ToolCall(call_id="alternative", tool="telemetry_ro.telemetry_prom_metric_range", arguments={}, occurred_at=NOW)
    assert runtime.before_call(alternative).allowed is True
    payload = _metric_payload()
    payload["resultType"] = payload["data"]["resultType"]
    payload["result"] = payload["data"]["result"]
    payload.pop("data")
    runtime.after_result(alternative, ToolResult(call_id="alternative", status="completed", payload=payload, occurred_at=NOW))

    assert factory.finish_inputs(runtime, context, {"fault_effect_verified": True})["oracle"]["evidence_covers_fault_window"] is True


@pytest.mark.parametrize("payload", [
    {"ok": True},
    _metric_payload(values=[]),
    _metric_payload(values=[[(NOW - timedelta(seconds=40)).timestamp(), "NaN"], [NOW.timestamp(), "1"]]),
    _metric_payload(uid="wrong-uid"),
    _metric_payload(values=[[(NOW - timedelta(seconds=60)).timestamp(), "1"], [(NOW - timedelta(seconds=31)).timestamp(), "2"]]),
    _metric_payload(values=[["not-a-timestamp", "1"], [NOW.timestamp(), "2"]]),
])
def test_d7_rejects_request_only_or_insufficient_returned_metric_evidence(tmp_path: Path, payload: dict):
    factory, policy, ledger = _factory(tmp_path)
    context = _context()
    runtime = factory.build("trial-1", "D7", "A", context, policy, ledger)
    alternative = _activate_d7(runtime)
    runtime.after_result(alternative, ToolResult(call_id="alternative", status="completed", payload=payload, occurred_at=NOW))

    inputs = factory.finish_inputs(runtime, context, {"fault_effect_verified": True})

    assert inputs["oracle"]["evidence_covers_fault_window"] is False


def test_d7_rejects_unidentified_metric_even_with_uid_and_window(tmp_path: Path):
    factory, policy, ledger = _factory(tmp_path)
    context = _context()
    runtime = factory.build("trial-1", "D7", "A", context, policy, ledger)
    runtime.before_call(ToolCall(call_id="primary", tool="telemetry_ro.telemetry_prom_metric_range", arguments={}, occurred_at=NOW))
    alternative = ToolCall(call_id="alternative", tool="coroot_ro.coroot_metrics_range", arguments={}, occurred_at=NOW)
    assert runtime.before_call(alternative).allowed is True
    runtime.after_result(
        alternative,
        ToolResult(
            call_id="alternative", status="completed",
            payload={"ok": True, "data": {"resultType": "matrix", "result": [
                {"metric": {"pod_uid": "uid-actual"}, "values": [
                    [(NOW - timedelta(seconds=40)).timestamp(), "1.0"],
                    [NOW.timestamp(), "1.0"],
                ]},
            ]}},
            occurred_at=NOW,
        ),
    )

    inputs = factory.finish_inputs(runtime, context, {"fault_effect_verified": True})

    assert inputs["oracle"]["evidence_covers_fault_window"] is False


@pytest.mark.parametrize("metric", [
    "kube_pod_status_ready",
    "up",
    "kube_pod_container_status_restarts_total",
    "kube_pod_info",
])
def test_d7_rejects_metadata_or_liveness_metric_even_when_oracle_effect_is_true(tmp_path: Path, metric: str):
    factory, policy, ledger = _factory(tmp_path)
    context = _context(fault_type="network-delay")
    runtime = factory.build("trial-1", CapabilityLossCase.D7, CapabilityLossVariant.A, context, policy, ledger)
    runtime.before_call(ToolCall(call_id="primary", tool="telemetry_ro.telemetry_prom_metric_range", arguments={}, occurred_at=NOW))
    alternative = ToolCall(
        call_id="alternative", tool="coroot_ro.coroot_metrics_range",
        arguments={"metric": metric},
        occurred_at=NOW,
    )
    assert runtime.before_call(alternative).allowed is True
    runtime.after_result(
        alternative,
        ToolResult(
            call_id="alternative", status="completed",
            payload=_metric_payload(metric=metric),
            occurred_at=NOW,
        ),
    )

    inputs = factory.finish_inputs(runtime, context, {"fault_effect_verified": True, "evidence_refs": ["oracle://effect"]})
    finished = runtime.finish(oracle=inputs["oracle"], finalizer=inputs["finalizer"], agent_assessment={"effect_assessment": "verified"})

    assert inputs["oracle"]["evidence_covers_fault_window"] is False
    assert finished.score.raw_capability_score != 3
    assert finished.score.final_score == 0


def test_d7_unknown_executed_fault_type_keeps_alternative_metric_unverified(tmp_path: Path):
    factory, policy, ledger = _factory(tmp_path, cleanup_fault_type="disk-io")
    context = _context(fault_type=None)
    runtime = factory.build("trial-1", "D7", "A", context, policy, ledger)
    alternative = _activate_d7(runtime)
    runtime.after_result(alternative, ToolResult(call_id="alternative", status="completed", payload=_metric_payload(), occurred_at=NOW))

    assert factory.finish_inputs(runtime, context, {"fault_effect_verified": True})["oracle"]["evidence_covers_fault_window"] is False


@pytest.mark.parametrize("tool,payload", [
    ("coroot_ro.coroot_traces_find", {"ok": True, "traces": [{"timestamp": NOW.timestamp(), "pod_uid": "uid-actual"}]}),
    ("coroot_ro.coroot_logs_range", {"ok": True, "entries": [{"timestamp": NOW.timestamp(), "pod_uid": "uid-actual"}]}),
])
def test_d7_trace_and_log_outputs_are_not_invented_into_metric_evidence(tmp_path: Path, tool: str, payload: dict):
    factory, policy, ledger = _factory(tmp_path)
    context = _context()
    runtime = factory.build("trial-1", "D7", "A", context, policy, ledger)
    _activate_d7(runtime)
    alternative = ToolCall(call_id="secondary", tool=tool, arguments={"start": 0, "end": int(NOW.timestamp())}, occurred_at=NOW)
    assert runtime.before_call(alternative).allowed is True
    runtime.after_result(alternative, ToolResult(call_id="secondary", status="completed", payload=payload, occurred_at=NOW))

    assert factory.finish_inputs(runtime, context, {"fault_effect_verified": True})["oracle"]["evidence_covers_fault_window"] is False


def test_factory_does_not_turn_agent_claim_or_oracle_effect_into_missing_alternative_query_evidence(tmp_path: Path):
    factory, policy, ledger = _factory(tmp_path)
    context = _context()
    runtime = factory.build("trial-1", CapabilityLossCase.D7, CapabilityLossVariant.B, context, policy, ledger)
    primary = ToolCall(call_id="primary", tool="telemetry_ro.telemetry_prom_metric_range", arguments={"start": 0, "end": 1, "labels": {"pod_uid": "uid-actual"}}, occurred_at=NOW)
    runtime.before_call(primary)
    alternative = ToolCall(call_id="alternative", tool="coroot_ro.coroot_metrics_range", arguments={"start": 0, "end": 1}, occurred_at=NOW)
    runtime.before_call(alternative)
    runtime.after_result(alternative, ToolResult(call_id="alternative", status="completed", payload={"ok": True}, occurred_at=NOW))

    inputs = factory.finish_inputs(runtime, context, {"fault_effect_verified": True, "evidence_refs": ["oracle://effect"]})
    finished = runtime.finish(oracle=inputs["oracle"], finalizer=inputs["finalizer"], agent_assessment={"effect_assessment": "verified"})

    assert inputs["oracle"]["evidence_covers_fault_window"] is False
    assert finished.facts.d7 is not None and finished.facts.d7.evidence_covers_fault_window is False
    assert finished.score.final_score == 0


def test_expired_qualification_is_not_reused_as_a_precheck(tmp_path: Path):
    factory, policy, ledger = _factory(tmp_path)
    _qualification(factory.qualification_path, expires_at=NOW - timedelta(seconds=1))
    runtime = factory.build("trial-1", "D7", "A", _context(), policy, ledger)
    call = ToolCall(call_id="query", tool="telemetry_ro.telemetry_prom_metric_range", arguments={}, occurred_at=NOW)

    result = runtime.before_call(call)

    assert result.allowed is False
    assert result.payload["error"]["code"] == "CASE_INVALID"


def test_symlink_or_nonprivate_qualification_is_not_trusted(tmp_path: Path):
    factory, policy, ledger = _factory(tmp_path)
    target = tmp_path / "target.json"
    _qualification(target)
    factory.qualification_path.unlink()
    factory.qualification_path.symlink_to(target)
    runtime = factory.build("trial-1", "D7", "A", _context(), policy, ledger)

    result = runtime.before_call(ToolCall(call_id="query", tool="telemetry_ro.telemetry_prom_metric_range", arguments={}, occurred_at=NOW))

    assert result.allowed is False
    assert result.payload["error"]["code"] == "CASE_INVALID"


def test_symlink_private_parent_is_not_trusted(tmp_path: Path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    qualification = private / "qualification.json"
    _qualification(qualification)
    alias = tmp_path / "private-alias"
    alias.symlink_to(private, target_is_directory=True)
    ledger = PlatformLedger(tmp_path / "ledger-parent")
    policy = CapabilityPolicyRegistry(tmp_path / "policy-parent", ledger=ledger)
    policy.initialize("trial-1", PermissionProfile(profile_id="p0", mcp_servers=("telemetry_ro", "coroot_ro"), bladeai_native=BladeAINativePermissions()))
    factory = CapabilityLossRuntimeFactory(
        cleanup_backend=Cleanup(), qualification_path=alias / "qualification.json",
        evidence_root=tmp_path / "evidence-parent", now=lambda: NOW,
    )
    runtime = factory.build("trial-1", "D7", "A", _context(), policy, ledger)

    result = runtime.before_call(ToolCall(call_id="query", tool="telemetry_ro.telemetry_prom_metric_range", arguments={}, occurred_at=NOW))

    assert result.allowed is False
    assert result.payload["error"]["code"] == "CASE_INVALID"


# --- Pod identity and sample times as real backends return them (2026-09-24) ---
# Fleet D7 runs showed telemetry_ro series labelled only namespace+pod and
# Coroot series labelled by container_id: no UID label, so the UID-only rule
# could never be met, and Coroot answers a range reaching past "now" with null
# or carried-forward points.

WINDOW = FaultRunningWindow(
    started_at=NOW - timedelta(seconds=30), ended_at=NOW - timedelta(seconds=5), oracle_record_ref="private://oracle",
)
LEDGER_POD = OracleTarget(namespace="otel-demo", uid="uid-actual", name="cart-7ffd4d6f-x24ls")


def _at_offset(seconds: int) -> float:
    return (NOW + timedelta(seconds=seconds)).timestamp()


def _matrix(labels: dict, values=None) -> dict:
    values = values if values is not None else [[_at_offset(-40), "1.0"], [_at_offset(-20), "3.0"]]
    return {"ok": True, "data": {"resultType": "matrix", "result": [{"metric": labels, "values": values}]}}


@pytest.mark.parametrize("tool,metric,labels", [
    # telemetry_ro aggregates by (namespace, pod): no UID label at all.
    ("telemetry_prom_metric_range", "container_cpu_usage_seconds_total",
     {"namespace": "otel-demo", "pod": "cart-7ffd4d6f-x24ls"}),
    # Coroot container series name the Pod inside container_id.
    ("coroot_metrics_range", "container_resources_cpu_usage_seconds_total",
     {"__name__": "container_resources_cpu_usage_seconds_total", "app_id": "/k8s/otel-demo/cart",
      "container_id": "/k8s/otel-demo/cart-7ffd4d6f-x24ls/cart"}),
    # Raw cAdvisor series embed the UID in the cgroup path (systemd driver).
    ("telemetry_prom_query_range", "container_cpu_usage_seconds_total",
     {"id": "/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-poduid_actual.slice"}),
])
def test_d7_identifies_the_fault_pod_by_the_labels_real_backends_return(tool: str, metric: str, labels: dict):
    assert metric_result_covers_window(tool, {"metric": metric}, _matrix(labels), LEDGER_POD, WINDOW, "cpu-load", observed_at=NOW)


@pytest.mark.parametrize("labels", [
    {"namespace": "otel-demo", "pod": "cart-7ffd4d6f-other"},
    {"namespace": "otel-demo-02", "pod": "cart-7ffd4d6f-x24ls"},
    {"container_id": "/k8s/otel-demo/cart-7ffd4d6f-x24ls-b/cart"},
    {"app_id": "/k8s/otel-demo/cart"},
])
def test_d7_rejects_series_of_another_pod_or_without_pod_identity(labels: dict):
    assert not metric_result_covers_window(
        "coroot_metrics_range", {"metric": "container_resources_cpu_usage_seconds_total"},
        _matrix(labels), LEDGER_POD, WINDOW, "cpu-load", observed_at=NOW,
    )


def test_d7_pod_name_counts_only_when_it_certainly_belongs_to_the_fault_uid():
    unnamed = OracleTarget(namespace="otel-demo", uid="uid-actual")
    labels = {"namespace": "otel-demo", "pod": "cart-7ffd4d6f-x24ls"}

    assert not metric_result_covers_window(
        "telemetry_prom_metric_range", {"metric": "container_cpu_usage_seconds_total"},
        _matrix(labels), unnamed, WINDOW, "cpu-load", observed_at=NOW,
    )
    # The inventory fell back to the bound name while the bound UID is another Pod's.
    assert oracle_target(uid="uid-actual", namespace="otel-demo", inventory_name="cart-a",
                         bound_name="cart-a", bound_uid="uid-other").name is None
    # A name that differs from the bound one can only be the ledger's own.
    assert oracle_target(uid="uid-actual", namespace="otel-demo", inventory_name="cart-b",
                         bound_name="cart-a", bound_uid="uid-other").name == "cart-b"
    assert oracle_target(uid="uid-actual", namespace="otel-demo", inventory_name="cart-a",
                         bound_name="cart-a", bound_uid="uid-actual").name == "cart-a"
    assert oracle_target(uid="uid-actual", namespace="otel-demo", inventory_name="unbound",
                         bound_name="unbound", bound_uid="unbound").name is None


@pytest.mark.parametrize("values,observed_offset,covered", [
    # A null point (a Coroot gap) no longer discards the finite ones.
    ([[_at_offset(-40), "1"], [_at_offset(-35), None], [_at_offset(-20), "3"]], 0, True),
    # No baseline: every sample lies inside the fault window.
    ([[_at_offset(-25), "3"], [_at_offset(-10), "3"]], 0, False),
    # Baseline and post-fault samples only: nothing observed while it ran.
    ([[_at_offset(-40), "1"], [_at_offset(0), "1"]], 0, False),
    # Asked 5 s into the fault: a later-stamped point was carried forward, not observed.
    ([[_at_offset(-40), "1"], [_at_offset(-20), "3"]], -25, False),
    ([[_at_offset(-40), "1"], [_at_offset(-28), "3"], [_at_offset(-20), "3"]], -25, True),
])
def test_d7_counts_finite_samples_observed_by_query_time_from_baseline_into_the_window(values, observed_offset: int, covered: bool):
    labels = {"namespace": "otel-demo", "pod": "cart-7ffd4d6f-x24ls"}
    observed_at = NOW + timedelta(seconds=observed_offset)

    assert metric_result_covers_window(
        "telemetry_prom_metric_range", {"metric": "container_cpu_usage_seconds_total"},
        _matrix(labels, values), LEDGER_POD, WINDOW, "cpu-load", observed_at=observed_at,
    ) is covered


def test_d7_factory_accepts_real_prometheus_series_of_the_ledger_pod_and_records_the_pod(tmp_path: Path):
    factory, policy, ledger = _factory(tmp_path, cleanup_fault_type="cpu-load", target_name="cart-7ffd4d6f-x24ls")
    context = _context(fault_type=None)
    runtime = factory.build("trial-1", "D7", "A", context, policy, ledger)
    primary = ToolCall(call_id="primary", tool="coroot_ro.coroot_metrics_range", arguments={}, occurred_at=NOW)
    assert runtime.before_call(primary).allowed is False
    alternative = ToolCall(
        call_id="alternative", tool="telemetry_ro.telemetry_prom_metric_range",
        arguments={"metric": "container_cpu_usage_seconds_total", "labels": {"pod": "cart-7ffd4d6f-x24ls"}},
        occurred_at=NOW,
    )
    assert runtime.before_call(alternative).allowed is True
    payload = _matrix({"namespace": "otel-demo", "pod": "cart-7ffd4d6f-x24ls"})
    runtime.after_result(alternative, ToolResult(call_id="alternative", status="completed", payload=payload, occurred_at=NOW))

    inputs = factory.finish_inputs(runtime, context, {"fault_effect_verified": True})

    assert inputs["oracle"]["evidence_covers_fault_window"] is True
    assert inputs["oracle"]["evidence_refs"][0] == "platform://tool-result/alternative"
    record = json.loads((tmp_path / "evidence" / "trial-1" / "oracle-fault-window.json").read_text(encoding="utf-8"))
    assert (record["namespace"], record["target_name"], record["fault_type"]) == ("otel-demo", "cart-7ffd4d6f-x24ls", "cpu-load")
    window, loaded = read_oracle_window(tmp_path / "evidence", "trial-1")
    assert loaded == record
    assert window.oracle_record_ref == "private://capability-loss/trial-1/oracle-fault-window.json"
    assert read_oracle_window(tmp_path / "evidence", "../trial-1") is None
    assert read_oracle_window(tmp_path / "evidence", "trial-2") is None
