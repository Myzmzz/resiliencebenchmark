import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from mcp_servers.chaos_control.service import (
    FAULT_TYPE_LABEL,
    LOGICAL_NAMESPACE_LABEL,
    OWNER_LABEL,
    OWNER_VALUE,
    RUN_ID_LABEL,
    TARGET_UID_LABEL,
    ChaosControlService,
    ExperimentRecord,
    InMemoryChaosBackend,
    KubectlChaosBackend,
    RuntimeConfig,
)


def run(coro):
    return asyncio.run(coro)


def call(coro):
    from mcp_servers.chaos_control.service import ChaosControlError

    try:
        return run(coro)
    except ChaosControlError as exc:
        return exc.as_response()


async def capture(coro):
    from mcp_servers.chaos_control.service import ChaosControlError

    try:
        return await coro
    except ChaosControlError as exc:
        return exc.as_response()


class ChaosControlServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.ledger_dir = Path(self.tempdir.name) / "ledger"
        self.baseline_dir = Path(self.tempdir.name) / "baseline"
        self.baseline_dir.mkdir(mode=0o700)
        self.baseline_token = "baseline-ok-token"
        self.config = RuntimeConfig(
            execute_enabled=True,
            kubeconfig="/tmp/controller.kubeconfig",
            namespace_allowlist=frozenset({"otel-demo"}),
            controller_token_ref="k8s://resbench/controller-token#token",
            controller_pod_uid="controller-pod-uid",
            ledger_dir=self.ledger_dir,
            baseline_ledger_dir=self.baseline_dir,
        )
        self.backend = InMemoryChaosBackend(pod_uids={("otel-demo", "checkoutservice-abc123"): "pod-uid-1"})
        self.service = ChaosControlService(self.config, self.backend)
        self.write_baseline()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_baseline(self, token=None, **overrides):
        token = token or self.baseline_token
        payload = {
            "passed": True,
            "run_id": "episode-e2e-001-r001",
            "namespace": "otel-demo",
            "target_name": "checkoutservice-abc123",
            "target_uid": "pod-uid-1",
            "controller_pod_uid": "controller-pod-uid",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        }
        payload.update(overrides)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        path = self.baseline_dir / f"{token_hash}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def create_kwargs(self, **overrides):
        values = {
            "run_id": "episode-e2e-001-r001",
            "namespace": "otel-demo",
            "target_name": "checkoutservice-abc123",
            "target_uid": "pod-uid-1",
            "fault_type": "network-delay",
            "duration_seconds": 120,
            "intensity": {"delay_ms": 250},
            "kubeconfig": "/tmp/controller.kubeconfig",
            "controller_token_ref": "k8s://resbench/controller-token#token",
            "expected_controller_pod_uid": "controller-pod-uid",
            "baseline_gate_token": self.baseline_token,
            "cleanup_handle": "cleanup-episode-e2e-001-r001",
        }
        values.update(overrides)
        return values

    def test_from_env_empty_mapping_does_not_read_host_environment(self):
        old_value = os.environ.get("RESBENCH_CHAOS_EXECUTE_ENABLED")
        os.environ["RESBENCH_CHAOS_EXECUTE_ENABLED"] = "true"
        try:
            config = RuntimeConfig.from_env({})
        finally:
            if old_value is None:
                os.environ.pop("RESBENCH_CHAOS_EXECUTE_ENABLED", None)
            else:
                os.environ["RESBENCH_CHAOS_EXECUTE_ENABLED"] = old_value

        self.assertFalse(config.execute_enabled)
        self.assertIsNone(config.kubeconfig)

    def test_validate_plan_is_read_only_and_reuses_controller_policy(self):
        result = run(
            self.service.validate_plan(
                run_id="episode-e2e-001-r001",
                namespace="otel-demo",
                target_name="checkoutservice-abc123",
                target_uid="pod-uid-1",
                fault_type="network-delay",
                duration_seconds=120,
                intensity={"delay_ms": 250},
            )
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["read_only"])
        self.assertIn("network-delay", result["policy"]["allowed_fault_types"])
        self.assertEqual([], self.backend.created_manifests)

    def test_create_is_hard_disabled_by_default(self):
        service = ChaosControlService(RuntimeConfig(namespace_allowlist=frozenset({"otel-demo"})), self.backend)

        result = call(service.create_experiment(**self.create_kwargs()))

        self.assertFalse(result["ok"])
        self.assertEqual("EXECUTION_DISABLED", result["error"]["code"])
        self.assertEqual([], self.backend.created_manifests)

    def test_create_requires_explicit_configured_kubeconfig_and_token_ref(self):
        result = call(self.service.create_experiment(**self.create_kwargs(kubeconfig="/tmp/other.kubeconfig")))

        self.assertFalse(result["ok"])
        self.assertEqual("EXPLICIT_KUBECONFIG_REQUIRED", result["error"]["code"])

    def test_create_rejects_forged_baseline_token(self):
        result = call(self.service.create_experiment(**self.create_kwargs(baseline_gate_token="forged-token")))

        self.assertFalse(result["ok"])
        self.assertEqual("BASELINE_TOKEN_NOT_FOUND", result["error"]["code"])
        self.assertNotIn("forged-token", json.dumps(result))

    def test_create_rejects_expired_baseline_token(self):
        self.write_baseline(expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())

        result = call(self.service.create_experiment(**self.create_kwargs()))

        self.assertFalse(result["ok"])
        self.assertEqual("BASELINE_TOKEN_EXPIRED", result["error"]["code"])

    def test_create_rejects_baseline_field_drift(self):
        self.write_baseline(target_uid="stale-pod-uid")

        result = call(self.service.create_experiment(**self.create_kwargs()))

        self.assertFalse(result["ok"])
        self.assertEqual("BASELINE_LEDGER_MISMATCH", result["error"]["code"])

    def test_create_rejects_baseline_symlink(self):
        token_hash = hashlib.sha256(self.baseline_token.encode()).hexdigest()
        path = self.baseline_dir / f"{token_hash}.json"
        path.unlink()
        os.symlink("/tmp/nonexistent-baseline-ledger.json", path)

        result = call(self.service.create_experiment(**self.create_kwargs()))

        self.assertFalse(result["ok"])
        self.assertEqual("LEDGER_FILE_UNSAFE", result["error"]["code"])

    def test_create_rejects_selector_target(self):
        result = call(self.service.create_experiment(**self.create_kwargs(selector={"app": "checkout"})))

        self.assertFalse(result["ok"])
        self.assertEqual("PLAN_REJECTED_BY_SAFETY_POLICY", result["error"]["code"])
        self.assertIn("SELECTOR_TARGET_FORBIDDEN", result["error"]["next_step"])

    def test_create_rejects_global_unowned_error_resource(self):
        self.backend.experiments[("sock-shop", "manual-chaos")] = ExperimentRecord(
            name="manual-chaos",
            namespace="sock-shop",
            run_id="",
            target_name="front-end-abc123",
            target_uid="pod-uid-2",
            fault_type="network-delay",
            phase="Error",
            owner=None,
            labels={},
        )

        result = call(self.service.create_experiment(**self.create_kwargs()))

        self.assertFalse(result["ok"])
        self.assertEqual("UNSAFE_UNOWNED_CHAOSBLADE_PRESENT", result["error"]["code"])
        self.assertEqual([], self.backend.created_manifests)

    def test_create_rejects_live_pod_uid_drift(self):
        self.backend.pod_uids[("otel-demo", "checkoutservice-abc123")] = "new-pod-uid"

        result = call(self.service.create_experiment(**self.create_kwargs()))

        self.assertFalse(result["ok"])
        self.assertEqual("TARGET_UID_MISMATCH", result["error"]["code"])
        self.assertEqual([], self.backend.created_manifests)

    def test_create_writes_cluster_scoped_manifest_and_private_ledger(self):
        result = run(self.service.create_experiment(**self.create_kwargs()))

        self.assertTrue(result["ok"], result)
        manifest = self.backend.created_manifests[0]
        self.assertNotIn("namespace", manifest["metadata"])
        self.assertEqual({"experiments"}, set(manifest["spec"]))
        labels = manifest["metadata"]["labels"]
        self.assertEqual("episode-e2e-001-r001", labels[RUN_ID_LABEL])
        self.assertEqual("otel-demo", labels[LOGICAL_NAMESPACE_LABEL])
        self.assertEqual("pod-uid-1", labels[TARGET_UID_LABEL])
        self.assertEqual("network-delay", labels[FAULT_TYPE_LABEL])
        self.assertEqual(OWNER_VALUE, labels[OWNER_LABEL])
        matchers = {item["name"]: item["value"] for item in manifest["spec"]["experiments"][0]["matchers"]}
        self.assertEqual(["checkoutservice-abc123"], matchers["names"])
        self.assertEqual(["otel-demo"], matchers["namespace"])

        ledger_mode = stat.S_IMODE(os.stat(self.ledger_dir).st_mode)
        file_path = self.ledger_dir / "cleanup-episode-e2e-001-r001.json"
        file_mode = stat.S_IMODE(os.stat(file_path).st_mode)
        self.assertEqual(0o700, ledger_mode)
        self.assertEqual(0o600, file_mode)
        file_text = file_path.read_text()
        payload = json.loads(file_text)
        self.assertEqual("cleanup-episode-e2e-001-r001", payload["cleanup_handle"])
        self.assertNotIn("baseline-ok-token", file_text)
        self.assertEqual(hashlib.sha256(self.baseline_token.encode()).hexdigest(), payload["baseline_gate_token_sha256"])
        self.assertEqual("active", payload["state"])

    def test_create_rejects_replayed_baseline_token(self):
        first = run(self.service.create_experiment(**self.create_kwargs()))
        self.assertTrue(first["ok"], first)

        result = call(
            self.service.create_experiment(
                **self.create_kwargs(cleanup_handle="cleanup-episode-e2e-001-r002")
            )
        )

        self.assertFalse(result["ok"])
        self.assertEqual("BASELINE_TOKEN_REPLAYED", result["error"]["code"])

    def test_create_apply_failure_keeps_cleanup_ledger(self):
        from mcp_servers.chaos_control.service import ChaosControlError

        class FailingCreateBackend(InMemoryChaosBackend):
            async def create_experiment(self, manifest, kubeconfig):
                self.created_manifests.append(manifest)
                raise ChaosControlError(
                    "APPLY_FAILED",
                    "simulated apply failure",
                    next_step="destroy with the returned cleanup handle",
                )

        backend = FailingCreateBackend(pod_uids={("otel-demo", "checkoutservice-abc123"): "pod-uid-1"})
        service = ChaosControlService(self.config, backend)

        result = call(service.create_experiment(**self.create_kwargs()))

        self.assertFalse(result["ok"])
        self.assertEqual("APPLY_FAILED", result["error"]["code"])
        ledger_path = self.ledger_dir / "cleanup-episode-e2e-001-r001.json"
        self.assertTrue(ledger_path.exists())
        ledger = json.loads(ledger_path.read_text())
        self.assertEqual("create_failed", ledger["state"])
        cleanup = run(service.destroy_experiment(cleanup_handle="cleanup-episode-e2e-001-r001", kubeconfig="/tmp/controller.kubeconfig"))
        self.assertTrue(cleanup["ok"], cleanup)
        self.assertTrue(cleanup["verified_absent"])

    def test_concurrent_creates_are_serialized_by_process_lock(self):
        class SlowCreateBackend(InMemoryChaosBackend):
            def __init__(self):
                super().__init__(pod_uids={("otel-demo", "checkoutservice-abc123"): "pod-uid-1"})
                self.inflight = 0
                self.max_inflight = 0

            async def create_experiment(self, manifest, kubeconfig):
                self.inflight += 1
                self.max_inflight = max(self.max_inflight, self.inflight)
                await asyncio.sleep(0.01)
                try:
                    return await super().create_experiment(manifest, kubeconfig)
                finally:
                    self.inflight -= 1

        backend = SlowCreateBackend()
        service = ChaosControlService(self.config, backend)
        second_token = "baseline-ok-token-2"
        self.write_baseline(token=second_token)

        async def scenario():
            return await asyncio.gather(
                capture(service.create_experiment(**self.create_kwargs())),
                capture(
                    service.create_experiment(
                        **self.create_kwargs(
                            baseline_gate_token=second_token,
                            cleanup_handle="cleanup-episode-e2e-001-r002",
                        )
                    )
                ),
            )

        results = run(scenario())

        self.assertEqual(1, backend.max_inflight)
        self.assertEqual(1, len([item for item in results if item["ok"]]))
        self.assertEqual(1, len([item for item in results if not item["ok"]]))

    def test_destroy_only_uses_server_ledger_handle_and_verifies_absence(self):
        create_result = run(self.service.create_experiment(**self.create_kwargs()))
        name = create_result["created"]["name"]

        destroy_result = run(
            self.service.destroy_experiment(
                cleanup_handle="cleanup-episode-e2e-001-r001",
                kubeconfig="/tmp/controller.kubeconfig",
            )
        )

        self.assertTrue(destroy_result["ok"], destroy_result)
        self.assertTrue(destroy_result["verified_absent"])
        self.assertEqual([("otel-demo", name)], self.backend.deleted)

    def test_destroy_rejects_unknown_handle_without_deleting_anything(self):
        result = call(
            self.service.destroy_experiment(
                cleanup_handle="cleanup-missing-handle",
                kubeconfig="/tmp/controller.kubeconfig",
            )
        )

        self.assertFalse(result["ok"])
        self.assertEqual("UNKNOWN_CLEANUP_HANDLE", result["error"]["code"])
        self.assertEqual([], self.backend.deleted)

    def test_destroy_rejects_cleanup_ledger_symlink(self):
        self.ledger_dir.mkdir(mode=0o700)
        os.symlink("/tmp/nonexistent-cleanup-ledger.json", self.ledger_dir / "cleanup-episode-e2e-001-r001.json")

        result = call(
            self.service.destroy_experiment(
                cleanup_handle="cleanup-episode-e2e-001-r001",
                kubeconfig="/tmp/controller.kubeconfig",
            )
        )

        self.assertFalse(result["ok"])
        self.assertEqual("LEDGER_FILE_UNSAFE", result["error"]["code"])
        self.assertEqual([], self.backend.deleted)

    def test_inventory_counts_global_unsafe_unowned_resources(self):
        self.backend.experiments[("otel-demo", "owned")] = ExperimentRecord(
            name="owned",
            namespace="otel-demo",
            run_id="run-1",
            target_name="pod-a",
            target_uid="uid-a",
            fault_type="cpu-load",
            phase="Running",
            owner=OWNER_VALUE,
            labels={OWNER_LABEL: OWNER_VALUE},
        )
        self.backend.experiments[("sock-shop", "unowned")] = ExperimentRecord(
            name="unowned",
            namespace="sock-shop",
            run_id="",
            target_name="pod-b",
            target_uid="uid-b",
            fault_type="cpu-load",
            phase="Unknown",
            owner=None,
            labels={},
        )

        result = run(self.service.inventory_run(namespace="otel-demo", kubeconfig="/tmp/controller.kubeconfig"))

        self.assertTrue(result["ok"])
        self.assertTrue(result["cluster_scoped"])
        self.assertEqual(1, result["active_owned_count"])
        self.assertEqual(1, result["global_unsafe_unowned_count"])


