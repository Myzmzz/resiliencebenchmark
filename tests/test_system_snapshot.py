from __future__ import annotations

from pathlib import Path

import pytest

from controller.run_contracts import (
    HarnessSelection,
    ProgressionPolicy,
    RunMode,
    RunSpec,
    ScanScope,
)
from controller.system_snapshot import (
    KubectlObservationAdapter,
    RuntimeSnapshot,
    RuntimeTargetSnapshot,
    SnapshotError,
    SnapshotStatus,
    SystemScanner,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT.parent / "benchmark-sources" / "materialized"


def _spec(*, mode: RunMode = RunMode.DRY_RUN, namespace: str = "otel-demo") -> RunSpec:
    return RunSpec(
        request_id="snapshot-request",
        requester="benchmark-admin",
        mode=mode,
        scan=ScanScope(
            application="otel-demo",
            namespace=namespace,
            source_lock_id="otel-demo-2.2.0",
            evidence_roots=["benchmark-app", "benchmark-config", "benchmark-workload"],
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


def test_dry_run_snapshot_uses_locked_source_and_formal_workload_contract() -> None:
    snapshot = SystemScanner(REPO_ROOT, SOURCE_ROOT).scan("run-test", _spec())

    assert snapshot.schema_version == "system-snapshot.v1"
    assert snapshot.source.status is SnapshotStatus.QUALIFIED
    assert snapshot.source.commit == "b74a7bc7bbe66099c61951f42b24dab8b6f02d18"
    assert snapshot.workload.duration_seconds == 600
    assert snapshot.workload.evaluation_window_seconds == 300
    assert snapshot.workload.calibration_status == "qualified"
    assert snapshot.workload.minimum_throughput_rps == pytest.approx(7.240010246537277)
    assert snapshot.runtime.status is SnapshotStatus.NOT_REQUESTED
    assert [item.alias for item in snapshot.evidence_roots] == [
        "benchmark-app",
        "benchmark-config",
        "benchmark-workload",
    ]


def test_execute_snapshot_consumes_normalized_runtime_adapter() -> None:
    class Adapter:
        def scan(self, namespace: str) -> RuntimeSnapshot:
            assert namespace == "otel-demo"
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
                        name="checkoutservice-abc",
                        uid="uid-123",
                        ready=True,
                    )
                ],
            )

    snapshot = SystemScanner(REPO_ROOT, SOURCE_ROOT).scan(
        "run-live",
        _spec(mode=RunMode.EXECUTE),
        runtime_adapter=Adapter(),
    )

    assert snapshot.runtime.status is SnapshotStatus.QUALIFIED
    assert snapshot.runtime.chaosblade_global_count == 0
    assert snapshot.runtime.targets[0].uid == "uid-123"


def test_execute_without_runtime_adapter_is_fail_closed() -> None:
    snapshot = SystemScanner(REPO_ROOT, SOURCE_ROOT).scan(
        "run-live",
        _spec(mode=RunMode.EXECUTE),
    )

    assert snapshot.runtime.status is SnapshotStatus.UNAVAILABLE
    assert "must block" in snapshot.limitations[0]


def test_run_namespace_must_match_trusted_application_registry() -> None:
    with pytest.raises(SnapshotError, match="liveReference"):
        SystemScanner(REPO_ROOT, SOURCE_ROOT).scan(
            "run-wrong-namespace",
            _spec(namespace="default"),
        )


def test_observation_adapter_uses_fixed_service_proxy_queries() -> None:
    requested = []

    def runner(argv: list[str], timeout_seconds: int) -> str:
        requested.append(argv[-1])
        path = argv[-1]
        if "prometheus" in path:
            return '{"data":{"result":[{"value":[1,"42"]}]}}'
        if path.endswith("/api/services"):
            return '{"data":["frontend","checkout","cart"]}'
        if "/api/traces?" in path:
            return '{"data":[{"traceID":"abc"}]}'
        if "loki" in path:
            return '{"data":[{"namespace":"otel-demo"}]}'
        raise AssertionError(path)

    observed = KubectlObservationAdapter(
        Path("/Users/mymz/.kube/coroot-config"),
        runner=runner,
    ).scan("otel-demo")

    assert observed.status is SnapshotStatus.QUALIFIED
    assert observed.prometheus_series == 42
    assert observed.jaeger_trace_count == 1
    assert observed.loki_streams == 1
    assert all("otel-demo" in path or "jaeger" in path for path in requested)
