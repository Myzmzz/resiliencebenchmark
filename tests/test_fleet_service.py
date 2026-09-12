"""Fleet service: guards, planning, batch lifecycle and result export.

No cluster and no Controller are contacted; the kubectl runner and the
Controller client are replaced by fakes that answer like the real ones.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fleet_service.api import create_app
from fleet_service.contracts import BatchRequest, FleetConfig
from fleet_service.controller_client import ControllerError
from fleet_service.guard import FleetGuardError, assert_destroyable, assert_operable_namespace
from fleet_service.kube import KubeClient
from fleet_service.manifests import slot_manifests
from fleet_service.provisioner import Provisioner
from fleet_service.scheduler import BatchDispatcher, build_run_request, classify_failure, plan_batch
from fleet_service.store import FleetStore


CONFIG = {
    "replicas": 3,
    "namespace_prefix": "otel-demo",
    "controller_image": "registry.example/resbench-stage2:controller@sha256:" + "a" * 64,
    "agent_image": "registry.example/resbench-stage2:agent@sha256:" + "b" * 64,
    "litellm_image": "registry.example/resbench-litellm:1.92.0@sha256:" + "c" * 64,
    "coroot_project_id": "p1nar0hw",
    "nodes": ["node-a", "node-b"],
    "max_concurrency": 3,
    "source_head": "abc1234",
}


class FakeKubeRunner:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def run(self, argv, *, stdin=None, timeout=120):
        self.calls.append({"argv": list(argv), "stdin": stdin})
        joined = " ".join(argv)
        if "deployments" in argv:
            payload = {"items": [
                {"metadata": {"name": "cart"}, "spec": {"replicas": 1}, "status": {"readyReplicas": 1}},
                {"metadata": {"name": "load-generator"}, "spec": {"replicas": 1}, "status": {"readyReplicas": 1}},
            ]}
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
        if "deployment" in argv:
            payload = {"spec": {"replicas": 1}, "status": {"readyReplicas": 1}}
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
        if "apply" in argv:
            return subprocess.CompletedProcess(argv, 0, "deployment.apps/resbench-stage2-s01\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


class FakeController:
    """One Controller instance bound to one replica namespace."""

    def __init__(self, namespace: str):
        self.namespace = namespace
        self.created: list[dict[str, Any]] = []
        self.stopped: list[str] = []
        self.terminal: dict[str, dict[str, Any]] = {}
        self.submit_error: ControllerError | None = None

    def options(self):
        return {"applications": [{"application": self.namespace, "runnable": True}],
                "gateway_probe": {"status": "complete", "available_models": ["qwen3.8-max"]},
                "capability_loss": {"runnable": True}}

    def autonomy_cases(self):
        return {"levels": [{"level": "L0_COMPLETE_TASK",
                            "copy_ready_prompt": f"请针对 {self.namespace} 的 cart 服务注入 80% CPU 负载，最长持续 300 秒，并验证故障效果和业务恢复。"}]}

    def prompt_variants(self, application, slots):
        assert application == self.namespace
        return {
            "variant_set_id": "pv-" + "0" * 16,
            "variants": [
                {"level": level,
                 "prompt": f"请针对 {application} 的 cart 服务 {level}",
                 "lint": {"passed": True, "violations": []}}
                for level in ("L0", "L1", "L2", "L3", "L4")
            ],
        }

    def create_run(self, body, *, idempotency_key=None):
        if self.submit_error is not None:
            raise self.submit_error
        assert body["application"] == self.namespace, "a run must never leave its replica"
        run_id = f"lxr-{len(self.created):016x}"
        self.created.append({**body, "run_id": run_id, "idempotency_key": idempotency_key})
        return {"run_id": run_id, "task_id": f"stage2-task-{run_id[-16:]}", "status": "QUEUED"}

    def run(self, run_id):
        return self.terminal.get(run_id, {"run_id": run_id, "status": "RUNNING", "terminal": False})

    def score(self, run_id):
        return {"run_id": run_id, "total_score": 0.75}

    def stop_run(self, run_id):
        self.stopped.append(run_id)
        return {"run_id": run_id, "stop_requested": True}

    def tasks(self):
        return {"tasks": [{"task_id": "stage2-task-0123456789abcdef"}]}

    def reset_environment(self, task_id):
        return {"task_id": task_id, "verified": True}


@pytest.fixture
def fleet(tmp_path: Path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    runner = FakeKubeRunner()
    kube = KubeClient(runner=runner)
    controllers: dict[str, FakeController] = {}

    def client_factory(url: str) -> FakeController:
        slot = url.rsplit("/", 1)[-1].split(".")[0].removeprefix("resbench-stage2-")
        namespace = f"otel-demo-{slot.removeprefix('s')}"
        return controllers.setdefault(namespace, FakeController(namespace))

    deploys: list[list[str]] = []

    def deploy_runner(argv):
        deploys.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"result": "applied-ready"}), "")

    provisioner = Provisioner(
        store, kube=kube, repo_root=Path(__file__).resolve().parents[1],
        client_factory=client_factory, deploy_runner=deploy_runner, sleep=lambda _seconds: None,
    )
    dispatcher = BatchDispatcher(store, client_factory=client_factory, poll_seconds=0.01)
    app = create_app(store=store, provisioner=provisioner, dispatcher=dispatcher,
                     client_factory=client_factory)
    client = TestClient(app)
    client.put("/api/v1/fleet/config", json=CONFIG)
    return {
        "client": client, "store": store, "controllers": controllers,
        "dispatcher": dispatcher, "deploys": deploys, "runner": runner,
        "provisioner": provisioner,
    }


def _batch(items, **overrides):
    body = {
        "batch_id": "dx-parallel-20260912-01",
        "cluster": "tencent-2node",
        "max_concurrency": 3,
        "defaults": {"model": "qwen3.8-max", "llm_tag": "qwen3.8-max@dashscope", "duration_seconds": 300},
        "items": items,
    }
    body.update(overrides)
    return body


def _item(index, harness, case="D1", **overrides):
    body = {
        "item_id": f"i-{index:03d}", "test_kind": "Dx", "autonomy_level": "L0",
        "case": case, "harness": harness,
    }
    body.update(overrides)
    return body


# -- guards ----------------------------------------------------------------

def test_only_numbered_replicas_of_this_prefix_are_operable():
    assert assert_operable_namespace("otel-demo", "otel-demo-07") == "otel-demo-07"
    for namespace in ("otel-demo", "observability", "coroot", "chaos-mesh", "kube-system",
                      "resiliencebenchmark-system", "otel-demo-prod", "otel-demoX"):
        with pytest.raises(FleetGuardError):
            assert_operable_namespace("otel-demo", namespace)


def test_destructive_calls_need_the_namespace_repeated_back():
    assert assert_destroyable("otel-demo", "otel-demo-07", "otel-demo-07") == "otel-demo-07"
    for confirm in (None, "", "yes", "otel-demo", "otel-demo-08"):
        with pytest.raises(FleetGuardError, match="confirm"):
            assert_destroyable("otel-demo", "otel-demo-07", confirm)


def test_delete_slot_refuses_without_confirm_and_touches_nothing(fleet):
    client, store = fleet["client"], fleet["store"]
    client.post("/api/v1/fleet/provision?dry_run=false&wait=false")
    before = len(fleet["runner"].calls)

    refused = client.request("DELETE", "/api/v1/fleet/slots/s01?dry_run=false")

    assert refused.status_code == 400
    assert "confirm" in refused.json()["detail"]
    assert not any("delete" in " ".join(call["argv"]) for call in fleet["runner"].calls[before:])
    assert store.slot("s01") is not None


def test_delete_slot_dry_run_is_recorded_and_keeps_the_slot(fleet):
    client, store = fleet["client"], fleet["store"]
    client.post("/api/v1/fleet/provision?dry_run=false&wait=false")

    response = client.request("DELETE", "/api/v1/fleet/slots/s02?confirm=otel-demo-02&dry_run=true")

    assert response.status_code == 200
    assert response.json()["dry_run"] is True
    assert store.slot("s02") is not None
    deletes = [call["argv"] for call in fleet["runner"].calls if "delete" in call["argv"]]
    assert deletes and all("--dry-run=server" in argv for argv in deletes)
    audit = [event for event in store.audit_log() if event["action"] == "delete_slot"]
    assert audit and audit[0]["dry_run"] is True and audit[0]["namespace"] == "otel-demo-02"


# -- provisioning ----------------------------------------------------------

def test_provision_dry_run_renders_manifests_without_applying(fleet):
    client = fleet["client"]

    response = client.post("/api/v1/fleet/provision?dry_run=true")

    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True
    assert [slot["namespace"] for slot in body["slots"]] == ["otel-demo-01", "otel-demo-02", "otel-demo-03"]
    kinds = {row["kind"] for row in body["slots"][0]["objects"]}
    assert {"Namespace", "LimitRange", "ResourceQuota", "NetworkPolicy",
            "Deployment", "Service", "PersistentVolumeClaim", "Role", "RoleBinding"} <= kinds
    applies = [call["argv"] for call in fleet["runner"].calls if "apply" in call["argv"]]
    assert applies and all("--dry-run=server" in argv for argv in applies)
    assert all("--server-dry-run" in argv for argv in fleet["deploys"])
    assert fleet["store"].slots() == []


def test_provisioned_controller_is_bound_to_its_own_replica():
    config = FleetConfig.model_validate(CONFIG)
    objects = slot_manifests(config, 2)
    deployment = next(item for item in objects if item["kind"] == "Deployment")
    stage2 = next(c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == "stage2")
    env = {item["name"]: item.get("value") for item in stage2["env"]}

    assert env["RESBENCH_APPLICATION_NAMESPACE"] == "otel-demo-02"
    assert env["RESBENCH_APPLICATION"] == "otel-demo-02"
    assert env["RESBENCH_CONTROL_NAMESPACE"] == "resiliencebenchmark-system"
    # Each slot keeps its own lock file, ports and evidence volume.
    assert deployment["spec"]["template"]["spec"]["volumes"][1]["persistentVolumeClaim"]["claimName"] == "resbench-stage2-s02-data"
    # Slots are spread over the configured nodes rather than stacked.
    assert deployment["spec"]["template"]["spec"]["nodeSelector"] == {"kubernetes.io/hostname": "node-b"}


def test_replica_rbac_is_per_namespace_and_never_shared(fleet):
    config = FleetConfig.model_validate(CONFIG)
    first = {item["metadata"]["name"] for item in slot_manifests(config, 1) if item["kind"] == "RoleBinding"}
    second = {item["metadata"]["name"] for item in slot_manifests(config, 2) if item["kind"] == "RoleBinding"}

    assert first.isdisjoint(second)


def test_provision_installs_the_trimmed_system_under_test(fleet):
    fleet["client"].post("/api/v1/fleet/provision?dry_run=false&wait=true")

    for argv in fleet["deploys"]:
        assert argv[argv.index("--values-profile") + 1] == "replica"
        assert argv[argv.index("--application") + 1] == "otel-demo"
        assert argv[argv.index("--namespace") + 1].startswith("otel-demo-")
        assert "--execute" in argv
    assert [slot["phase"] for slot in fleet["store"].slots()] == ["Ready"] * 3


# -- batches ---------------------------------------------------------------

def test_batch_dry_run_shows_the_schedule_without_running_anything(fleet):
    client = fleet["client"]
    client.post("/api/v1/fleet/provision?dry_run=false&wait=true")
    items = [_item(n, harness) for n, harness in
             enumerate(["codex", "claude-code", "deepseek-harness"] * 2, 1)]
    for index, item in enumerate(items):
        item["repetition"] = 1 + index // 3

    response = client.post("/api/v1/fleet/batches?dry_run=true", json=_batch(items))

    assert response.status_code == 202
    body = response.json()
    assert body["dry_run"] is True
    assert len(body["schedule"]) == 6
    assert {row["namespace"] for row in body["schedule"]} == {"otel-demo-01", "otel-demo-02", "otel-demo-03"}
    assert sorted(row["wave"] for row in body["schedule"]) == [0, 0, 0, 1, 1, 1]
    assert fleet["store"].batches() == []
    assert all(not controller.created for controller in fleet["controllers"].values())


def test_batch_rejects_a_self_inconsistent_item(fleet):
    client = fleet["client"]
    client.post("/api/v1/fleet/provision?dry_run=false&wait=true")

    response = client.post(
        "/api/v1/fleet/batches?dry_run=true",
        json=_batch([_item(1, "codex", case="D7")]),
    )

    assert response.status_code == 422
    assert "tool_substitution_variant" in response.text


def test_batch_dispatches_one_trial_per_replica(fleet):
    client = fleet["client"]
    client.post("/api/v1/fleet/provision?dry_run=false&wait=true")
    items = [_item(n, harness) for n, harness in
             enumerate(["codex", "claude-code", "deepseek-harness"] * 2, 1)]
    for index, item in enumerate(items):
        item["repetition"] = 1 + index // 3

    response = client.post("/api/v1/fleet/batches?dry_run=false", json=_batch(items))

    assert response.status_code == 202
    running = [row for row in response.json()["items"] if row["state"] == "Running"]
    assert len(running) == 3
    assert len({row["namespace"] for row in running}) == 3
    for namespace, controller in fleet["controllers"].items():
        assert len(controller.created) <= 1
        for created in controller.created:
            assert created["application"] == namespace
            assert namespace in created["prompt"]
            assert created["variant_set_id"]


def test_repeated_batch_submission_does_not_run_it_twice(fleet):
    client = fleet["client"]
    client.post("/api/v1/fleet/provision?dry_run=false&wait=true")
    body = _batch([_item(1, "codex")])
    client.post("/api/v1/fleet/batches?dry_run=false", json=body)
    submitted = sum(len(controller.created) for controller in fleet["controllers"].values())

    replay = client.post("/api/v1/fleet/batches?dry_run=false", json=body)

    assert replay.status_code == 202
    assert replay.json()["idempotent_replay"] is True
    assert sum(len(controller.created) for controller in fleet["controllers"].values()) == submitted


def test_finished_trials_free_their_slot_and_reach_the_result_matrix(fleet):
    client, dispatcher = fleet["client"], fleet["dispatcher"]
    client.post("/api/v1/fleet/provision?dry_run=false&wait=true")
    items = [_item(n, harness) for n, harness in
             enumerate(["codex", "claude-code", "deepseek-harness"] * 2, 1)]
    for index, item in enumerate(items):
        item["repetition"] = 1 + index // 3
    client.post("/api/v1/fleet/batches?dry_run=false", json=_batch(items))

    for _pass in range(4):
        for controller in fleet["controllers"].values():
            for created in controller.created:
                controller.terminal[created["run_id"]] = {
                    "run_id": created["run_id"], "status": "COMPLETED", "terminal": True, "failure": None,
                }
        dispatcher.tick()

    batch = client.get("/api/v1/fleet/batches/dx-parallel-20260912-01").json()
    assert batch["counts"] == {"Done": 6}
    assert batch["state"] == "Completed"
    csv_rows = client.get("/api/v1/fleet/batches/dx-parallel-20260912-01/results?format=csv").text
    assert csv_rows.splitlines()[0].startswith("batch_id,item_id,namespace,slot_id")
    assert len(csv_rows.strip().splitlines()) == 7


def test_platform_failures_are_voided_and_agent_failures_are_kept(fleet):
    client, store, dispatcher = fleet["client"], fleet["store"], fleet["dispatcher"]
    client.post("/api/v1/fleet/provision?dry_run=false&wait=true")
    client.post("/api/v1/fleet/batches?dry_run=false",
                json=_batch([_item(1, "codex"), _item(2, "claude-code", repetition=2)]))
    items = {item["item_id"]: item for item in store.items("dx-parallel-20260912-01")}
    first, second = items["i-001"], items["i-002"]
    controllers = {slot["namespace"]: fleet["controllers"][slot["namespace"]] for slot in store.slots()
                   if slot["namespace"] in fleet["controllers"]}
    controllers[first["namespace"]].terminal[first["run_id"]] = {
        "status": "FAILED", "terminal": True,
        "failure": {"code": "STAGE2_PLATFORM_FAILED", "reason": "tunnel dropped"},
    }
    controllers[second["namespace"]].terminal[second["run_id"]] = {
        "status": "FAILED", "terminal": True,
        "failure": {"code": "HARNESS_TIMEOUT", "reason": "agent ran out of time"},
    }

    dispatcher.poll_running()

    after = {item["item_id"]: item for item in store.items("dx-parallel-20260912-01")}
    assert after["i-001"]["state"] == "Queued"
    assert after["i-001"]["platform_retries"] == 1
    assert after["i-001"]["failure"]["voided_and_requeued"] is True
    assert after["i-002"]["state"] == "Failed"
    assert after["i-002"]["failure"]["owner"] == "agent"


def test_platform_retries_stop_at_the_configured_limit(fleet):
    client, store, dispatcher = fleet["client"], fleet["store"], fleet["dispatcher"]
    client.post("/api/v1/fleet/provision?dry_run=false&wait=true")
    client.post("/api/v1/fleet/batches?dry_run=false",
                json=_batch([_item(1, "codex")], platform_retry_limit=1))

    for _attempt in range(3):
        for item in store.items("dx-parallel-20260912-01"):
            if item["state"] in {"Running", "Assigned"} and item["run_id"]:
                controller = fleet["controllers"][item["namespace"]]
                controller.terminal[item["run_id"]] = {
                    "status": "FAILED", "terminal": True,
                    "failure": {"code": "STAGE2_PLATFORM_FAILED", "reason": "503"},
                }
        dispatcher.tick()

    item = store.item("dx-parallel-20260912-01", "i-001")
    assert item["state"] == "Failed"
    assert item["platform_retries"] == 1
    assert item["failure"]["owner"] == "platform"


def test_a_rejected_submission_is_invalid_and_keeps_the_controller_message(fleet):
    client, store = fleet["client"], fleet["store"]
    client.post("/api/v1/fleet/provision?dry_run=false&wait=true")
    for controller in ["otel-demo-01", "otel-demo-02", "otel-demo-03"]:
        fleet["controllers"].setdefault(controller, FakeController(controller))
    for controller in fleet["controllers"].values():
        controller.submit_error = ControllerError(
            "POST /lx/runs returned 422", status=422,
            payload={"detail": "case D7 requires tool_substitution_variant"},
        )

    client.post("/api/v1/fleet/batches?dry_run=false", json=_batch([_item(1, "codex")]))

    item = store.item("dx-parallel-20260912-01", "i-001")
    assert item["state"] == "Invalid"
    assert "tool_substitution_variant" in json.dumps(item["failure"])


def test_stop_dequeues_the_rest_and_stops_what_is_running(fleet):
    client, store = fleet["client"], fleet["store"]
    client.post("/api/v1/fleet/provision?dry_run=false&wait=true")
    items = [_item(n, harness) for n, harness in
             enumerate(["codex", "claude-code", "deepseek-harness"] * 2, 1)]
    for index, item in enumerate(items):
        item["repetition"] = 1 + index // 3
    client.post("/api/v1/fleet/batches?dry_run=false", json=_batch(items))

    response = client.post("/api/v1/fleet/batches/dx-parallel-20260912-01/stop", json={})

    assert response.status_code == 202
    actions = {row["action"] for row in response.json()["stopped"]}
    assert actions == {"stop_requested", "dequeued"}
    assert store.batch("dx-parallel-20260912-01")["state"] == "Stopped"
    assert sum(len(controller.stopped) for controller in fleet["controllers"].values()) == 3


def test_manual_prompt_is_sent_verbatim_with_its_fault_contract():
    controller = FakeController("otel-demo-02")
    resolved = {
        "autonomy_level": "L0", "harness": "codex", "model": "qwen3.8-max",
        "llm_tag": "tag", "duration_seconds": 300, "case": "C0", "prompt_source": "manual",
        "prompt": "请针对 otel-demo-02 的 cart 服务做一次实验。", "note": None,
        "tool_substitution_variant": None,
        "slots": {"target": "cart", "fault_type": "cpu_load",
                  "fault_params": {"cpu_percent": 80}, "duration_seconds": 300},
    }

    body = build_run_request(controller, "otel-demo-02", resolved)

    assert body["prompt"] == resolved["prompt"]
    assert body["slots"]["duration_seconds"] == 300
    assert "variant_set_id" not in body


def test_a_pinned_namespace_must_be_a_ready_slot():
    request = BatchRequest.model_validate(_batch([_item(1, "codex", namespace="otel-demo-09")]))
    slots = [{"slot_id": "s01", "slot_index": 1, "namespace": "otel-demo-01", "phase": "Ready"}]

    with pytest.raises(ValueError, match="otel-demo-09"):
        plan_batch(request, slots)


def test_preflight_reports_every_slot(fleet):
    client = fleet["client"]
    client.post("/api/v1/fleet/provision?dry_run=false&wait=true")

    body = client.get("/api/v1/fleet/preflight").json()

    assert [row["slot_id"] for row in body["slots"]] == ["s01", "s02", "s03"]
    assert all(row["reachable"] for row in body["slots"])
    assert all(row["gateway_probe"] == "complete" for row in body["slots"])
    assert all(row["environment"]["ready"] for row in body["slots"])


def test_slot_prompt_shows_what_that_replica_renders(fleet):
    client = fleet["client"]
    client.post("/api/v1/fleet/provision?dry_run=false&wait=true")

    body = client.get("/api/v1/fleet/slots/s02/prompt?level=L0").json()

    assert body["namespace"] == "otel-demo-02"
    assert "otel-demo-02" in body["prompt"]
    assert "otel-demo-02" in body["autonomy_cases"][0]["copy_ready_prompt"]


def test_failure_classification_separates_the_two_causes():
    assert classify_failure({"code": "STAGE2_PLATFORM_FAILED"}) == "platform"
    assert classify_failure({"code": "X", "reason": "upstream model rate limited"}) == "platform"
    assert classify_failure({"code": "HARNESS_TIMEOUT"}) == "agent"
    assert classify_failure({"code": "OUTPUT_UNSTRUCTURED"}) == "agent"
    assert classify_failure(None, http_status=503) == "platform"


def test_dry_run_reports_what_it_could_not_simulate_on_a_fresh_replica(fleet, monkeypatch):
    """A server dry run creates no namespace, so it cannot check what goes inside one."""
    monkeypatch.setattr(fleet["provisioner"].kube, "namespace_exists",
                        lambda namespace: namespace == "resiliencebenchmark-system")

    body = fleet["client"].post("/api/v1/fleet/provision?dry_run=true").json()

    slot = body["slots"][0]
    assert slot["system_under_test"]["skipped"] is True
    assert "cannot create it" in slot["system_under_test"]["reason"]
    deferred = {row["kind"] for row in slot["not_simulated"]["objects"]}
    assert {"LimitRange", "ResourceQuota", "NetworkPolicy", "Role", "RoleBinding"} <= deferred
    # The Namespace itself and the control-namespace objects are still checked.
    applied_kinds = {row["kind"] for row in slot["objects"]} - deferred
    assert {"Namespace", "Deployment", "Service", "PersistentVolumeClaim"} <= applied_kinds


def test_dry_run_checks_everything_once_the_replica_exists(fleet):
    """Re-provisioning an existing replica validates every object and the install."""
    body = fleet["client"].post("/api/v1/fleet/provision?dry_run=true").json()

    slot = body["slots"][0]
    assert "not_simulated" not in slot
    assert slot["system_under_test"]["mode"] == "server-dry-run"
    assert slot["system_under_test"]["exit_code"] == 0


def test_deploy_gets_a_private_copy_of_the_runtime_env_file(tmp_path: Path):
    """The mounted Secret is 0440; deploy_application.py refuses group-readable files."""
    mounted = tmp_path / "mounted" / "otel-demo.env"
    mounted.parent.mkdir()
    mounted.write_text("HARBOR_REGISTRY=registry.example\n", encoding="utf-8")
    mounted.chmod(0o440)
    seen: list[dict[str, Any]] = []

    def deploy_runner(argv):
        path = Path(argv[argv.index("--runtime-env-file") + 1])
        seen.append({"path": path, "mode": path.stat().st_mode & 0o777,
                     "content": path.read_text(encoding="utf-8")})
        return subprocess.CompletedProcess(argv, 0, json.dumps({"result": "applied-ready"}), "")

    store = FleetStore(tmp_path / "fleet.sqlite3")
    provisioner = Provisioner(
        store, kube=KubeClient(runner=FakeKubeRunner()), repo_root=tmp_path,
        runtime_env_file=str(mounted), deploy_runner=deploy_runner, sleep=lambda _s: None,
    )

    provisioner.deploy_sut(FleetConfig.model_validate(CONFIG), "otel-demo-01", dry_run=False)

    assert len(seen) == 1
    assert seen[0]["mode"] == 0o600
    assert seen[0]["content"] == mounted.read_text(encoding="utf-8")
    assert seen[0]["path"] != mounted
    # The private copy does not outlive the call.
    assert not seen[0]["path"].exists()
