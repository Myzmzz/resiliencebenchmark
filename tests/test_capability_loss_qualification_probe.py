"""No-cluster tests for the D7/D8 capability-loss qualification probe.

Kubernetes, Coroot, Prometheus, Chaos Mesh and ChaosBlade are fakes at the
transport or backend boundary; target binding, the observation services, both
controlled-execution services and the qualification loader are production code.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_servers.chaos_core.backends.chaos_mesh import UID_FENCE_LABEL, InMemoryChaosMeshBackend
from mcp_servers.chaos_core.backends.chaosblade import InMemoryChaosBackend
from mcp_servers.chaos_core.contracts import ChaosControlError
from mcp_servers.coroot_ro.service import HttpResponse as CorootResponse
from mcp_servers.coroot_ro.service import _coroot_labels_string
from mcp_servers.telemetry_ro.service import HttpResponse as TelemetryResponse
from stage2_service.capability_loss import qualification_probe as probe
from stage2_service.capability_loss.factory import (
    QUALIFICATION_SCHEMA,
    CapabilityLossRuntimeFactory,
    _trusted_private_regular_file,
)
from stage2_service.contracts import RuntimeTarget
from stage2_service.preparation import ApplicationTrafficCapabilityIssuer, KubernetesTrialPreparer
from stage2_service.runtime_lock import RuntimeLock


NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
NAMESPACE = "otel-demo"
CONTROLLER = ("resiliencebenchmark-system", "stage2-0")
MESH = "chaos_mesh_control"
BLADE = "chaos_control"


@pytest.fixture(autouse=True)
def _hermetic_observation_environment(monkeypatch):
    """Only the fake platform environment may configure the observation clients."""

    for name in (
        "RESBENCH_COROOT_SESSION_COOKIE", "RESBENCH_JAEGER_URL", "RESBENCH_LOKI_URL",
        "RESBENCH_TELEMETRY_ALLOW_RAW_QUERIES", "RESBENCH_TELEMETRY_TIMEOUT_SECONDS",
        "RESBENCH_JAEGER_ALLOWED_SERVICES", "RESBENCH_WORKLOAD_STATS_URL", "STAGE2_SUBSTITUTION_QUALIFICATION_FILE",
    ):
        monkeypatch.delenv(name, raising=False)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


async def _no_sleep(_seconds: float) -> None:
    return None


class FakeCoreApi:
    """The kubernetes CoreV1Api calls used by target binding."""

    def __init__(self) -> None:
        self.pods: dict[str, SimpleNamespace] = {}

    def add_pod(self, name: str, uid: str, *, component: str) -> SimpleNamespace:
        pod = SimpleNamespace(
            metadata=SimpleNamespace(
                name=name, uid=uid, creation_timestamp=NOW - timedelta(hours=1), deletion_timestamp=None,
                labels={"app.kubernetes.io/component": component, "opentelemetry.io/name": component},
            ),
            status=SimpleNamespace(conditions=[SimpleNamespace(type="Ready", status="True")]),
            spec=SimpleNamespace(containers=[SimpleNamespace(name=component)]),
        )
        self.pods[name] = pod
        return pod

    def list_namespaced_pod(self, namespace: str, label_selector: str | None = None, field_selector: str | None = None):
        items = list(self.pods.values()) if namespace == NAMESPACE else []
        if label_selector:
            key, _, value = label_selector.partition("=")
            items = [pod for pod in items if pod.metadata.labels.get(key) == value]
        if field_selector:
            items = [pod for pod in items if pod.metadata.name == field_selector.partition("=")[2]]
        return SimpleNamespace(items=items)


class FakeCorootTransport:
    """Coroot panel and series APIs that return CPU data only for Pods that have it."""

    def __init__(self) -> None:
        self.pods_with_data: set[str] = set()

    async def get_json(self, *, base_url, path, params, headers, timeout_seconds, max_bytes):
        if path.endswith("/panel/data"):
            query = json.loads(params["query"])["source"]["metrics"]["queries"][0]["query"]
        else:
            query = params["match[]"]
        container_id = re.search(r'container_id="([^"]+)"', query).group(1)
        labels = {"__name__": probe.COROOT_METRIC, "container_id": container_id}
        has_data = container_id.split("/")[3] in self.pods_with_data
        if path.endswith("/panel/data"):
            start_ms, end_ms, step_ms = params["from"], params["to"], 60_000
            points = (end_ms - start_ms) // step_ms + 1
            series = [{"name": _coroot_labels_string(labels), "data": [0.25] * points}] if has_data else []
            return CorootResponse(200, {"chart": {"ctx": {"from": start_ms, "to": end_ms, "step": step_ms}, "series": series}})
        return CorootResponse(200, {"status": "success", "data": [labels] if has_data else []})


class FakeTelemetryTransport:
    """Prometheus query_range returning cAdvisor CPU series only for Pods that have data."""

    def __init__(self) -> None:
        self.pods_with_data: dict[str, str] = {}

    async def get_json(self, *, base_url, path, params, timeout_seconds, max_bytes):
        pod = re.search(r'pod="([^"]+)"', params["query"]).group(1)
        result = []
        if pod in self.pods_with_data:
            cgroup = self.pods_with_data[pod].replace("-", "_")
            result = [{
                "metric": {
                    "__name__": probe.TELEMETRY_METRIC, "namespace": NAMESPACE, "pod": pod, "container": "cart",
                    "id": f"/kubepods.slice/kubepods-burstable-pod{cgroup}.slice/cri-containerd-1.scope",
                },
                "values": [[second, "0.5"] for second in range(params["start"], params["end"] + 1, params["step"])],
            }]
        return TelemetryResponse(200, {"status": "success", "data": {"resultType": "matrix", "result": result}})


class PendingChaosMeshBackend(InMemoryChaosMeshBackend):
    """Chaos Mesh accepted the object but never reported AllInjected."""

    async def create_experiment(self, manifest, kubeconfig):
        record = dataclasses.replace(await super().create_experiment(manifest, kubeconfig), phase="Pending")
        self.experiments[(record.namespace, record.name)] = record
        return record


class AmbiguousCreateChaosMeshBackend(InMemoryChaosMeshBackend):
    """The NetworkChaos was created but the create response was lost."""

    async def create_experiment(self, manifest, kubeconfig):
        await super().create_experiment(manifest, kubeconfig)
        raise ChaosControlError("KUBECTL_FAILED", "kubectl timed out after create", next_step="reconcile")


class StuckChaosMeshBackend(InMemoryChaosMeshBackend):
    """Deleting the NetworkChaos never takes effect, e.g. behind a stuck finalizer."""

    async def delete_experiment(self, namespace, name, kubeconfig):
        self.deleted.append((namespace, name))


class PendingChaosBladeBackend(InMemoryChaosBackend):
    """The ChaosBlade operator accepted the CR but never reported it Running."""

    async def create_experiment(self, manifest, kubeconfig):
        record = dataclasses.replace(await super().create_experiment(manifest, kubeconfig), phase="Initialized")
        self.experiments[(record.namespace, record.name)] = record
        return record


class AmbiguousCreateChaosBladeBackend(InMemoryChaosBackend):
    """The ChaosBlade CR was created but the create response was lost."""

    async def create_experiment(self, manifest, kubeconfig):
        await super().create_experiment(manifest, kubeconfig)
        raise ChaosControlError("KUBECTL_FAILED", "kubectl timed out after create", next_step="reconcile")


class HealthyTraffic:
    def current(self) -> dict[str, Any]:
        return {"application_owned": True, "load_generator_ready": True, "traffic_observed": True}

    def record_baseline(self, trial_id: str, evidence) -> None:
        return None


@dataclass
class World:
    clock: Clock
    core: FakeCoreApi
    mesh: InMemoryChaosMeshBackend
    blade: InMemoryChaosBackend
    coroot: FakeCorootTransport
    telemetry: FakeTelemetryTransport
    private_root: Path
    output: Path
    lock: RuntimeLock
    platform: probe.ProbePlatform

    def add_cart(self, name: str, uid: str) -> None:
        pod = self.core.add_pod(name, uid, component="cart")
        self.mesh.pod_uids[(NAMESPACE, name)] = uid
        self.blade.pod_uids[(NAMESPACE, name)] = uid
        # One label dict, so the Chaos Mesh fence is visible to the Kubernetes reads.
        self.mesh.pod_labels[(NAMESPACE, name)] = pod.metadata.labels
        self.coroot.pods_with_data.add(name)
        self.telemetry.pods_with_data[name] = uid

    def replace_cart(self, old: str, new: str, uid: str) -> None:
        self.core.pods.pop(old)
        self.mesh.pod_uids.pop((NAMESPACE, old))
        self.mesh.pod_labels.pop((NAMESPACE, old))
        self.blade.pod_uids.pop((NAMESPACE, old))
        self.add_cart(new, uid)

    def backend(self, server: str):
        return self.mesh if server == MESH else self.blade

    def use_backend(self, server: str, backend_type: type) -> None:
        """Swap one executor's backend, keeping the Pods (and shared labels) it sees."""

        replacement = backend_type(pod_uids=self.backend(server).pod_uids)
        if server == MESH:
            replacement.pod_labels.update(self.mesh.pod_labels)
            self.mesh = replacement
            self.platform = dataclasses.replace(self.platform, chaos_mesh_backend=replacement)
        else:
            self.blade = replacement
            self.platform = dataclasses.replace(self.platform, chaosblade_backend=replacement)

    def evidence(self, record_ref: str) -> dict[str, Any]:
        name = record_ref.removeprefix(probe.RECORD_REF_PREFIX)
        return json.loads((self.private_root / probe.EVIDENCE_RELATIVE_DIR / name).read_text(encoding="utf-8"))


