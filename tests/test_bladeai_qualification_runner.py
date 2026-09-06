from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.qualify_bladeai_task as qualify_bladeai_task
from stage2_service.artifacts import ArtifactStore
from stage2_service.bladeai_qualification_runner import (
    BladeAIQualificationRunner,
    prepare_output_dir,
)
from stage2_service.contracts import (
    AgentVerdict,
    CapabilityProfile,
    HarnessKind,
    HarnessReport,
    RecoveryResult,
)
from stage2_service.gateway_config import GatewayConfigSnapshot
from stage2_service.platform_ledger import PlatformLedger


MODEL = "gpt-5.5"
CANARY_POD = "bladeai-canary"
CANARY_UID = "11111111-2222-4333-8444-555555555555"


def _pod(*, labels=None, ready: bool = True) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "namespace": "otel-demo",
            "name": CANARY_POD,
            "uid": CANARY_UID,
            "labels": labels or {"resiliencebenchmark.io/qualification": "bladeai-wp8"},
        },
        "status": {
            "conditions": [
                {"type": "Ready", "status": "True" if ready else "False"},
            ],
        },
    }


def _gateway_snapshot(tmp_path: Path) -> GatewayConfigSnapshot:
    path = tmp_path / "gateway.yaml"
    path.write_text(
        """
model_list:
  - model_name: gpt-5.5
    litellm_params:
      model: openai/gpt-5.5
      api_base: https://provider.example/v1
      api_key: os.environ/STAGE2_GATEWAY_API_KEY
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return GatewayConfigSnapshot.from_file(path, required_aliases=(MODEL,))


class _Lock:
    def __init__(self, log: list[str]):
        self.log = log

    def acquire(self, *, owner: str):
        self.log.append(f"lock:{owner}")
        return self

    def __enter__(self):
        self.log.append("lock-enter")
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.log.append("lock-exit")


class _Traffic:
    def __init__(self, log: list[str]):
        self.log = log

    def start_sampling(self):
        self.log.append("traffic-start")

    def close(self):
        self.log.append("traffic-close")


class _Core:
    def __init__(self, pod: dict):
        self.pod = pod
        self.requests: list[tuple[str, str]] = []

    def read_namespaced_pod(self, *, name: str, namespace: str):
        self.requests.append((namespace, name))
        return self.pod


class _Issuer:
    def __init__(self, log: list[str]):
        self.log = log
        self.bound_target = None

    def issue(self, trial_id: str, *, namespace: str, target):
        self.log.append("issue")
        self.bound_target = target
        return "b" * 40


class _Permissions:
    def __init__(self, log: list[str]):
        self.log = log
        self.runtime = None

    def provision(self, campaign_id, trial_id, harness, episode, runtime):
        self.log.append("provision")
        self.runtime = runtime
        return CapabilityProfile(
            harness=harness,
            mcp_servers=("k8s_ro", "telemetry_ro", "source_ro", "chaos_control", "harness_channel"),
            mcp_tools=("chaos_create_experiment", "chaos_destroy_experiment"),
            kubernetes_rules=(),
            direct_kubeconfig=False,
            allowed_fault_types=("network-delay", "cpu-load"),
            expires_at=datetime.now(UTC),
        )

    def restore(self, trial_id: str):
        self.log.append("restore")
        return {"verified": True}


class _HarnessRunner:
    def __init__(self, log: list[str], *, raises: bool = False):
        self.log = log
        self.raises = raises
        self.kwargs = None

    def run(self, **kwargs):
        self.log.append("run")
        self.kwargs = kwargs
        if self.raises:
            raise RuntimeError("boom")
        trial_id = kwargs["trial_id"]
        return HarnessReport(
            status="completed",
            agent_verdict=AgentVerdict.PASS,
            lifecycle_events=(),
            artifact_refs=(
                f"{kwargs['campaign_id']}/{trial_id}/canonical-events.jsonl",
                f"{kwargs['campaign_id']}/{trial_id}/gateway-requests.json",
                f"{kwargs['campaign_id']}/{trial_id}/bladeai-launch.json",
                f"{kwargs['campaign_id']}/{trial_id}/bladeai-shim-evidence.json",
            ),
            final_output={
                "trial_id": trial_id,
                "model_alias": MODEL,
                "bladeai_launch": {"schema_version": "stage2-bladeai-launch.v1"},
                "bladeai_shim_evidence": [{"_resbench": {"controller_call_id": "create"}}],
            },
        )


class _Finalizer:
    def __init__(self, log: list[str]):
        self.log = log

    def finalize(self, trial_id, episode, runtime, report):
        self.log.append("finalize")
        return RecoveryResult(
            agent_attempted=True,
            agent_recovery_verified=True,
            controller_cleanup_verified=True,
            fault_absent=True,
            business_recovery_verified=True,
            chaos_inventory_clear=True,
            recovery_attribution={
                "trial_id": trial_id,
                "cleanup_handle": runtime.cleanup_handle,
                "target_uid": runtime.target.uid,
            },
            main_fault_ever_active=True,
            main_fault_target_verified=True,
            fault_effect_verified=False,
        )


class _Supervisor:
    def __init__(self, log: list[str]):
        self.log = log

    def stop(self):
        self.log.append("stop")


def _components(tmp_path: Path, log: list[str], *, pod=None, runner_raises: bool = False):
    return SimpleNamespace(
        traffic=_Traffic(log),
        preparer=SimpleNamespace(core_api=_Core(pod or _pod())),
        issuer=_Issuer(log),
        permissions=_Permissions(log),
        token_registry=SimpleNamespace(platform_ledger=PlatformLedger(tmp_path / "ledger")),
        harness_runner=_HarnessRunner(log, raises=runner_raises),
        finalizer=_Finalizer(log),
        supervisor=_Supervisor(log),
    )


def _system(tmp_path: Path, components) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(
            repo_root=tmp_path,
            artifact_root=tmp_path / "artifacts",
            gateway_snapshot=_gateway_snapshot(tmp_path),
        ),
        build_runtime=lambda episode, model_by_harness, *, namespace: components,
    )


def test_bladeai_wp8_runner_binds_existing_labelled_canary_and_writes_record(tmp_path, monkeypatch):
    log: list[str] = []
    components = _components(tmp_path, log)
    monkeypatch.setattr(
        "stage2_service.bladeai_qualification_runner.load_fixed_episode",
        lambda ref, *, root: SimpleNamespace(ref=SimpleNamespace(episode_id="EPI-TEST-BLADEAI-WP8")),
    )
    monkeypatch.setattr(
        "stage2_service.bladeai_qualification_runner.fixed_otel_episode_ref",
        lambda repo_root: SimpleNamespace(episode_id="EPI-TEST-BLADEAI-WP8"),
    )
    captured_refs = {}
    def fake_evaluate_wp8_artifacts(refs, *, artifact_root, gateway):
        captured_refs["refs"] = list(refs)
        return {
            "status": "passed",
            "passed": True,
            "qualification_type": "BLADEAI_WP8_FULL_CHAIN_QUALIFICATION",
            "failure_reasons": [],
        }

    monkeypatch.setattr(
        "stage2_service.bladeai_qualification_runner.evaluate_wp8_artifacts",
        fake_evaluate_wp8_artifacts,
    )

    result = BladeAIQualificationRunner(
        _system(tmp_path, components),
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        runtime_lock=_Lock(log),
    ).run(model=MODEL, canary_pod=CANARY_POD, output_dir=tmp_path / "out")

    assert result.record["passed"] is True
    assert result.output.is_file()
    assert components.preparer.core_api.requests == [("otel-demo", CANARY_POD)]
    assert components.issuer.bound_target.uid == CANARY_UID
    assert components.permissions.runtime.target.uid == CANARY_UID
    assert components.harness_runner.kwargs["harness"] is HarnessKind.BLADEAI
    assert components.harness_runner.kwargs["capability"].allowed_fault_types == ("network-delay",)
    assert CANARY_UID not in components.harness_runner.kwargs["base_prompt"]
    assert {
        "harness-report.json",
        "runtime-context.json",
        "recovery.json",
        "canary-evidence.json",
        "canonical-events.jsonl",
        "gateway-requests.json",
        "bladeai-launch.json",
        "bladeai-shim-evidence.json",
    } <= {Path(ref).name for ref in captured_refs["refs"]}


def test_bladeai_wp8_runner_rejects_unlabelled_or_not_ready_canary(tmp_path, monkeypatch):
    log: list[str] = []
    components = _components(tmp_path, log, pod=_pod(labels={}, ready=False))
    monkeypatch.setattr(
        "stage2_service.bladeai_qualification_runner.load_fixed_episode",
        lambda ref, *, root: SimpleNamespace(ref=SimpleNamespace(episode_id="EPI-TEST-BLADEAI-WP8")),
    )
    monkeypatch.setattr(
        "stage2_service.bladeai_qualification_runner.fixed_otel_episode_ref",
        lambda repo_root: SimpleNamespace(episode_id="EPI-TEST-BLADEAI-WP8"),
    )

    result = BladeAIQualificationRunner(
        _system(tmp_path, components),
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        runtime_lock=_Lock(log),
    ).run(model=MODEL, canary_pod=CANARY_POD, output_dir=tmp_path / "out")

    assert result.record["passed"] is False
    assert any(reason.startswith("runner_error:ValueError") for reason in result.record["failure_reasons"])
    assert "run" not in log
    assert "finalize" not in log


def test_bladeai_wp8_runner_finalizes_before_restore_after_harness_exception(tmp_path, monkeypatch):
    log: list[str] = []
    components = _components(tmp_path, log, runner_raises=True)
    monkeypatch.setattr(
        "stage2_service.bladeai_qualification_runner.load_fixed_episode",
        lambda ref, *, root: SimpleNamespace(ref=SimpleNamespace(episode_id="EPI-TEST-BLADEAI-WP8")),
    )
    monkeypatch.setattr(
        "stage2_service.bladeai_qualification_runner.fixed_otel_episode_ref",
        lambda repo_root: SimpleNamespace(episode_id="EPI-TEST-BLADEAI-WP8"),
    )

    result = BladeAIQualificationRunner(
        _system(tmp_path, components),
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        runtime_lock=_Lock(log),
    ).run(model=MODEL, canary_pod=CANARY_POD, output_dir=tmp_path / "out")

    assert result.record["passed"] is False
    assert log.index("finalize") < log.index("restore") < log.index("stop") < log.index("traffic-close")


def test_bladeai_wp8_output_dir_must_not_overwrite_existing_files(tmp_path):
    output = tmp_path / "out"
    output.mkdir()
    (output / "old-failure.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="new or empty"):
        prepare_output_dir(output)


def test_bladeai_wp8_cli_requires_execute_before_loading_runtime(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        qualify_bladeai_task.Stage2RuntimeConfig,
        "from_env",
        lambda: (_ for _ in ()).throw(AssertionError("runtime must not load without --execute")),
    )

    code = qualify_bladeai_task.main([
        "--model",
        MODEL,
        "--canary-pod",
        CANARY_POD,
        "--output-dir",
        str(tmp_path / "out"),
    ])

    assert code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "rejected"
