from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

from stage2_service.capability_policy import CapabilityPolicyRegistry
from stage2_service.contracts import (
    DisturbancePlan,
    DisturbanceRecord,
    DisturbanceType,
    LifecyclePhase,
    PermissionProfile,
)
from stage2_service.runtime_adapters import (
    CompositeDisturbanceExecutor,
    McpTokenStateRegistry,
)


class NoKubernetes:
    pass


class ManualTimer:
    def __init__(self, _delay: float, callback):
        self.callback = callback
        self.cancelled = False
        self.alive = False
        self.joins: list[float | None] = []

    def start(self) -> None:
        self.alive = True

    def cancel(self) -> None:
        self.cancelled = True
        self.alive = False

    def join(self, timeout: float | None = None) -> None:
        self.joins.append(timeout)

    def is_alive(self) -> bool:
        return self.alive

    def fire(self) -> None:
        self.alive = False
        self.callback()


def _plan(trial_id: str = "campaign-1234567890abcdef-codex-d5-1", duration: int = 1):
    return DisturbancePlan(
        trial_id=trial_id,
        disturbance_id="d5-channel-1",
        type=DisturbanceType.TOOL_CHANNEL_INTERRUPTION,
        phase=LifecyclePhase.C4_EFFECT,
        trigger_event_id="event-effect-check-started",
        committed_dependency="mcp.telemetry.read",
        backend="mcp_policy",
        parameters={
            "servers": ("k8s_ro", "telemetry_ro", "source_ro"),
            "duration_seconds": duration,
        },
        expected_behaviors=("channel_restored",),
        failure_conditions=(),
        rollback={},
    )


def _policy(tmp_path: Path, trial_id: str):
    registry = CapabilityPolicyRegistry(tmp_path / "policy")
    registry.initialize(
        trial_id,
        PermissionProfile(
            profile_id="p0-full-authorized",
            mcp_servers=("k8s_ro", "telemetry_ro", "source_ro", "chaos_control"),
        ),
    )
    return registry


def _executor(tmp_path: Path, policy, *, timer_factory, observer=None):
    return CompositeDisturbanceExecutor(
        kubernetes_client=NoKubernetes(),
        mcp_tokens=McpTokenStateRegistry(tmp_path / "tokens"),
        policy_registry=policy,
        timer_factory=timer_factory,
        restoration_observer=observer,
        clock=lambda: datetime(2026, 9, 5, tzinfo=UTC),
    )


def test_d5_apply_returns_before_restoration_and_callback_follows_policy_restore(tmp_path: Path):
    plan = _plan()
    policy = _policy(tmp_path, plan.trial_id)
    timers: list[ManualTimer] = []
    callback_records = []

    def timer_factory(delay, callback):
        timer = ManualTimer(delay, callback)
        timers.append(timer)
        return timer

    def observer(record):
        assert policy.snapshot().server_policy("telemetry_ro").channel_unavailable_until is None
        callback_records.append(record)

    executor = _executor(tmp_path, policy, timer_factory=timer_factory, observer=observer)
    applied = executor.apply(plan)

    assert applied.rolled_back is False
    assert applied.application_evidence["restoration"]["status"] == "pending"
    assert callback_records == []
    assert policy.snapshot().server_policy("telemetry_ro").channel_unavailable_until is not None

    timers[0].fire()
    completed = executor.wait_for_restoration(applied)

    assert completed.rolled_back is True
    assert completed.application_evidence["restoration"]["verified"] is True
    assert completed.application_evidence["channel_restored_feedback"]["event_type"] == "CHANNEL_RESTORED"
    assert callback_records == [completed]


def test_d5_early_rollback_cancels_timer_and_restores_once(tmp_path: Path):
    plan = _plan()
    policy = _policy(tmp_path, plan.trial_id)
    timers: list[ManualTimer] = []
    executor = _executor(
        tmp_path,
        policy,
        timer_factory=lambda delay, callback: timers.append(ManualTimer(delay, callback)) or timers[-1],
    )
    applied = executor.apply(plan)

    restored = executor.rollback(applied)

    assert timers[0].cancelled is True
    assert timers[0].joins
    assert restored.rolled_back is True
    assert restored.rollback_evidence["source"] == "d5-channel-rollback"
    sequence = policy.snapshot().sequence
    timers[0].fire()  # A stale callback must not restore a second time.
    assert policy.snapshot().sequence == sequence


def test_d5_restoration_failure_remains_visible(tmp_path: Path):
    plan = _plan()
    policy = _policy(tmp_path, plan.trial_id)
    timers: list[ManualTimer] = []
    executor = _executor(
        tmp_path,
        policy,
        timer_factory=lambda delay, callback: timers.append(ManualTimer(delay, callback)) or timers[-1],
    )
    applied = executor.apply(plan)

    def fail_restore(*_args, **_kwargs):
        raise OSError("policy disk unavailable")

    policy.restore = fail_restore  # type: ignore[method-assign]
    timers[0].fire()

    completed = executor.wait_for_restoration(applied)
    assert completed.rolled_back is False
    assert completed.application_evidence["restoration"] == {
        "status": "failed",
        "verified": False,
        "source": "d5-channel-restored",
        "error_type": "OSError",
    }
    rolled_back = executor.rollback(applied)
    assert rolled_back.rolled_back is False