def _world(tmp_path: Path) -> World:
    root = tmp_path.resolve()
    private_root = root / "private"
    private_root.mkdir(mode=0o700)
    baseline_dir = private_root / "chaos-control" / "baseline"
    core = FakeCoreApi()
    mesh = InMemoryChaosMeshBackend(pod_uids={CONTROLLER: "controller-pod-uid"})
    blade = InMemoryChaosBackend(pod_uids={CONTROLLER: "controller-pod-uid"})
    coroot = FakeCorootTransport()
    telemetry = FakeTelemetryTransport()
    resolve_target, read_pod, live_pod_uids = probe.kubernetes_target_access(
        KubernetesTrialPreparer(core, capability_issuer=None), NAMESPACE,
    )
    issuer = ApplicationTrafficCapabilityIssuer(
        ledger_dir=baseline_dir, controller_pod_uid="controller-pod-uid", traffic_evidence=HealthyTraffic(),
    )
    environment = {
        "RESBENCH_COROOT_URL": "http://coroot.test",
        "RESBENCH_COROOT_PROJECT_ID": "proj1",
        "RESBENCH_COROOT_ALLOWED_NAMESPACE": NAMESPACE,
        "RESBENCH_COROOT_ALLOWED_SERVICES": "frontend,cart",
        "RESBENCH_COROOT_ALLOW_ANONYMOUS_READ": "true",
        "RESBENCH_COROOT_TIMEOUT_SECONDS": "10",
        "RESBENCH_PROMETHEUS_URL": "http://prometheus.test",
        "RESBENCH_TELEMETRY_ALLOWED_NAMESPACES": NAMESPACE,
        # telemetry_ro fails closed without it, exactly as in runtime_factory's MCP environment.
        "RESBENCH_JAEGER_ALLOWED_SERVICES": "frontend,frontend-proxy,checkout,cart,payment,shipping",
        "RESBENCH_CHAOS_EXECUTE_ENABLED": "true",
        "RESBENCH_CHAOS_KUBECONFIG": "/tmp/executor.kubeconfig",
        "RESBENCH_CHAOS_CLEANUP_KUBECONFIG": "/tmp/finalizer.kubeconfig",
        "RESBENCH_CHAOS_NAMESPACE_ALLOWLIST": NAMESPACE,
        "RESBENCH_CHAOS_CONTROLLER_TOKEN_REF": "k8s://resiliencebenchmark-system/serviceaccount/resbench-stage2-controller",
        "RESBENCH_CHAOS_CONTROLLER_POD_UID": "controller-pod-uid",
        "RESBENCH_CHAOS_CONTROLLER_POD_NAMESPACE": CONTROLLER[0],
        "RESBENCH_CHAOS_CONTROLLER_POD_NAME": CONTROLLER[1],
        "RESBENCH_CHAOS_BASELINE_LEDGER_DIR": str(baseline_dir),
        "RESBENCH_CHAOS_LEDGER_DIR": str(private_root / "chaos-control" / "active"),
    }

    def issue_baseline(run_id: str, target: probe.TargetPod) -> str:
        runtime_target = RuntimeTarget(namespace=target.namespace, component=target.component, name=target.name, uid=target.uid)
        return issuer.issue(run_id, namespace=NAMESPACE, target=runtime_target)

    platform = probe.ProbePlatform(
        namespace=NAMESPACE,
        private_root=private_root,
        resolve_target=resolve_target,
        read_pod=read_pod,
        live_pod_uids=live_pod_uids,
        issue_baseline=issue_baseline,
        coroot_application_id=lambda target: f"proj1:{target.namespace}:Deployment:{target.component}",
        mcp_environment=environment,
        coroot_transport=coroot,
        telemetry_transport=telemetry,
        chaos_mesh_backend=mesh,
        chaosblade_backend=blade,
    )
    world = World(
        clock=Clock(NOW), core=core, mesh=mesh, blade=blade, coroot=coroot, telemetry=telemetry,
        private_root=private_root, output=private_root / probe.OUTPUT_FILENAME,
        lock=RuntimeLock(root / "run" / "stage2-active-run.lock"), platform=platform,
    )
    world.add_cart("cart-a", "uid-a")
    return world


