from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from stage2_service.artifacts import ArtifactStore
from stage2_service.campaign import CampaignEngine, _guided_turn_feedback
from stage2_service.contracts import (
    AgentVerdict,
    CampaignRequest,
    CapabilityProfile,
    DisturbanceRecord,
    DisturbanceType,
    HarnessKind,
    HarnessReport,
    InteractionMode,
    KubernetesRule,
    LifecycleEvent,
    LifecyclePhase,
    MainFaultSpec,
    PlatformStatus,
    RecoveryResult,
    RuntimeTarget,
    Stage2CaseId,
    TargetSpec,
    TrialKind,
    TrialRuntimeContext,
)
from stage2_service.disturbance import RuntimeDisturbancePlanner
from stage2_service.episode import load_fixed_episode


REPO_ROOT = Path(__file__).resolve().parents[1]
EPISODE_ROOT = REPO_ROOT / "tasks/episodes/otel-demo/EPI-OTEL-CART-DEADLINE-001"


def test_guided_feedback_is_not_created_after_native_turn_timeout():
    sent: set[str] = set()
    feedback = _guided_turn_feedback(
        {
            "event_type": "NATIVE_TURN_COMPLETED",
            "payload": {
                "summary": {
                    "timed_out": True,
                    "cancelled": False,
                    "returncode": -15,
                },
                "lifecycle_events": [{"kind": "recovery_accepted"}],
            },
        },
        interaction_mode=InteractionMode.GUIDED,
        sent=sent,
    )

    assert feedback is None
    assert sent == set()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _request():
    from stage2_service.contracts import FixedEpisodeRef

    return CampaignRequest(
        request_id="stage2-test-001",
        episode=FixedEpisodeRef(
            internal_path="tasks/episodes/otel-demo/EPI-OTEL-CART-DEADLINE-001/episode-internal.yaml",
            public_path="tasks/episodes/otel-demo/EPI-OTEL-CART-DEADLINE-001/episode-public.yaml",
            episode_id="EPI-OTEL-CART-DEADLINE-001",
            internal_sha256=_sha(EPISODE_ROOT / "episode-internal.yaml"),
            public_sha256=_sha(EPISODE_ROOT / "episode-public.yaml"),
        ),
        model_by_harness={
            HarnessKind.CODEX: "gpt-5.6-sol",
        },
        target=TargetSpec(namespace="otel-demo", component="cart"),
        main_fault=MainFaultSpec(
            fault_type="network-delay",
            duration_seconds=180,
            intensity={"delay_ms": 1000},
        ),
    )


class Gate:
    def __init__(self, qualified: bool = True):
        self.qualified = qualified

    def qualify(self, _episode):
        return {"qualified": self.qualified, "built_in_load_generator_ready": self.qualified}


class Preparer:
    def prepare(self, trial_id, episode, *, namespace, target, main_fault):
        del namespace, target
        return TrialRuntimeContext(
            trial_id=trial_id,
            episode_id=episode.internal.identity.episode_id,
            target=RuntimeTarget(
                namespace="otel-demo",
                component="cart",
                name="cart-example",
                uid="11111111-2222-4333-8444-555555555555",
            ),
            main_fault={
                "fault_type": main_fault.fault_type,
                "duration_seconds": main_fault.duration_seconds,
                "intensity": dict(main_fault.intensity),
                "selection_mode": "explicit_api_contract",
            },
            cleanup_handle="cleanup-" + "a" * 36,
            baseline_capability="b" * 40,
        )


