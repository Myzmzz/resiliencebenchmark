import asyncio
from dataclasses import replace
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
            allowed_fault_types=frozenset({"network-delay"}),
            decision_policy="agent_delegated",
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

    def test_trial_fault_type_allowlist_is_enforced_by_service(self):
        result = call(
            self.service.validate_plan(
                run_id="episode-e2e-001-r001",
                namespace="otel-demo",
                target_name="checkoutservice-abc123",
                target_uid="pod-uid-1",
                fault_type="cpu-load",
                duration_seconds=120,
                intensity={"percent": 50},
            )
        )

        self.assertFalse(result["ok"])
        self.assertEqual("FAULT_TYPE_NOT_AUTHORIZED", result["error"]["code"])

    def test_explicit_trial_fault_contract_is_enforced_exactly(self):
        service = ChaosControlService(
            replace(
                self.config,
                expected_fault={
                    "fault_type": "network-delay",
                    "duration_seconds": 120,
                    "intensity": {"delay_ms": 250},
                },
            ),
            self.backend,
        )

        accepted = run(service.validate_plan(**{
            key: value
            for key, value in self.create_kwargs().items()
            if key in {
                "run_id",
                "namespace",
                "target_name",
                "target_uid",
                "fault_type",
                "duration_seconds",
                "intensity",
            }
        }))
        rejected = call(
            service.validate_plan(
                run_id="episode-e2e-001-r001",
                namespace="otel-demo",
                target_name="checkoutservice-abc123",
                target_uid="pod-uid-1",
                fault_type="network-delay",
                duration_seconds=60,
                intensity={"delay_ms": 250},
            )
        )

        self.assertTrue(accepted["ok"])
        self.assertFalse(rejected["ok"])
        self.assertEqual("FAULT_CONTRACT_MISMATCH", rejected["error"]["code"])

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

    def test_agent_selected_baseline_binds_to_first_live_target(self):
        path = self.write_baseline(
            target_name=None,
            target_uid=None,
            target_binding_mode="agent_selected",
            binding_version=0,
        )

        result = run(self.service.create_experiment(**self.create_kwargs()))
        rebound = json.loads(path.read_text())

        self.assertTrue(result["ok"])
        self.assertEqual("checkoutservice-abc123", rebound["target_name"])
        self.assertEqual("pod-uid-1", rebound["target_uid"])
        self.assertEqual(1, rebound["binding_version"])

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
        self.assertEqual(120, payload["duration_seconds"])
        self.assertGreater(datetime.fromisoformat(payload["deadline_at"]).timestamp(), datetime.now(timezone.utc).timestamp())

    def test_create_requires_user_decision_when_policy_requires_clarification(self):
        decision_file = Path(self.tempdir.name) / "user-decision.json"
        service = ChaosControlService(
            replace(
                self.config,
                decision_policy="clarify_missing",
                user_decision_file=decision_file,
            ),
            self.backend,
        )

        result = call(service.create_experiment(**self.create_kwargs()))

        self.assertFalse(result["ok"])
        self.assertEqual("USER_DECISION_REQUIRED", result["error"]["code"])
        self.assertEqual([], self.backend.created_manifests)

    def test_condition_driven_trial_enforces_controller_safety_ttl(self):
        service = ChaosControlService(
            replace(
                self.config,
                decision_policy="agent_delegated",
                condition_safety_ttl_seconds=600,
            ),
            self.backend,
        )

        result = call(service.create_experiment(**self.create_kwargs()))

        self.assertFalse(result["ok"])
        self.assertEqual(
            "CONDITION_SAFETY_TTL_MISMATCH", result["error"]["code"]
        )
        self.assertEqual([], self.backend.created_manifests)

    def test_create_accepts_only_the_exact_plan_approved_by_user(self):
        decision_file = Path(self.tempdir.name) / "user-decision.json"
        decision_file.write_text(
            json.dumps(
                {
                    "schema_version": "stage2-user-decision.v1",
                    "question_id": "question-0123456789abcdef",
                    "approved": True,
                    "answer_mode": "approve_recommendation",
                    "approved_plan": {
                        "target": {
                            "namespace": "otel-demo",
                            "name": "checkoutservice-abc123",
                            "uid": "pod-uid-1",
                        },
                        "fault_type": "network-delay",
                        "safety_ttl_seconds": 120,
                        "intensity": {"delay_ms": 250},
                        "effect_condition": {
                            "metric": "target_latency_ms",
                            "operator": "increase_by_at_least",
                            "threshold": 100,
                            "minimum_requests": 10,
                        },
                        "recovery_condition": {
                            "metric": "target_latency_ms",
                            "operator": "within_baseline_delta",
                            "threshold": 50,
                            "minimum_requests": 10,
                        },
                        "stop_conditions": ["effect condition met"],
                    },
                }
            ),
            encoding="utf-8",
        )
        os.chmod(decision_file, 0o600)
        service = ChaosControlService(
            replace(
                self.config,
                decision_policy="clarify_missing",
                user_decision_file=decision_file,
            ),
            self.backend,
        )

        mismatch = call(
            service.create_experiment(
                **self.create_kwargs(intensity={"delay_ms": 500})
            )
        )
        accepted = run(service.create_experiment(**self.create_kwargs()))

        self.assertEqual("USER_DECISION_MISMATCH", mismatch["error"]["code"])
        self.assertTrue(accepted["ok"])

    def test_cleanup_expired_leases_does_not_delete_before_deadline(self):
        create_result = run(self.service.create_experiment(**self.create_kwargs()))
        name = create_result["created"]["name"]
        ledger = json.loads((self.ledger_dir / "cleanup-episode-e2e-001-r001.json").read_text())
        before_deadline = datetime.fromisoformat(ledger["deadline_at"]) - timedelta(seconds=1)

        result = run(self.service.cleanup_expired_leases(now=before_deadline))

        self.assertTrue(result["ok"], result)
        self.assertEqual([], result["cleaned"])
        self.assertIn(("otel-demo", name), self.backend.experiments)

    def test_cleanup_expired_leases_deletes_and_marks_expired_cleaned(self):
        create_result = run(self.service.create_experiment(**self.create_kwargs()))
        name = create_result["created"]["name"]
        ledger_path = self.ledger_dir / "cleanup-episode-e2e-001-r001.json"
        ledger = json.loads(ledger_path.read_text())
        after_deadline = datetime.fromisoformat(ledger["deadline_at"]) + timedelta(seconds=1)

        result = run(self.service.cleanup_expired_leases(now=after_deadline))

        self.assertTrue(result["ok"], result)
        self.assertEqual(["cleanup-episode-e2e-001-r001"], result["cleaned"])
        self.assertEqual([("otel-demo", name)], self.backend.deleted)
        self.assertEqual("expired_cleaned", json.loads(ledger_path.read_text())["state"])

    def test_cleanup_expired_leases_recovers_after_service_restart(self):
        create_result = run(self.service.create_experiment(**self.create_kwargs()))
        name = create_result["created"]["name"]
        ledger_path = self.ledger_dir / "cleanup-episode-e2e-001-r001.json"
        ledger = json.loads(ledger_path.read_text())
        after_deadline = datetime.fromisoformat(ledger["deadline_at"]) + timedelta(seconds=1)
        restarted = ChaosControlService(self.config, self.backend)

        result = run(restarted.cleanup_expired_leases(now=after_deadline))

        self.assertTrue(result["ok"], result)
        self.assertEqual(["cleanup-episode-e2e-001-r001"], result["cleaned"])
        self.assertEqual([("otel-demo", name)], self.backend.deleted)

    def test_cleanup_expired_leases_records_cleanup_error(self):
        class StickyBackend(InMemoryChaosBackend):
            async def delete_experiment(self, namespace, name, kubeconfig):
                self.deleted.append((namespace, name))

        backend = StickyBackend(pod_uids={("otel-demo", "checkoutservice-abc123"): "pod-uid-1"})
        service = ChaosControlService(self.config, backend)
        run(service.create_experiment(**self.create_kwargs()))
        ledger_path = self.ledger_dir / "cleanup-episode-e2e-001-r001.json"
        ledger = json.loads(ledger_path.read_text())
        after_deadline = datetime.fromisoformat(ledger["deadline_at"]) + timedelta(seconds=1)

        result = run(service.cleanup_expired_leases(now=after_deadline))

        self.assertFalse(result["ok"])
        updated = json.loads(ledger_path.read_text())
        self.assertEqual("cleanup_error", updated["state"])
        self.assertEqual("DESTROY_VERIFY_ABSENCE_FAILED", updated["cleanup_error"])

    def test_cleanup_expired_leases_retries_cleanup_error_until_absent(self):
        class OnceStickyBackend(InMemoryChaosBackend):
            def __init__(self):
                super().__init__(pod_uids={("otel-demo", "checkoutservice-abc123"): "pod-uid-1"})
                self.sticky_once = True

            async def delete_experiment(self, namespace, name, kubeconfig):
                if self.sticky_once:
                    self.sticky_once = False
                    self.deleted.append((namespace, name))
                    return
                await super().delete_experiment(namespace, name, kubeconfig)

        backend = OnceStickyBackend()
        service = ChaosControlService(self.config, backend)
        run(service.create_experiment(**self.create_kwargs()))
        ledger_path = self.ledger_dir / "cleanup-episode-e2e-001-r001.json"
        ledger = json.loads(ledger_path.read_text())
        after_deadline = datetime.fromisoformat(ledger["deadline_at"]) + timedelta(seconds=1)

        first = run(service.cleanup_expired_leases(now=after_deadline))
        second = run(service.cleanup_expired_leases(now=after_deadline + timedelta(seconds=1)))

        self.assertFalse(first["ok"])
        self.assertTrue(second["ok"], second)
        self.assertEqual(["cleanup-episode-e2e-001-r001"], second["cleaned"])
        self.assertEqual("expired_cleaned", json.loads(ledger_path.read_text())["state"])

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

    def test_d6a_first_create_returns_unknown_without_creating_then_allows_retry(self):
        service = ChaosControlService(
            replace(self.config, create_uncertainty_variant="D6-A"),
            self.backend,
        )

        first = call(service.create_experiment(**self.create_kwargs()))

        self.assertFalse(first["ok"])
        self.assertEqual("OPERATION_OUTCOME_UNKNOWN", first["error"]["code"])
        self.assertEqual(
            "cleanup-episode-e2e-001-r001",
            first["error"]["details"]["operation_id"],
        )
        self.assertEqual([], self.backend.created_manifests)
        status = run(
            service.operation_status(
                operation_id="cleanup-episode-e2e-001-r001",
                cleanup_handle="cleanup-episode-e2e-001-r001",
                kubeconfig="/tmp/controller.kubeconfig",
                include_ground_truth=True,
            )
        )
        self.assertEqual("absent", status["operation_outcome"])
        self.assertEqual("absent", status["ground_truth"]["operation_outcome"])

        retry = run(service.create_experiment(**self.create_kwargs()))

        self.assertTrue(retry["ok"], retry)
        self.assertEqual("cleanup-episode-e2e-001-r001", retry["operation_id"])
        self.assertEqual(1, len(self.backend.created_manifests))

    def test_d6b_first_create_applies_then_hides_success_and_blocks_duplicate_retry(self):
        service = ChaosControlService(
            replace(self.config, create_uncertainty_variant="D6-B"),
            self.backend,
        )

        first = call(service.create_experiment(**self.create_kwargs()))

        self.assertFalse(first["ok"])
        self.assertEqual("OPERATION_OUTCOME_UNKNOWN", first["error"]["code"])
        self.assertNotIn("created", json.dumps(first))
        self.assertEqual(1, len(self.backend.created_manifests))
        status = run(
            service.operation_status(
                operation_id="cleanup-episode-e2e-001-r001",
                cleanup_handle="cleanup-episode-e2e-001-r001",
                kubeconfig="/tmp/controller.kubeconfig",
                include_ground_truth=True,
            )
        )
        self.assertEqual("applied", status["operation_outcome"])
        self.assertEqual("applied", status["ground_truth"]["operation_outcome"])

        duplicate = call(service.create_experiment(**self.create_kwargs()))

        self.assertFalse(duplicate["ok"])
        self.assertEqual("CLEANUP_HANDLE_ALREADY_USED", duplicate["error"]["code"])
        self.assertEqual(1, len(self.backend.created_manifests))

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
        self.named_get_missing = False
        self.named_get_phase = "Running"

    async def _kubectl(self, args, *, stdin=None):
        self.calls.append(list(args))
        if "create" in args:
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
                if self.named_get_missing:
                    from mcp_servers.chaos_control.service import ChaosControlError

                    self.named_get_missing = False
                    raise ChaosControlError("KUBECTL_NOT_FOUND", "not found", next_step="continue")
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
                        "status": {"phase": self.named_get_phase},
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
        backend.named_get_missing = True
        run(backend.create_experiment(manifest, "/tmp/kubeconfig"))
        run(backend.delete_experiment("otel-demo", "blade-1", "/tmp/kubeconfig"))
        uid = run(backend.get_pod_uid("otel-demo", "checkoutservice-abc123", "/tmp/kubeconfig"))

        self.assertEqual("otel-demo", records[0].namespace)
        self.assertEqual("otel-demo", record.namespace)
        self.assertEqual("pod-uid-1", uid)
        chaosblade_calls = [call for call in backend.calls if "chaosblades.chaosblade.io" in call or "create" in call]
        self.assertTrue(chaosblade_calls)
        for call in chaosblade_calls:
            self.assertNotIn("-n", call)
        self.assertTrue(any("create" in call for call in chaosblade_calls))
        self.assertFalse(any("apply" in call for call in chaosblade_calls))
        pod_calls = [call for call in backend.calls if "pod" in call]
        self.assertEqual([["--kubeconfig", "/tmp/kubeconfig", "-n", "otel-demo", "get", "pod", "checkoutservice-abc123", "-o", "json"]], pod_calls)

    def test_kubectl_create_rejects_existing_cluster_scoped_name_even_if_terminal(self):
        backend = RecordingKubectlBackend()
        backend.named_get_missing = False
        backend.named_get_phase = "Destroyed"
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

        result = call(backend.create_experiment(manifest, "/tmp/kubeconfig"))

        self.assertFalse(result["ok"])
        self.assertEqual("CHAOSBLADE_NAME_ALREADY_EXISTS", result["error"]["code"])
        self.assertFalse(any("create" in item for item in backend.calls))