def _run(world: World, args: list[str], capsys) -> tuple[int, dict[str, Any]]:
    code = probe.main(
        [*args, "--output", str(world.output)],
        platform_factory=lambda _namespace: world.platform, runtime_lock=world.lock,
        clock=world.clock, sleep=_no_sleep,
    )
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _load(world: World, *, now: datetime | None = None):
    factory = CapabilityLossRuntimeFactory(
        cleanup_backend=None, qualification_path=world.output,
        evidence_root=world.private_root / "capability-loss", now=lambda: now or world.clock(),
    )
    return factory, factory._qualification(SimpleNamespace(target=SimpleNamespace(namespace=NAMESPACE)))


def test_generated_file_is_accepted_by_the_factory_and_satisfies_every_precheck(tmp_path: Path, capsys):
    world = _world(tmp_path)

    code, summary = _run(world, [], capsys)

    assert code == 0 and summary["ok"] is True and summary["failures"] == []
    assert summary["d8_servers"] == [MESH, BLADE]
    factory, qualification = _load(world)
    assert qualification is not None
    assert qualification["scope"] == {
        "application": "otel-demo", "namespace": "otel-demo",
        "issued_at": NOW.isoformat(), "expires_at": (NOW + timedelta(hours=24)).isoformat(),
    }
    assert factory._d7_precheck(qualification, "coroot_ro", "uid-a").valid
    assert factory._d7_precheck(qualification, "telemetry_ro", "uid-a").valid
    assert not factory._d7_precheck(qualification, "coroot_ro", "uid-other").valid
    # D8 is bidirectional: whichever executor the Agent validates on first, the other has a canary.
    assert factory._d8_precheck(qualification, MESH).valid
    assert factory._d8_precheck(qualification, BLADE).valid
    assert set(summary["prechecks"]) >= {f"d8:{MESH}", f"d8:{BLADE}"} and all(summary["prechecks"].values())
    assert summary["loader_accepted"] is True
    # Each record_ref resolves to private evidence of what was observed.
    samples = {sample["server"]: sample for sample in qualification["d7_historical_samples"]}
    coroot = world.evidence(samples["coroot_ro"]["record_ref"])
    assert coroot["accepted"] is True
    assert coroot["attempts"][0]["request"]["labels"] == {"container_id": "/k8s/otel-demo/cart-a/cart"}
    telemetry = world.evidence(samples["telemetry_ro"]["record_ref"])
    assert telemetry["attempts"][0]["response"]["matching_series"][0]["pod_uid_in_cgroup_id"] is True
    canaries = {item["alternative_server"]: world.evidence(item["record_ref"]) for item in qualification["d8_canaries"]}
    for server, executor in ((MESH, "chaos_mesh"), (BLADE, "chaosblade")):
        assert canaries[server]["executor_id"] == executor
        assert canaries[server]["create_verified"] is True and canaries[server]["destroy_verified"] is True
        assert canaries[server]["polls"][0]["phase"] == "Running"
    # Each canary was its executor's smallest fault and left nothing behind.
    manifest = world.mesh.created_manifests[0]
    assert manifest["kind"] == "NetworkChaos"
    assert manifest["spec"]["delay"] == {"latency": "1ms"} and manifest["spec"]["duration"] == "15s"
    experiment = world.blade.created_manifests[0]["spec"]["experiments"][0]
    assert (experiment["scope"], experiment["target"], experiment["action"]) == ("pod", "cpu", "fullload")
    assert {"name": "cpu-percent", "value": ["1"]} in experiment["matchers"]
    assert {"name": "names", "value": ["cart-a"]} in experiment["matchers"]
    assert world.mesh.experiments == {} and world.blade.experiments == {}
    assert world.mesh.deleted and world.blade.deleted
    assert UID_FENCE_LABEL not in world.core.pods["cart-a"].metadata.labels