class Permissions:
    def __init__(self):
        self.provisioned: list[str] = []
        self.restored: list[str] = []

    def provision(self, _campaign_id, trial_id, harness, _episode, _runtime):
        self.provisioned.append(trial_id)
        return CapabilityProfile(
            harness=harness,
            mcp_servers=("k8s_ro", "telemetry_ro", "source_ro", "chaos_control"),
            mcp_tools=("k8s_list_resources", "chaos_create_experiment"),
            kubernetes_rules=(
                KubernetesRule(api_group="", resource="pods", verbs=("get", "list"), namespace="otel-demo"),
            ),
            direct_kubeconfig=harness is HarnessKind.BLADEAI,
            allowed_fault_types=("network-delay",),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    def restore(self, trial_id):
        self.restored.append(trial_id)
        return {"verified": True}


class Disturbances:
    def __init__(self):
        self.applied = []

    def apply(self, plan):
        self.applied.append(plan)
        if plan.type is DisturbanceType.TARGET_CHANGE:
            target = plan.parameters["target"]
            return DisturbanceRecord(
                plan=plan,
                applied=True,
                application_evidence={
                    "old_name": target["name"],
                    "old_uid": target["uid"],
                    "replacement_name": "cart-replacement",
                    "replacement_uid": "uid-new",
                    "baseline_capability": {
                        "baseline_capability_rebound": True,
                        "binding_version": 2,
                    },
                },
            )
        return DisturbanceRecord(
            plan=plan,
            applied=True,
            application_evidence={"verified": True},
        )

    def rollback(self, record):
        return record.model_copy(update={"rolled_back": True, "rollback_evidence": {"verified": True}})


class Runner:
    def run(
        self,
        *,
        campaign_id,
        trial_id,
        harness,
        model_alias,
        episode,
        runtime_context,
        capability,
        case,
        base_prompt,
        event_observer,
        **_runtime_options,
    ):
        del model_alias, episode, capability, base_prompt
        events = []
        if case.trial_kind is TrialKind.PROTECTED_INFRASTRUCTURE:
            events = [
                LifecycleEvent(
                    event_id=f"{trial_id}-p2-stop",
                    campaign_id=campaign_id,
                    trial_id=trial_id,
                    harness=harness,
                    phase=LifecyclePhase.C5_SAFETY,
                    kind="unsafe_refusal",
                    payload={"reason": "protected_infrastructure"},
                )
            ]
        elif case.trial_kind is TrialKind.PROMPT_HIDDEN_TARGET:
            events = [
                LifecycleEvent(
                    event_id=f"{trial_id}-p1-bind",
                    campaign_id=campaign_id,
                    trial_id=trial_id,
                    harness=harness,
                    phase=LifecyclePhase.C2_TARGET,
                    kind="target_bound",
                    payload={"target": runtime_context.target.model_dump(mode="json")},
                ),
                LifecycleEvent(
                    event_id=f"{trial_id}-p1-create",
                    campaign_id=campaign_id,
                    trial_id=trial_id,
                    harness=harness,
                    phase=LifecyclePhase.C3_INJECT,
                    kind="main_fault_requested",
                    payload={"target_uid": runtime_context.target.uid},
                ),
            ]
        elif case.trial_kind is TrialKind.CHAOS_PERMISSION_REVOKED:
            events = [
                LifecycleEvent(
                    event_id=f"{trial_id}-d1-intent",
                    campaign_id=campaign_id,
                    trial_id=trial_id,
                    harness=harness,
                    phase=LifecyclePhase.C2_TARGET,
                    kind="plan_validated",
                    payload={"target_uid": runtime_context.target.uid},
                ),
                LifecycleEvent(
                    event_id=f"{trial_id}-d1-denied",
                    campaign_id=campaign_id,
                    trial_id=trial_id,
                    harness=harness,
                    phase=LifecyclePhase.C3_INJECT,
                    kind="permission_denied",
                    payload={"capability": "mcp.chaos.create"},
                ),
                LifecycleEvent(
                    event_id=f"{trial_id}-d1-stop",
                    campaign_id=campaign_id,
                    trial_id=trial_id,
                    harness=harness,
                    phase=LifecyclePhase.C5_SAFETY,
                    kind="safe_stop",
                    payload={"reason": "policy_denied"},
                ),
            ]
        elif case.trial_kind is TrialKind.TARGET_CHANGE:
            events = [
                LifecycleEvent(
                    event_id=f"{trial_id}-d2-bind",
                    campaign_id=campaign_id,
                    trial_id=trial_id,
                    harness=harness,
                    phase=LifecyclePhase.C2_TARGET,
                    kind="target_bound",
                    payload={"target": runtime_context.target.model_dump(mode="json")},
                ),
                LifecycleEvent(
                    event_id=f"{trial_id}-d2-rebind",
                    campaign_id=campaign_id,
                    trial_id=trial_id,
                    harness=harness,
                    phase=LifecyclePhase.C2_TARGET,
                    kind="target_reconfirmed",
                    payload={"uid": "uid-new"},
                ),
                LifecycleEvent(
                    event_id=f"{trial_id}-d2-create",
                    campaign_id=campaign_id,
                    trial_id=trial_id,
                    harness=harness,
                    phase=LifecyclePhase.C3_INJECT,
                    kind="main_fault_requested",
                    payload={"target_uid": "uid-new"},
                ),
            ]
        elif case.trial_kind is TrialKind.EFFECT_OBSERVABILITY_REVOKED:
            events = [
                LifecycleEvent(
                    event_id=f"{trial_id}-d3-running",
                    campaign_id=campaign_id,
                    trial_id=trial_id,
                    harness=harness,
                    phase=LifecyclePhase.C3_INJECT,
                    kind="main_fault_running",
                    payload={"target_uid": runtime_context.target.uid},
                ),
                LifecycleEvent(
                    event_id=f"{trial_id}-d3-denied",
                    campaign_id=campaign_id,
                    trial_id=trial_id,
                    harness=harness,
                    phase=LifecyclePhase.C4_EFFECT,
                    kind="permission_denied",
                    payload={"capability": "mcp.telemetry.read"},
                ),
                LifecycleEvent(
                    event_id=f"{trial_id}-d3-unverified",
                    campaign_id=campaign_id,
                    trial_id=trial_id,
                    harness=harness,
                    phase=LifecyclePhase.C4_EFFECT,
                    kind="effect_unverified",
                    payload={},
                ),
            ]
        elif case.trial_kind is TrialKind.RECOVERY_OBSERVABILITY_REVOKED:
            events = [
                LifecycleEvent(
                    event_id=f"{trial_id}-d4-recovery",
                    campaign_id=campaign_id,
                    trial_id=trial_id,
                    harness=harness,
                    phase=LifecyclePhase.C6_RECOVERY,
                    kind="recovery_accepted",
                    payload={},
                ),
                LifecycleEvent(
                    event_id=f"{trial_id}-d4-denied",
                    campaign_id=campaign_id,
                    trial_id=trial_id,
                    harness=harness,
                    phase=LifecyclePhase.C6_RECOVERY,
                    kind="permission_denied",
                    payload={"capability": "mcp.k8s.read"},
                ),
                LifecycleEvent(
                    event_id=f"{trial_id}-d4-unverified",
                    campaign_id=campaign_id,
                    trial_id=trial_id,
                    harness=harness,
                    phase=LifecyclePhase.C6_RECOVERY,
                    kind="recovery_unverified",
                    payload={},
                ),
            ]
        elif case.trial_kind is TrialKind.TOOL_CHANNEL_INTERRUPTED:
            events = [
                LifecycleEvent(
                    event_id=f"{trial_id}-d5-check",
                    campaign_id=campaign_id,
                    trial_id=trial_id,
                    harness=harness,
                    phase=LifecyclePhase.C4_EFFECT,
                    kind="effect_check_started",
                    payload={},
                ),
                LifecycleEvent(
                    event_id=f"{trial_id}-d5-error",
                    campaign_id=campaign_id,
                    trial_id=trial_id,
                    harness=harness,
                    phase=LifecyclePhase.C4_EFFECT,
                    kind="tool_channel_error",
                    payload={"capability": "mcp.telemetry.read"},
                ),
                LifecycleEvent(
                    event_id=f"{trial_id}-d5-reconcile",
                    campaign_id=campaign_id,
                    trial_id=trial_id,
                    harness=harness,
                    phase=LifecyclePhase.C4_EFFECT,
                    kind="effect_unverified",
                    payload={},
                ),
            ]
        elif case.trial_kind is TrialKind.OPERATION_OUTCOME_UNCERTAIN:
            events = [
                LifecycleEvent(
                    event_id=f"{trial_id}-d6-request",
                    campaign_id=campaign_id,
                    trial_id=trial_id,
                    harness=harness,
                    phase=LifecyclePhase.C3_INJECT,
                    kind="main_fault_requested",
                    payload={"target_uid": runtime_context.target.uid},
                ),
                LifecycleEvent(
                    event_id=f"{trial_id}-d6-reconcile",
                    campaign_id=campaign_id,
                    trial_id=trial_id,
                    harness=harness,
                    phase=LifecyclePhase.C3_INJECT,
                    kind="operation_reconciled",
                    payload={},
                ),
                LifecycleEvent(
                    event_id=f"{trial_id}-d6-stop",
                    campaign_id=campaign_id,
                    trial_id=trial_id,
                    harness=harness,
                    phase=LifecyclePhase.C5_SAFETY,
                    kind="safe_stop",
                    payload={"reason": "operation_reconciled"},
                ),
            ]
        else:
            events = [
            LifecycleEvent(
                event_id=f"{trial_id}-c1",
                campaign_id=campaign_id,
                trial_id=trial_id,
                harness=harness,
                phase=LifecyclePhase.C1_PLAN,
                kind="plan_committed",
                payload={"capabilities": ["mcp.chaos.create", "k8s.read"]},
            ),
            LifecycleEvent(
                event_id=f"{trial_id}-c2",
                campaign_id=campaign_id,
                trial_id=trial_id,
                harness=harness,
                phase=LifecyclePhase.C2_TARGET,
                kind="target_bound",
                payload={
                    "target": {
                        "namespace": "otel-demo",
                        "name": "cart-example",
                        "uid": "11111111-2222-4333-8444-555555555555",
                    }
                },
            ),
            *(
                LifecycleEvent(
                    event_id=f"{trial_id}-{phase.value}",
                    campaign_id=campaign_id,
                    trial_id=trial_id,
                    harness=harness,
                    phase=phase,
                    kind=f"phase-{phase.value}",
                    payload={},
                )
                for phase in (LifecyclePhase.C3_INJECT, LifecyclePhase.C4_EFFECT, LifecyclePhase.C5_SAFETY, LifecyclePhase.C6_RECOVERY)
            ),
        ]
        for event in events:
            event_observer(event)
        return HarnessReport(
            status="completed",
            agent_verdict=AgentVerdict.PASS,
            lifecycle_events=tuple(events),
            artifact_refs=(f"harness://{trial_id}",),
            final_output={
                "agent_result": {
                    "remaining_risk": case.expected_agent_signal,
                    "recovery_check": case.expected_agent_signal,
                }
            },
        )


class Finalizer:
    def finalize(self, trial_id, _episode, _runtime, _report):
        return RecoveryResult(
            agent_attempted=True,
            agent_recovery_verified=True,
            controller_cleanup_verified=True,
            fault_absent=True,
            business_recovery_verified=True,
            evidence_refs=(f"recovery://{trial_id}",),
        )


class Evaluator:
    def evaluate(self, *, kind, report, disturbances, recovery, diagnostic_only):
        del kind, diagnostic_only
        if report.status != "completed" or not recovery.controller_cleanup_verified:
            return AgentVerdict.FAIL
        if disturbances and not all(item.applied for item in disturbances):
            return AgentVerdict.FAIL
        return AgentVerdict.PASS


class Resetter:
    def __init__(self, verified: bool = True):
        self.verified = verified
        self.calls: list[str] = []

    def reset(self, trial_id, _episode, _mutation_evidence=None):
        self.calls.append(trial_id)
        return {"verified": self.verified}


def _engine(tmp_path: Path, *, gate=True, reset=True):
    request = _request()
    episode = load_fixed_episode(request.episode, root=REPO_ROOT)
    permissions = Permissions()
    disturbances = Disturbances()
    resetter = Resetter(reset)
    engine = CampaignEngine(
        episode=episode,
        environment_gate=Gate(gate),
        preparer=Preparer(),
        permissions=permissions,
        harness_runner=Runner(),
        disturbance_planner=RuntimeDisturbancePlanner(),
        disturbance_executor=disturbances,
        finalizer=Finalizer(),
        evaluator=Evaluator(),
        resetter=resetter,
        artifacts=ArtifactStore(tmp_path),
    )
    return engine, request, permissions, disturbances, resetter


def test_campaign_runs_codex_nine_case_suite(tmp_path: Path):
    engine, request, permissions, disturbances, resetter = _engine(tmp_path)
    result = engine.run(request)

    assert result.platform_status is PlatformStatus.COMPLETED
    assert len(result.trials) == 9
    assert [item.kind for item in result.trials] == [
        TrialKind.CONTROL,
        TrialKind.PROMPT_HIDDEN_TARGET,
        TrialKind.PROTECTED_INFRASTRUCTURE,
        TrialKind.CHAOS_PERMISSION_REVOKED,
        TrialKind.TARGET_CHANGE,
        TrialKind.EFFECT_OBSERVABILITY_REVOKED,
        TrialKind.RECOVERY_OBSERVABILITY_REVOKED,
        TrialKind.TOOL_CHANNEL_INTERRUPTED,
        TrialKind.OPERATION_OUTCOME_UNCERTAIN,
    ]
    assert len(disturbances.applied) == 6
    assert {item.type.value for item in disturbances.applied} == {
        "target_change",
        "permission_change",
        "observability_change",
        "tool_channel_interruption",
        "operation_outcome_uncertainty",
    }
    assert len(permissions.provisioned) == len(permissions.restored) == 9
    assert len(resetter.calls) == 9
    assert all(item.platform_valid for item in result.trials)
    attempts = {
        path.parent.name: json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / result.campaign_id / "trials").glob(
            "*/disturbance-attempt.json"
        )
    }
    assert len(attempts) == 9
    assert next(
        value for key, value in attempts.items() if "-c0-" in key
    )["state"] == "NOT_APPLICABLE"
    assert all(
        next(value for key, value in attempts.items() if f"-{case}-" in key)["state"]
        == "ROLLED_BACK"
        for case in ("d1", "d2", "d3", "d4", "d5", "d6-a")
    )
    assert len(
        list(
            (tmp_path / result.campaign_id / "trials").glob(
                "*/evaluation-decision.json"
            )
        )
    ) == 9
    assert (tmp_path / result.campaign_id / "campaign/evaluation.json").is_file()
    assert (tmp_path / result.campaign_id / "manifest.sha256").is_file()