class RecordingKubectlBackend(KubectlChaosBackend):
    def __init__(self) -> None:
        super().__init__("kubectl")
        self.calls = []

    async def _kubectl(self, args, *, stdin=None):
        self.calls.append(list(args))
        if "apply" in args:
            return ""
        if "pod" in args:
            return json.dumps({"metadata": {"uid": "pod-uid-1"}})
        if "delete" in args:
            return ""
        if args[-2:] == ["-o", "json"]:
            if len(args) >= 4 and args[2] == "get" and args[3] == "chaosblades.chaosblade.io":
                if len(args) == 6:
                    return json.dumps(
                        {
                            "items": [
                                {
                                    "metadata": {"name": "blade-1", "labels": {LOGICAL_NAMESPACE_LABEL: "otel-demo"}},
                                    "spec": {
                                        "experiments": [
                                            {
                                                "target": "network",
                                                "action": "delay",
                                                "matchers": [
                                                    {"name": "names", "value": ["checkoutservice-abc123"]},
                                                    {"name": "namespace", "value": ["otel-demo"]},
                                                ],
                                            }
                                        ]
                                    },
                                    "status": {"phase": "Destroyed"},
                                }
                            ]
                        }
                    )
                return json.dumps(
                    {
                        "metadata": {"name": args[4], "labels": {LOGICAL_NAMESPACE_LABEL: "otel-demo"}},
                        "spec": {
                            "experiments": [
                                {
                                    "target": "network",
                                    "action": "delay",
                                    "matchers": [
                                        {"name": "names", "value": ["checkoutservice-abc123"]},
                                        {"name": "namespace", "value": ["otel-demo"]},
                                    ],
                                }
                            ]
                        },
                        "status": {"phase": "Running"},
                    }
                )
        return "{}"


