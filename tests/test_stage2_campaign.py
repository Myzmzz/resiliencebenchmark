from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from stage2_service.artifacts import ArtifactStore
from stage2_service.campaign import CampaignEngine
from stage2_service.contracts import (
    AgentVerdict,
    BladeAINativePermissions,
    CampaignRequest,
    CapabilityProfile,
    DisturbanceRecord,
    HarnessKind,
    HarnessModelSelection,
    HarnessReport,
    KubernetesRule,
    LifecycleEvent,
    LifecyclePhase,
    McpServerName,
    PlatformStatus,
    PermissionProfile,
    RecoveryResult,
    RuntimeTarget,
    TrialKind,
    TrialRuntimeContext,
)
from stage2_service.disturbance import RuntimeDisturbancePlanner
from stage2_service.episode import load_fixed_episode
from stage2_service.model_matrix import ModelMatrixCompiler


REPO_ROOT = Path(__file__).resolve().parents[1]
EPISODE_ROOT = REPO_ROOT / "tasks/episodes/otel-demo/EPI-OTEL-CART-DEADLINE-001"


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
        model_matrix=(
            HarnessModelSelection(harness=HarnessKind.CODEX, models=("gpt-5.6",)),
            HarnessModelSelection(
                harness=HarnessKind.CLAUDE_CODE, models=("claude-opus-5",)
            ),
            HarnessModelSelection(
                harness=HarnessKind.DEEPSEEK, models=("deepseek-v4-pro",)
            ),
            HarnessModelSelection(harness=HarnessKind.BLADEAI, models=("gpt-5.6",)),
        ),
    )


class Gate:
    def __init__(self, qualified: bool = True):
        self.qualified = qualified

    def qualify(self, _episode):
        return {"qualified": self.qualified, "built_in_load_generator_ready": self.qualified}


class Preparer:
    def prepare(self, trial_id, episode):
        return TrialRuntimeContext(
            trial_id=trial_id,
            episode_id=episode.internal.identity.episode_id,
            target=RuntimeTarget(
                namespace="otel-demo",
                component="cart",
                name="cart-example",
                uid="11111111-2222-4333-8444-555555555555",
            ),
            main_fault={"fault_type": "network-delay"},
            cleanup_handle="cleanup-" + "a" * 36,
            baseline_capability="b" * 40,
        )


class Permissions:
    def __init__(self):
        self.provisioned: list[str] = []
        self.restored: list[str] = []
        self.profiles: list[str] = []

    def provision(
        self,
        _campaign_id,
        trial_id,
        harness,
        _episode,
        _runtime,
        permission_profile,
    ):
        self.provisioned.append(trial_id)
        self.profiles.append(permission_profile.profile_id)
        return CapabilityProfile(
            permission_profile_id=permission_profile.profile_id,
            harness=harness,
            mcp_servers=(
                McpServerName.K8S_RO,
                McpServerName.TELEMETRY_RO,
                McpServerName.SOURCE_RO,
                McpServerName.CHAOS_CONTROL,
            ),
            mcp_tools=("k8s_list_resources", "chaos_create_experiment"),
            kubernetes_rules=(
                KubernetesRule(api_group="", resource="pods", verbs=("get", "list"), namespace="otel-demo"),
            ),
            direct_kubeconfig=harness is HarnessKind.BLADEAI,
            native_permissions=BladeAINativePermissions(),
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
        event_observer,
    ):
        del episode, runtime_context, capability
        events = (
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
        )
        for event in events:
            event_observer(event)
        return HarnessReport(
            status="completed",
            agent_verdict=AgentVerdict.PASS,
            lifecycle_events=events,
            artifact_refs=(f"harness://{trial_id}",),
            provider_reported_model=model_alias,
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

    def reset(self, trial_id, _episode):
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
        model_matrix=ModelMatrixCompiler(REPO_ROOT / "harness/models.yaml"),
    )
    return engine, request, permissions, disturbances, resetter


def test_campaign_runs_four_harnesses_and_three_trials(tmp_path: Path):
    engine, request, permissions, disturbances, resetter = _engine(tmp_path)
    result = engine.run(request)

    assert result.platform_status is PlatformStatus.COMPLETED
    assert len(result.trials) == 12
    assert [item.kind for item in result.trials[:3]] == [
        TrialKind.CONTROL,
        TrialKind.TARGET_CHANGE,
        TrialKind.PERMISSION_CHANGE,
    ]
    assert len(disturbances.applied) == 8
    assert {item.type.value for item in disturbances.applied} == {
        "target_change",
        "permission_change",
    }
    assert len(permissions.provisioned) == len(permissions.restored) == 12
    assert permissions.profiles == ["p0-full-authorized"] * 12
    assert {item.permission_profile_id for item in result.trials} == {
        "p0-full-authorized"
    }
    assert len(resetter.calls) == 12
    assert all(item.platform_valid for item in result.trials)
    assert all(
        item.model_identity.verification == "provider_verified"
        for item in result.trials
    )
    assert len(result.model_summaries) == 4
    assert all(summary.total_trials == 3 for summary in result.model_summaries)
    assert all(summary.valid_trials == 3 for summary in result.model_summaries)
    assert all(summary.pass_rate == 1.0 for summary in result.model_summaries)