class ScoredQualification:
    def qualify(self, _request):
        return {
            "execution_allowed": True,
            "formal_eligible": True,
            "scored": True,
        }


class ControlFailureEvaluator:
    def __init__(self):
        self.diagnostic_flags: list[bool] = []

    def evaluate(self, *, kind, report, disturbances, recovery, diagnostic_only):
        del report, disturbances, recovery
        self.diagnostic_flags.append(diagnostic_only)
        return AgentVerdict.FAIL if kind is TrialKind.CONTROL else AgentVerdict.PASS


def test_control_failure_does_not_downgrade_later_disturbance_cases(tmp_path: Path):
    engine, request, _permissions, _disturbances, _resetter = _engine(tmp_path)
    evaluator = ControlFailureEvaluator()
    engine.qualification_gate = ScoredQualification()
    engine.evaluator = evaluator

    result = engine.run(request)

    assert result.trials[0].agent_verdict is AgentVerdict.FAIL
    assert all(item.agent_verdict is AgentVerdict.PASS for item in result.trials[1:])
    assert all(item.diagnostic_only is False for item in result.trials)
    assert evaluator.diagnostic_flags == [False] * 9


def test_campaign_blocks_before_harness_when_application_traffic_is_absent(tmp_path: Path):
    engine, request, permissions, disturbances, resetter = _engine(tmp_path, gate=False)
    result = engine.run(request)

    assert result.platform_status is PlatformStatus.BLOCKED
    assert result.trials == ()
    assert permissions.provisioned == []
    assert disturbances.applied == []
    assert resetter.calls == []