class KubectlBackendCommandTest(unittest.TestCase):
    def test_chaosblade_cr_operations_are_cluster_scoped_but_pod_uid_uses_namespace(self):
        backend = RecordingKubectlBackend()
        manifest = {
            "apiVersion": "chaosblade.io/v1alpha1",
            "kind": "ChaosBlade",
            "metadata": {"name": "blade-1", "labels": {LOGICAL_NAMESPACE_LABEL: "otel-demo"}},
            "spec": {
                "experiments": [
                    {
                        "scope": "pod",
                        "target": "network",
                        "action": "delay",
                        "matchers": [
                            {"name": "names", "value": ["checkoutservice-abc123"]},
                            {"name": "namespace", "value": ["otel-demo"]},
                        ],
                    }
                ]
            },
        }

        records = run(backend.list_experiments("/tmp/kubeconfig"))
        record = run(backend.get_experiment("otel-demo", "blade-1", "/tmp/kubeconfig"))
        run(backend.create_experiment(manifest, "/tmp/kubeconfig"))
        run(backend.delete_experiment("otel-demo", "blade-1", "/tmp/kubeconfig"))
        uid = run(backend.get_pod_uid("otel-demo", "checkoutservice-abc123", "/tmp/kubeconfig"))

        self.assertEqual("otel-demo", records[0].namespace)
        self.assertEqual("otel-demo", record.namespace)
        self.assertEqual("pod-uid-1", uid)
        chaosblade_calls = [call for call in backend.calls if "chaosblades.chaosblade.io" in call or "apply" in call]
        self.assertTrue(chaosblade_calls)
        for call in chaosblade_calls:
            self.assertNotIn("-n", call)
        pod_calls = [call for call in backend.calls if "pod" in call]
        self.assertEqual([["--kubeconfig", "/tmp/kubeconfig", "-n", "otel-demo", "get", "pod", "checkoutservice-abc123", "-o", "json"]], pod_calls)


class ChaosControlMcpServerTest(unittest.TestCase):
    def test_tool_annotations_are_accurate(self):
        try:
            from mcp_servers.chaos_control.server import mcp
        except ModuleNotFoundError as exc:
            self.skipTest(f"MCP SDK is not installed in this interpreter: {exc}")

        tools = run(mcp.list_tools())
        by_name = {tool.name: tool for tool in tools}

        self.assertTrue(by_name["chaos_validate_plan"].annotations.read_only_hint)
        self.assertTrue(by_name["chaos_inventory_run"].annotations.read_only_hint)
        self.assertFalse(by_name["chaos_create_experiment"].annotations.read_only_hint)
        self.assertFalse(by_name["chaos_create_experiment"].annotations.destructive_hint)
        self.assertTrue(by_name["chaos_destroy_experiment"].annotations.destructive_hint)
        self.assertTrue(by_name["chaos_destroy_experiment"].annotations.idempotent_hint)


if __name__ == "__main__":
    unittest.main()