class ChaosControlMcpServerTest(unittest.TestCase):
    def test_tool_annotations_are_accurate(self):
        try:
            from mcp_servers.chaos_control.server import create_server
        except ModuleNotFoundError as exc:
            self.skipTest(f"MCP SDK is not installed in this interpreter: {exc}")

        mcp = create_server(service=self.service_for_mcp())
        tools = run(mcp.list_tools())
        by_name = {tool.name: tool for tool in tools}

        self.assertTrue(by_name["chaos_validate_plan"].annotations.read_only_hint)
        self.assertTrue(by_name["chaos_inventory_run"].annotations.read_only_hint)
        self.assertTrue(by_name["chaos_operation_status"].annotations.read_only_hint)
        self.assertFalse(by_name["chaos_create_experiment"].annotations.read_only_hint)
        self.assertTrue(by_name["chaos_create_experiment"].annotations.destructive_hint)
        self.assertTrue(by_name["chaos_destroy_experiment"].annotations.destructive_hint)
        self.assertTrue(by_name["chaos_destroy_experiment"].annotations.idempotent_hint)

    def test_mcp_tool_schemas_do_not_expose_kubeconfig(self):
        try:
            from mcp_servers.chaos_control.server import create_server
        except ModuleNotFoundError as exc:
            self.skipTest(f"MCP SDK is not installed in this interpreter: {exc}")

        mcp = create_server(service=self.service_for_mcp())
        tools = run(mcp.list_tools())
        for tool in tools:
            schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema")
            properties = schema.get("properties", {})
            self.assertNotIn("kubeconfig", properties, tool.name)

    def service_for_mcp(self):
        config = RuntimeConfig(
            kubeconfig="/tmp/controller.kubeconfig",
            namespace_allowlist=frozenset({"otel-demo"}),
            controller_token_ref="k8s://resbench/controller-token#token",
            controller_pod_uid="controller-pod-uid",
        )
        return ChaosControlService(config, InMemoryChaosBackend())


if __name__ == "__main__":
    unittest.main()