def test_campaign_accepts_four_native_harness_matrix(tmp_path: Path):
    engine, request, _permissions, _disturbances, _resetter = _engine(tmp_path)
    request = request.model_copy(
        update={
            "harnesses": tuple(HarnessKind),
            "model_by_harness": {
                HarnessKind.CODEX: "gpt-5.6-sol",
                HarnessKind.CLAUDE_CODE: "claude-opus-5",
                HarnessKind.DEEPSEEK: "gpt-5.6-sol",
                HarnessKind.BLADEAI: "gpt-5.6-sol",
            },
            "cases": (Stage2CaseId.C0,),
        }
    )

    result = engine.run(request)

    assert result.platform_status is PlatformStatus.COMPLETED
    assert {item.harness for item in result.trials} == set(HarnessKind)
    assert len(result.trials) == 4


def test_campaign_stops_after_reset_failure(tmp_path: Path):
    engine, request, _permissions, _disturbances, resetter = _engine(tmp_path, reset=False)
    result = engine.run(request)

    assert result.platform_status is PlatformStatus.RESET_FAILED
    assert len(result.trials) == 1
    assert len(resetter.calls) == 1


class RaisingRunner:
    def run(self, **_kwargs):
        raise RuntimeError("native harness crashed")


class FailedRunner:
    def run(self, **_kwargs):
        return HarnessReport(
            status="failed",
            agent_verdict=AgentVerdict.INCONCLUSIVE,
            lifecycle_events=(),
        )


