from datetime import datetime, timedelta, timezone
import unittest

from controller.safety import (
    ChaosBladeAction,
    ControllerPolicy,
    LifecyclePhase,
    RunLease,
    TargetIdentity,
    default_policy,
    should_cleanup_on_agent_loss,
    validate_action,
    validate_lifecycle_transition,
    validate_policy,
)


class SafetyValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = default_policy({"otel-demo"})
        self.target = TargetIdentity(
            namespace="otel-demo",
            kind="Pod",
            name="checkoutservice-abc123",
            uid="pod-uid-1",
        )

    def action(self, **overrides):
        values = {
            "run_id": "episode-e2e-001-r001",
            "namespace": "otel-demo",
            "target": self.target,
            "fault_type": "network-delay",
            "duration_seconds": 120,
            "intensity": {"delay_ms": 250},
            "labels": {"benchmark.run_id": "episode-e2e-001-r001"},
        }
        values.update(overrides)
        return ChaosBladeAction(**values)

    def test_accepts_single_target_action_with_uid_budget_and_run_label(self):
        result = validate_action(self.action(), self.policy)

        self.assertTrue(result.ok, result.findings)

    def test_rejects_action_outside_namespace_allowlist(self):
        target = TargetIdentity(
            namespace="sock-shop",
            kind="Pod",
            name="front-end-abc123",
            uid="pod-uid-2",
        )
        result = validate_action(self.action(namespace="sock-shop", target=target), self.policy)

        self.assertIn("NAMESPACE_NOT_ALLOWED", result.codes())

    def test_rejects_selector_target_even_with_one_namespace(self):
        target = TargetIdentity(
            namespace="otel-demo",
            kind="Pod",
            name="checkoutservice",
            uid="pod-uid-1",
            selector={"app": "checkoutservice"},
        )
        result = validate_action(self.action(target=target), self.policy)

        self.assertIn("SELECTOR_TARGET_FORBIDDEN", result.codes())

    def test_rejects_non_pod_target_kind(self):
        target = TargetIdentity(
            namespace="otel-demo",
            kind="Deployment",
            name="checkoutservice",
            uid="deployment-uid-1",
        )
        result = validate_action(self.action(target=target), self.policy)

        self.assertIn("TARGET_KIND_NOT_ALLOWED", result.codes())

    def test_rejects_missing_uid_and_label(self):
        target = TargetIdentity(namespace="otel-demo", kind="Pod", name="checkoutservice", uid="")
        result = validate_action(self.action(target=target, labels={}), self.policy)

        self.assertIn("MISSING_TARGET_UID", result.codes())
        self.assertIn("MISSING_RUN_ID_LABEL", result.codes())

    def test_rejects_intensity_and_duration_over_budget(self):
        result = validate_action(
            self.action(duration_seconds=181, intensity={"delay_ms": 1500}),
            self.policy,
        )

        self.assertIn("DURATION_BUDGET_EXCEEDED", result.codes())
        self.assertIn("INTENSITY_BUDGET_EXCEEDED", result.codes())

    def test_rejects_concurrent_fault(self):
        result = validate_action(self.action(), self.policy, active_action_count=1)

        self.assertIn("CONCURRENCY_BUDGET_EXCEEDED", result.codes())

    def test_policy_must_keep_abort_and_cleanup_enabled(self):
        unsafe = ControllerPolicy(
            namespace_allowlist=frozenset({"otel-demo"}),
            fault_type_budgets=self.policy.fault_type_budgets,
            max_concurrent_actions=2,
            require_single_target=False,
        )
        result = validate_policy(unsafe)

        self.assertIn("UNSAFE_CONCURRENCY", result.codes())
        self.assertIn("MULTI_TARGET_ALLOWED", result.codes())


class LifecycleTest(unittest.TestCase):
    def test_allows_nominal_lifecycle(self):
        phases = [
            LifecyclePhase.PREPARE,
            LifecyclePhase.QUALIFY,
            LifecyclePhase.BASELINE,
            LifecyclePhase.PLAN,
            LifecyclePhase.EXECUTE,
            LifecyclePhase.OBSERVE,
            LifecyclePhase.RECOVER,
            LifecyclePhase.EVALUATE,
            LifecyclePhase.CLEANUP,
        ]

        for current, next_phase in zip(phases, phases[1:]):
            self.assertTrue(validate_lifecycle_transition(current, next_phase).ok)

    def test_rejects_skipping_qualification(self):
        result = validate_lifecycle_transition(LifecyclePhase.PREPARE, LifecyclePhase.BASELINE)

        self.assertIn("INVALID_LIFECYCLE_TRANSITION", result.codes())

    def test_allows_abort_to_cleanup_before_terminal_phase(self):
        result = validate_lifecycle_transition(LifecyclePhase.EXECUTE, LifecyclePhase.CLEANUP)

        self.assertTrue(result.ok)


class AgentLossCleanupTest(unittest.TestCase):
    def test_agent_heartbeat_timeout_requires_cleanup(self):
        policy = default_policy({"otel-demo"})
        now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
        lease = RunLease(
            run_id="episode-e2e-001-r001",
            phase=LifecyclePhase.EXECUTE,
            started_at=now - timedelta(minutes=3),
            last_agent_heartbeat_at=now - timedelta(seconds=121),
            active_action_ids=("blade-action-1",),
        )

        self.assertTrue(should_cleanup_on_agent_loss(lease, policy, now=now))

    def test_cleanup_phase_does_not_reenter_cleanup(self):
        policy = default_policy({"otel-demo"})
        now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
        lease = RunLease(
            run_id="episode-e2e-001-r001",
            phase=LifecyclePhase.CLEANUP,
            started_at=now - timedelta(hours=1),
            last_agent_heartbeat_at=None,
        )

        self.assertFalse(should_cleanup_on_agent_loss(lease, policy, now=now))


if __name__ == "__main__":
    unittest.main()