def test_campaign_runs_every_model_for_one_harness_as_a_complete_variant(
    tmp_path: Path,
):
    engine, request, permissions, disturbances, resetter = _engine(tmp_path)
    request = request.model_copy(
        update={
            "model_matrix": (
                HarnessModelSelection(
                    harness=HarnessKind.CODEX,
                    models=("gpt-5.6", "gpt-5.5"),
                ),
            )
        }
    )

    result = engine.run(request)

    assert result.platform_status is PlatformStatus.COMPLETED
    assert len(result.trials) == 6
    assert [item.model_identity.requested_alias for item in result.trials] == [
        "gpt-5.6",
        "gpt-5.6",
        "gpt-5.6",
        "gpt-5.5",
        "gpt-5.5",
        "gpt-5.5",
    ]
    assert len(permissions.provisioned) == len(permissions.restored) == 6
    assert len(disturbances.applied) == 4
    assert len(resetter.calls) == 6
    assert [summary.requested_alias for summary in result.model_summaries] == [
        "gpt-5.6",
        "gpt-5.5",
    ]
    assert all(summary.pass_rate == 1.0 for summary in result.model_summaries)


def test_campaign_passes_the_selected_static_permission_profile_to_every_trial(
    tmp_path: Path,
):
    engine, request, permissions, _disturbances, _resetter = _engine(tmp_path)
    request = request.model_copy(
        update={
            "model_matrix": (
                HarnessModelSelection(
                    harness=HarnessKind.CODEX, models=("gpt-5.6",)
                ),
            ),
            "permission_profile": PermissionProfile(
                profile_id="p1-no-telemetry-mcp",
                mcp_servers=(McpServerName.K8S_RO, McpServerName.SOURCE_RO),
            ),
            "trial_kinds": (TrialKind.CONTROL,),
        }
    )

    result = engine.run(request)

    assert result.platform_status is PlatformStatus.COMPLETED
    assert permissions.profiles == ["p1-no-telemetry-mcp"]
    assert len(result.trials) == 1
    assert result.trials[0].kind is TrialKind.CONTROL


def test_static_permission_qualification_can_run_control_only_for_four_harnesses(
    tmp_path: Path,
):
    engine, request, permissions, disturbances, resetter = _engine(tmp_path)
    request = request.model_copy(update={"trial_kinds": (TrialKind.CONTROL,)})

    result = engine.run(request)

    assert result.platform_status is PlatformStatus.COMPLETED
    assert len(result.trials) == 4
    assert all(item.kind is TrialKind.CONTROL for item in result.trials)
    assert len(permissions.provisioned) == len(permissions.restored) == 4
    assert disturbances.applied == []
    assert len(resetter.calls) == 4


def test_campaign_blocks_unknown_or_protocol_incompatible_model_before_environment(
    tmp_path: Path,
):
    engine, request, permissions, disturbances, resetter = _engine(tmp_path)
    unknown = request.model_copy(
        update={
            "model_matrix": (
                HarnessModelSelection(
                    harness=HarnessKind.CODEX, models=("unknown-model",)
                ),
            )
        }
    )

    unknown_result = engine.run(unknown)

    assert unknown_result.platform_status is PlatformStatus.BLOCKED
    assert unknown_result.error == "model alias is not registered: unknown-model"
    assert permissions.provisioned == []
    assert disturbances.applied == []
    assert resetter.calls == []

    incompatible = request.model_copy(
        update={
            "model_matrix": (
                HarnessModelSelection(
                    harness=HarnessKind.CLAUDE_CODE, models=("gpt-5.6",)
                ),
            )
        }
    )
    incompatible_result = engine.run(incompatible)

    assert incompatible_result.platform_status is PlatformStatus.BLOCKED
    assert "incompatible with claude-code" in str(incompatible_result.error)


def test_campaign_blocks_before_harness_when_application_traffic_is_absent(tmp_path: Path):
    engine, request, permissions, disturbances, resetter = _engine(tmp_path, gate=False)
    result = engine.run(request)

    assert result.platform_status is PlatformStatus.BLOCKED
    assert result.trials == ()
    assert permissions.provisioned == []
    assert disturbances.applied == []
    assert resetter.calls == []


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


class ModelMismatchRunner(Runner):
    def run(self, **kwargs):
        report = super().run(**kwargs)
        return report.model_copy(
            update={"provider_reported_model": "unexpected-provider-model"}
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
    assert len(result.trials) == 12
    assert all(item.platform_valid is False for item in result.trials)
    assert all(item.agent_verdict is AgentVerdict.CASE_INVALID for item in result.trials)


def test_invalid_agent_output_after_successful_process_is_a_valid_agent_failure(tmp_path: Path):
    engine, request, _permissions, _disturbances, _resetter = _engine(tmp_path)
    engine.harness_runner = InvalidAgentOutputRunner()

    result = engine.run(request)

    assert result.trials[0].platform_valid is True
    assert result.trials[0].agent_verdict is AgentVerdict.FAIL


def test_provider_model_mismatch_is_case_invalid_not_model_failure(tmp_path: Path):
    engine, request, _permissions, _disturbances, _resetter = _engine(tmp_path)
    request = request.model_copy(
        update={
            "model_matrix": (
                HarnessModelSelection(
                    harness=HarnessKind.CODEX, models=("gpt-5.6",)
                ),
            )
        }
    )
    engine.harness_runner = ModelMismatchRunner()

    result = engine.run(request)

    assert result.platform_status is PlatformStatus.FAILED
    assert len(result.trials) == 3
    assert all(item.platform_valid is False for item in result.trials)
    assert all(item.agent_verdict is AgentVerdict.CASE_INVALID for item in result.trials)
    assert all(
        item.model_identity.verification == "provider_mismatch"
        for item in result.trials
    )
    assert result.model_summaries[0].valid_trials == 0
    assert result.model_summaries[0].case_invalid_count == 3
    assert result.model_summaries[0].pass_rate is None