class InvalidAgentOutputRunner:
    def run(self, **_kwargs):
        return HarnessReport(
            status="failed",
            agent_verdict=AgentVerdict.INCONCLUSIVE,
            lifecycle_events=(),
            final_output={
                "process_succeeded": True,
                "validation_error": "agent result schema mismatch",
            },
        )


class TimeoutRunner:
    def run(self, **_kwargs):
        return HarnessReport(
            status="timeout",
            agent_verdict=AgentVerdict.FAIL,
            lifecycle_events=(),
        )


def test_campaign_restores_permissions_and_resets_after_harness_exception(tmp_path: Path):
    engine, request, permissions, _disturbances, resetter = _engine(tmp_path)
    engine.harness_runner = RaisingRunner()

    result = engine.run(request)

    assert result.platform_status is PlatformStatus.FAILED
    assert result.error == "RuntimeError: native harness crashed"
    assert len(permissions.provisioned) == len(permissions.restored) == 1
    assert len(resetter.calls) == 1
    cleanup = list(tmp_path.glob("*/trials/*/emergency-cleanup.json"))
    assert len(cleanup) == 1


def test_failed_harness_process_is_case_invalid_not_agent_failure(tmp_path: Path):
    engine, request, _permissions, _disturbances, _resetter = _engine(tmp_path)
    engine.harness_runner = FailedRunner()

    result = engine.run(request)

    assert result.platform_status is PlatformStatus.FAILED
    assert len(result.trials) == 7
    assert all(item.platform_valid is False for item in result.trials)
    assert all(item.agent_verdict is AgentVerdict.CASE_INVALID for item in result.trials)


def test_timeout_is_a_platform_valid_agent_outcome_when_cleanup_succeeds(tmp_path: Path):
    engine, request, _permissions, _disturbances, _resetter = _engine(tmp_path)
    engine.harness_runner = TimeoutRunner()
    request = request.model_copy(update={"cases": (Stage2CaseId.C0,)})

    result = engine.run(request)

    assert len(result.trials) == 1
    assert result.trials[0].platform_valid is True
    assert result.trials[0].agent_verdict is AgentVerdict.FAIL


def test_invalid_agent_output_after_successful_process_is_a_valid_agent_failure(tmp_path: Path):
    engine, request, _permissions, _disturbances, _resetter = _engine(tmp_path)
    engine.harness_runner = InvalidAgentOutputRunner()

    result = engine.run(request)

    assert result.trials[0].platform_valid is True
    assert result.trials[0].agent_verdict is AgentVerdict.FAIL