def test_d5_real_timer_restores_policy_after_short_window(tmp_path: Path):
    plan = _plan(duration=1)
    policy = _policy(tmp_path, plan.trial_id)
    observed = threading.Event()
    executor = _executor(
        tmp_path,
        policy,
        timer_factory=lambda delay, callback: __import__("threading").Timer(delay, callback),
        observer=lambda _record: observed.set(),
    )

    applied = executor.apply(plan)
    assert policy.snapshot().server_policy("k8s_ro").channel_unavailable_until is not None
    completed = executor.wait_for_restoration(applied, timeout=2.5)

    assert observed.is_set()
    assert completed.rolled_back is True
    assert policy.snapshot().server_policy("k8s_ro").channel_unavailable_until is None


def test_d5_completed_trial_does_not_modify_next_trials_policy(tmp_path: Path):
    first = _plan("campaign-1234567890abcdef-codex-d5-first")
    second = _plan("campaign-1234567890abcdef-codex-d5-second")
    first_policy = _policy(tmp_path / "first", first.trial_id)
    second_policy = _policy(tmp_path / "second", second.trial_id)
    first_timers: list[ManualTimer] = []
    second_timers: list[ManualTimer] = []
    first_executor = _executor(
        tmp_path / "first",
        first_policy,
        timer_factory=lambda delay, callback: first_timers.append(ManualTimer(delay, callback)) or first_timers[-1],
    )
    second_executor = _executor(
        tmp_path / "second",
        second_policy,
        timer_factory=lambda delay, callback: second_timers.append(ManualTimer(delay, callback)) or second_timers[-1],
    )

    first_record = first_executor.apply(first)
    second_record = second_executor.apply(second)
    first_timers[0].fire()
    first_executor.wait_for_restoration(first_record)

    assert first_policy.snapshot().server_policy("k8s_ro").channel_unavailable_until is None
    assert second_policy.snapshot().server_policy("k8s_ro").channel_unavailable_until is not None
    second_executor.rollback(second_record)


def test_d5_concurrent_trials_restore_only_their_own_policy(tmp_path: Path):
    first = _plan("campaign-1234567890abcdef-codex-d5-concurrent-a")
    second = _plan("campaign-1234567890abcdef-codex-d5-concurrent-b")
    first_policy = _policy(tmp_path / "first", first.trial_id)
    second_policy = _policy(tmp_path / "second", second.trial_id)
    token_registry = McpTokenStateRegistry(tmp_path / "tokens")
    token_registry.register_policy_root(first.trial_id, first_policy.root)
    token_registry.register_policy_root(second.trial_id, second_policy.root)
    timers: dict[str, ManualTimer] = {}
    timer_lock = threading.Lock()

    def timer_factory(delay, callback):
        timer = ManualTimer(delay, callback)
        with timer_lock:
            assert len(timers) < 2
            timers[[first.trial_id, second.trial_id][len(timers)]] = timer
        return timer

    executor = CompositeDisturbanceExecutor(
        kubernetes_client=NoKubernetes(),
        mcp_tokens=token_registry,
        timer_factory=timer_factory,
    )
    records = []
    errors = []

    def apply(plan):
        try:
            records.append(executor.apply(plan))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    workers = [threading.Thread(target=apply, args=(plan,)) for plan in (first, second)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert errors == []
    assert len(records) == 2
    assert first_policy.snapshot().server_policy("k8s_ro").channel_unavailable_until is not None
    assert second_policy.snapshot().server_policy("k8s_ro").channel_unavailable_until is not None
    for timer in timers.values():
        timer.fire()
    completed = [executor.wait_for_restoration(record) for record in records]
    assert all(record.rolled_back for record in completed)
    assert first_policy.snapshot().server_policy("k8s_ro").channel_unavailable_until is None
    assert second_policy.snapshot().server_policy("k8s_ro").channel_unavailable_until is None


def test_d5_rollback_after_executor_restart_restores_the_persisted_snapshot(tmp_path: Path):
    """A restarted executor has no timer state; rollback must use apply's snapshot.

    The record is round-tripped through JSON the way the campaign persists it,
    so D5's apply-side evidence (``policy_snapshot``, ``servers``,
    ``duration_seconds``) is checked against what the recovered path reads.
    """
    plan = _plan()
    policy = _policy(tmp_path, plan.trial_id)
    provisioned = policy.snapshot()
    applied = _executor(tmp_path, policy, timer_factory=ManualTimer).apply(plan)
    assert policy.snapshot().server_policy("k8s_ro").channel_unavailable_until is not None
    persisted = DisturbanceRecord.model_validate_json(applied.model_dump_json())
    restarted = _executor(tmp_path, policy, timer_factory=ManualTimer)

    restored = restarted.rollback(persisted)

    assert restored.rolled_back is True
    assert restored.rollback_evidence["verified"] is True
    assert restored.rollback_evidence["source"] == "rollback-recovered"
    assert restored.application_evidence["restoration"]["status"] == "restored"
    assert policy.snapshot().servers == provisioned.servers