def test_d7_refresh_replaces_samples_of_a_replaced_pod_and_keeps_the_valid_canaries(tmp_path: Path, capsys):
    world = _world(tmp_path)
    assert _run(world, [], capsys)[0] == 0
    world.replace_cart("cart-a", "cart-b", "uid-b")
    world.clock.advance(hours=2)

    code, summary = _run(world, ["--d7"], capsys)

    assert code == 0 and summary["existing_file"] == "merged" and summary["d8_servers"] == []
    factory, qualification = _load(world)
    assert {(item["server"], item["target_uid"]) for item in qualification["d7_historical_samples"]} == {
        ("coroot_ro", "uid-b"), ("telemetry_ro", "uid-b"),
    }
    assert {item["reason"] for item in summary["dropped"]} == {"target_uid_not_live"}
    assert not factory._d7_precheck(qualification, "coroot_ro", "uid-a").valid
    assert factory._d7_precheck(qualification, "telemetry_ro", "uid-b").valid
    assert factory._d8_precheck(qualification, MESH).valid and factory._d8_precheck(qualification, BLADE).valid
    # The carried canaries still expire 24 h after they ran, not after this refresh.
    assert qualification["scope"]["expires_at"] == (NOW + timedelta(hours=24)).isoformat()


@pytest.mark.parametrize(("server", "backend_type"), [
    (MESH, PendingChaosMeshBackend),
    (MESH, AmbiguousCreateChaosMeshBackend),
    (BLADE, PendingChaosBladeBackend),
    (BLADE, AmbiguousCreateChaosBladeBackend),
])
def test_failing_canary_still_destroys_the_object_and_records_no_canary(tmp_path: Path, capsys, server, backend_type):
    world = _world(tmp_path)
    world.use_backend(server, backend_type)

    code, summary = _run(world, ["--d8-server", server], capsys)

    assert code == 1 and summary["ok"] is False and summary["d8_servers"] == [server]
    assert summary["d8_canaries"] == [] and summary["alerts"] == []
    assert [failure["probe"] for failure in summary["failures"]] == [f"d8:{server}"]
    backend = world.backend(server)
    assert backend.deleted, "destroy must run even though the canary failed"
    assert backend.experiments == {}
    assert world.backend(BLADE if server == MESH else MESH).created_manifests == []
    assert UID_FENCE_LABEL not in world.core.pods["cart-a"].metadata.labels
    evidence = world.evidence(summary["failures"][0]["evidence"])
    assert evidence["create_verified"] is False
    assert evidence["verified_facts"]["absent_for_finalizer"] is True
    assert evidence["verified_facts"]["nothing_left_behind"] is True
    factory, qualification = _load(world)
    assert factory._d8_precheck(qualification, server).reason == "alternative_canary_missing"


@pytest.mark.parametrize(("failing", "kept", "backend_type"), [
    (BLADE, MESH, PendingChaosBladeBackend),
    (MESH, BLADE, PendingChaosMeshBackend),
])
def test_a_failed_canary_drops_only_its_own_executor_entry(tmp_path: Path, capsys, failing, kept, backend_type):
    world = _world(tmp_path)
    code, first = _run(world, ["--d8"], capsys)
    assert code == 0 and first["modes"] == ["d8"] and first["d8_servers"] == [MESH, BLADE]
    old = {item["alternative_server"]: item["record_ref"] for item in first["d8_canaries"]}
    world.use_backend(failing, backend_type)
    world.clock.advance(minutes=30)

    code, summary = _run(world, ["--d8-server", failing], capsys)

    assert code == 1
    assert summary["dropped"] == [{"kind": "d8_canary", "record_ref": old[failing], "reason": "latest_canary_failed"}]
    factory, qualification = _load(world)
    assert [item["record_ref"] for item in qualification["d8_canaries"]] == [old[kept]]
    assert factory._d8_precheck(qualification, kept).valid
    assert factory._d8_precheck(qualification, failing).reason == "alternative_canary_missing"
    assert qualification["scope"]["expires_at"] == (NOW + timedelta(hours=24)).isoformat()


def test_second_canary_is_skipped_while_the_first_one_may_remain(tmp_path: Path, capsys):
    world = _world(tmp_path)
    world.use_backend(MESH, StuckChaosMeshBackend)

    code, summary = _run(world, ["--d8"], capsys)

    assert code == 1 and summary["d8_canaries"] == []
    assert len(summary["alerts"]) == 1 and "may remain" in summary["alerts"][0]
    reasons = {failure["probe"]: failure["reason"] for failure in summary["failures"]}
    assert reasons[f"d8:{MESH}"].startswith("destroy could not be verified")
    assert reasons[f"d8:{BLADE}"].startswith("skipped")
    assert world.blade.created_manifests == [], "no second fault may be stacked on the target Pod"


def test_file_and_directories_are_owner_only_even_if_the_parent_was_group_readable(tmp_path: Path, capsys):
    world = _world(tmp_path)
    os.chmod(world.private_root, 0o755)

    code, _summary = _run(world, ["--d7"], capsys)

    assert code == 0
    assert stat.S_IMODE(world.private_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(world.output.stat().st_mode) == 0o600
    assert _trusted_private_regular_file(world.output)
    evidence_dir = world.private_root / probe.EVIDENCE_RELATIVE_DIR
    assert stat.S_IMODE(evidence_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(evidence_dir.parent.stat().st_mode) == 0o700
    assert {stat.S_IMODE(path.stat().st_mode) for path in evidence_dir.iterdir()} == {0o600}
    assert not list(world.private_root.glob(f".{probe.OUTPUT_FILENAME}.*.tmp"))


def test_scope_expires_after_the_ttl_and_expired_canaries_are_not_carried(tmp_path: Path, capsys):
    world = _world(tmp_path)
    assert _run(world, ["--ttl-hours", "1"], capsys)[0] == 0
    assert _load(world, now=NOW + timedelta(minutes=59))[1] is not None
    assert _load(world, now=NOW + timedelta(hours=1))[1] is None
    world.clock.advance(hours=2)

    code, summary = _run(world, ["--d7", "--ttl-hours", "1"], capsys)

    assert code == 0
    assert {(item["kind"], item["reason"]) for item in summary["dropped"]} == {
        ("d7_historical_sample", "expired"), ("d8_canary", "expired"),
    }
    factory, qualification = _load(world)
    assert qualification["d8_canaries"] == []
    for server in (MESH, BLADE):
        assert factory._d8_precheck(qualification, server).reason == "alternative_canary_missing"
    assert qualification["scope"]["expires_at"] == (NOW + timedelta(hours=3)).isoformat()


def test_hand_written_entries_without_probe_provenance_are_dropped(tmp_path: Path, capsys):
    world = _world(tmp_path)
    world.output.write_text(json.dumps({
        "schema_version": QUALIFICATION_SCHEMA,
        "scope": {"application": "otel-demo", "namespace": "otel-demo",
                  "issued_at": NOW.isoformat(), "expires_at": (NOW + timedelta(days=30)).isoformat()},
        "d7_historical_samples": [{"server": "coroot_ro", "target_uid": "uid-a", "observed_at": NOW.isoformat(), "record_ref": "private://hand"}],
        "d8_canaries": [{"alternative_server": MESH, "create_verified": True, "destroy_verified": True, "record_ref": "private://hand-canary"}],
    }), encoding="utf-8")
    os.chmod(world.output, 0o600)

    code, summary = _run(world, ["--d7"], capsys)

    assert code == 0
    assert {item["reason"] for item in summary["dropped"]} == {"unknown_provenance"}
    _factory, qualification = _load(world)
    assert "private://hand" not in {item["record_ref"] for item in qualification["d7_historical_samples"]}
    assert qualification["d8_canaries"] == []


def test_d7_sample_is_written_only_when_the_server_returned_data_for_the_target(tmp_path: Path, capsys):
    world = _world(tmp_path)
    world.coroot.pods_with_data.clear()

    code, summary = _run(world, ["--d7"], capsys)

    assert code == 1
    assert [failure["probe"] for failure in summary["failures"]] == ["d7:coroot_ro"]
    assert world.evidence(summary["failures"][0]["evidence"])["accepted"] is False
    factory, qualification = _load(world)
    assert [item["server"] for item in qualification["d7_historical_samples"]] == ["telemetry_ro"]
    assert not factory._d7_precheck(qualification, "coroot_ro", "uid-a").valid


def test_probe_refuses_to_run_while_a_stage2_campaign_holds_the_runtime_lock(tmp_path: Path, capsys):
    world = _world(tmp_path)
    built: list[str] = []

    with world.lock.acquire(owner="api:running-campaign"):
        code = probe.main(
            ["--output", str(world.output)], platform_factory=lambda namespace: built.append(namespace),
            runtime_lock=RuntimeLock(world.lock.path), clock=world.clock, sleep=_no_sleep,
        )

    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 1 and summary["ok"] is False and "no Stage-2 campaign is active" in summary["error"]
    assert built == [] and not world.output.exists()
    assert world.mesh.created_manifests == [] and world.blade.created_manifests == []


def test_dry_run_lists_both_canaries_without_building_or_writing_anything(tmp_path: Path, capsys):
    output = tmp_path / "qualification.json"
    lock = RuntimeLock(tmp_path / "stage2-active-run.lock")
    built: list[str] = []

    code = probe.main(["--dry-run", "--output", str(output)], platform_factory=lambda namespace: built.append(namespace), runtime_lock=lock)

    summary = json.loads(capsys.readouterr().out.strip())
    assert code == 0 and summary["dry_run"] is True and summary["actions_performed"] == "none"
    assert [read["server"] for read in summary["d7_reads"]] == ["coroot_ro", "telemetry_ro"]
    canaries = {item["server"]: item for item in summary["d8_canaries"]}
    assert list(canaries) == [MESH, BLADE]
    assert (canaries[MESH]["fault_type"], canaries[MESH]["intensity"]) == ("network-delay", {"delay_ms": 1})
    assert (canaries[BLADE]["fault_type"], canaries[BLADE]["intensity"]) == ("cpu-load", {"cpu_percent": 1})
    assert {item["duration_seconds"] for item in summary["d8_canaries"]} == {15}
    # --d8-server narrows D8 to one executor and, alone, implies --d8 without D7.
    assert probe.main(["--dry-run", "--d8-server", BLADE, "--output", str(output)], runtime_lock=lock) == 0
    narrowed = json.loads(capsys.readouterr().out.strip())
    assert narrowed["modes"] == ["d8"] and narrowed["d7_reads"] == []
    assert [item["server"] for item in narrowed["d8_canaries"]] == [BLADE]
    assert built == [] and not output.exists() and not lock.path.exists()
