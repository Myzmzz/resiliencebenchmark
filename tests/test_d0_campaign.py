from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from harness.d0.adapters import AdapterResult
from harness.d0.campaign import D0Campaign, D0CampaignConfig
from harness.d0.common import AGENTS, append_jsonl, redact_sensitive_text, utc_now
from harness.d0.inventory import collect_execution_inventory
from harness.d0.observer import KubectlD0Observer
from harness.d0.recompute import recompute_trial
from harness.d0.facade import D0ChaosFacade
from mcp_servers.chaos_control.service import KubectlChaosBackend
from mcp_servers.d0_chaos_control.server import D0AuditedKubectlBackend


def remote_host(_expected: str):
    return {
        "expected_host_id": "1.94.151.57",
        "declared_host_id": "1.94.151.57",
        "hostname": "remote-test-host",
        "platform": "Linux",
        "pid": 123,
        "verified": True,
        "observed_at": utc_now(),
    }


def fake_inventory(**_kwargs):
    return {
        "schema_version": "d0-execution-inventory.v1",
        "agents": {name: {"available": True} for name in AGENTS},
        "models": {name: {"requested_alias": name} for name in AGENTS},
    }


class FakeAdapter:
    def __init__(self, name: str, *, recovery: bool = True):
        self.name = name
        self.recovery = recovery

    def update_environment(self, _values):
        return None

    def run(self, *, prompt, trial_id, artifact_dir, event_sink):
        event_sink(
            {
                "ts": utc_now(),
                "actor": "agent",
                "agent": self.name,
                "kind": "tool_call",
                "tool": "chaos_create_experiment",
            }
        )
        event_sink(
            {
                "ts": utc_now(),
                "actor": "agent",
                "agent": self.name,
                "kind": "tool_call",
                "tool": "chaos_get_experiment",
            }
        )
        if self.recovery:
            event_sink(
                {
                    "ts": utc_now(),
                    "actor": "agent",
                    "agent": self.name,
                    "kind": "tool_call",
                    "tool": "chaos_destroy_experiment",
                }
            )
            event_sink(
                {
                    "ts": utc_now(),
                    "actor": "agent",
                    "agent": self.name,
                    "kind": "tool_call",
                    "tool": "chaos_recovery_status",
                }
            )
        return AdapterResult(
            status="finished",
            started_at=utc_now(),
            finished_at=utc_now(),
            process_status="completed",
            artifact_ref=artifact_dir.name,
            agent_recovery_requested=self.recovery,
            tool_calls=4 if self.recovery else 2,
            confirmations=1,
        )


def test_d0_maps_unified_gateway_config_into_bladeai_environment(tmp_path):
    adapters = {name: FakeAdapter(name) for name in AGENTS}
    campaign = D0Campaign(
        D0CampaignConfig(
            repo_root=tmp_path,
            artifact_root=tmp_path / "artifacts",
            kubeconfig=tmp_path / "kubeconfig",
            episode_file=tmp_path / "episode.yaml",
        ),
        environment={
            "RESBENCH_LLM_BASE_URL": "https://gateway.example/v1",
            "RESBENCH_LLM_API_KEY": "secret-value",
            "RESBENCH_D0_BLADEAI_MODEL": "gpt-5.6-sol",
        },
        adapters=adapters,
    )

    assert campaign.environment["BLADE_AI_API_BASE_URL"] == "https://gateway.example/v1"
    assert campaign.environment["BLADE_AI_LLM_API_KEY"] == "secret-value"
    assert campaign.environment["BLADE_AI_MODEL_NAME"] == "gpt-5.6-sol"


@dataclass
class FakeState:
    baseline_cpu: dict[str, int] = field(default_factory=lambda: {"accounting-pod": 3})
    initial_cr_names: set[str] = field(default_factory=set)
    new_cr_names: set[str] = field(default_factory=lambda: {"d0-cr"})
    effect_confirmed_at: str | None = "2026-09-01T00:00:00Z"
    effect_monotonic: float | None = 1.0
    recovery_observed_at: str | None = "2026-09-01T00:05:00Z"
    maximum_cpu_millicores: int = 800
    samples: int = 6
    errors: list[str] = field(default_factory=list)
    foreign_cr_names: set[str] = field(default_factory=set)


class FakeObserver:
    def __init__(self, *, artifact_dir: Path, **_kwargs):
        self.artifact_dir = artifact_dir
        self.state = FakeState()

    def prepare(self):
        sample = {
            "ts": "2026-09-01T00:00:00Z",
            "phase": "before",
            "pods": [
                {
                    "name": "accounting-pod",
                    "uid": "uid-1",
                    "ready": True,
                    "restart_count": 0,
                    "cpu_millicores": 3,
                }
            ],
            "chaosblades": [],
        }
        append_jsonl(self.artifact_dir / "oracle-samples.jsonl", sample)
        append_jsonl(
            self.artifact_dir / "oracle-samples.jsonl",
            {
                **sample,
                "ts": "2026-09-01T00:03:00Z",
                "phase": "watch",
                "pods": [{**sample["pods"][0], "cpu_millicores": 800}],
                "chaosblades": [{"name": "d0-cr", "phase": "Success"}],
            },
        )
        return sample

    def start(self):
        return None

    def stop(self):
        return None

    def fallback_cleanup(self):
        return {"requested": True, "verified": True, "deleted": ["d0-cr"]}

    def fault_duration_seconds(self):
        return 300.0

    def wait_recovery_convergence(self, *, timeout_seconds=60):
        return {"verified": True, "sample": {"ts": utc_now()}}


class FakeFacade:
    def __init__(self, **_kwargs):
        pass

    def start(self):
        return {"RESBENCH_CHAOS_CONTROL_MCP_URL": "http://127.0.0.1:19000/mcp"}

    def stop(self):
        return None

    def public_context(self):
        return {"url": "http://127.0.0.1:19000/mcp"}


class FakeBladeAIServer:
    def __init__(self, **_kwargs):
        pass

    def start(self):
        return None

    def stop(self):
        return None


def config(tmp_path: Path) -> D0CampaignConfig:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("test", encoding="utf-8")
    return D0CampaignConfig(
        repo_root=tmp_path,
        artifact_root=tmp_path / "artifacts",
        kubeconfig=kubeconfig,
        episode_file=tmp_path / "episode.yaml",
        sample_seconds=1,
        effect_wait_seconds=1,
        recovery_deadline_seconds=1,
    )


def test_campaign_runs_four_agents_and_builds_visualization(tmp_path, monkeypatch):
    monkeypatch.setenv("RESBENCH_D0_EXECUTION_HOST_ID", "1.94.151.57")
    adapters = {name: FakeAdapter(name) for name in AGENTS}
    report = D0Campaign(
        config(tmp_path),
        environment={"RESBENCH_D0_EXECUTION_HOST_ID": "1.94.151.57"},
        adapters=adapters,
        observer_factory=FakeObserver,
        host_evidence_provider=remote_host,
        inventory_provider=fake_inventory,
        facade_factory=FakeFacade,
        bladeai_server_factory=FakeBladeAIServer,
    ).run("d0-test-campaign")

    assert report["status"] == "QUALIFIED"
    assert [item["agent"] for item in report["results"]] == list(AGENTS)
    assert {item["status"] for item in report["results"]} == {"PASS"}
    artifact = Path(report["artifact_dir"])
    assert (artifact / "visualization" / "index.html").is_file()
    assert (artifact / "manifest.sha256").is_file()
    assert json.loads((artifact / "campaign.json").read_text())["status"] == "QUALIFIED"


def test_campaign_rejects_wrong_execution_host(tmp_path, monkeypatch):
    monkeypatch.delenv("RESBENCH_D0_EXECUTION_HOST_ID", raising=False)
    with pytest.raises(RuntimeError, match="WRONG_EXECUTION_HOST"):
        D0Campaign(
            config(tmp_path),
            environment={},
            adapters={name: FakeAdapter(name) for name in AGENTS},
            observer_factory=FakeObserver,
            inventory_provider=fake_inventory,
        ).run("d0-test-campaign")


def test_fallback_cleanup_is_not_agent_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("RESBENCH_D0_EXECUTION_HOST_ID", "1.94.151.57")

    class UnrecoveredObserver(FakeObserver):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.state.recovery_observed_at = None

    report = D0Campaign(
        config(tmp_path),
        environment={"RESBENCH_D0_EXECUTION_HOST_ID": "1.94.151.57"},
        adapters={name: FakeAdapter(name, recovery=False) for name in AGENTS},
        observer_factory=UnrecoveredObserver,
        host_evidence_provider=remote_host,
        inventory_provider=fake_inventory,
        facade_factory=FakeFacade,
        bladeai_server_factory=FakeBladeAIServer,
    ).run("d0-fallback-campaign")

    assert report["status"] == "QUALIFICATION_FAILED"
    assert {item["status"] for item in report["results"]} == {"FALLBACK_RECOVERED"}
    assert all(item["fallback_cleanup_used"] for item in report["results"])


def test_observer_prepare_waits_for_residue_free_low_cpu_state(tmp_path, monkeypatch):
    monkeypatch.setattr("harness.d0.observer.time.sleep", lambda _seconds: None)
    observer = KubectlD0Observer(
        kubeconfig=tmp_path / "kubeconfig",
        artifact_dir=tmp_path,
        trial_id="trial-1",
    )
    samples = iter(
        [
            {
                "ts": "2026-09-01T00:00:00Z",
                "pods": [
                    {
                        "name": "accounting-pod",
                        "uid": "uid-1",
                        "ready": True,
                        "restart_count": 0,
                        "cpu_millicores": 900,
                    }
                ],
                "chaosblades": [{"name": "old-cr"}],
            },
            {
                "ts": "2026-09-01T00:00:05Z",
                "pods": [
                    {
                        "name": "accounting-pod",
                        "uid": "uid-1",
                        "ready": True,
                        "restart_count": 0,
                        "cpu_millicores": 5,
                    }
                ],
                "chaosblades": [],
            },
        ]
    )
    monkeypatch.setattr(observer, "snapshot", lambda: next(samples))

    result = observer.prepare(convergence_timeout_seconds=5)

    assert result["pods"][0]["cpu_millicores"] == 5
    assert observer.state.baseline_cpu == {"accounting-pod": 5}
    rows = [json.loads(line) for line in (tmp_path / "oracle-samples.jsonl").read_text().splitlines()]
    assert [row["phase"] for row in rows] == ["precondition_wait", "before"]


def test_fallback_cleanup_retains_delete_timeout_as_reset_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("harness.d0.observer.time.sleep", lambda _seconds: None)
    observer = KubectlD0Observer(
        kubeconfig=tmp_path / "kubeconfig",
        artifact_dir=tmp_path,
        trial_id="trial-1",
    )
    observer.state.new_cr_names = {"d0-cr"}
    snapshots = iter(
        [
            {
                "ts": "2026-09-01T00:00:00Z",
                "pods": [],
                "chaosblades": [{"name": "d0-cr"}],
            },
            {
                "ts": "2026-09-01T00:00:01Z",
                "pods": [],
                "chaosblades": [],
            },
        ]
    )
    monkeypatch.setattr(observer, "snapshot", lambda: next(snapshots))

    def timeout_delete(_args, **_kwargs):
        raise __import__("subprocess").TimeoutExpired("kubectl", 60)

    monkeypatch.setattr(observer, "_run", timeout_delete)

    result = observer.fallback_cleanup()

    assert result["remaining"] == []
    assert result["errors"] == ["d0-cr:delete-timeout"]
    assert result["verified"] is False


def test_recompute_counts_delayed_restart_after_cpu_recovery(tmp_path):
    trial = tmp_path / "deepseek-harness"
    trial.mkdir()
    (trial / "result.json").write_text(
        json.dumps({"agent": "deepseek-harness", "fallback": {}}),
        encoding="utf-8",
    )
    (trial / "trial.json").write_text(
        json.dumps({"trial_id": "trial-1"}), encoding="utf-8"
    )
    samples = [
        {
            "ts": "2026-09-01T00:00:00Z",
            "phase": "before",
            "pods": [{"name": "accounting-pod", "ready": True, "restart_count": 0, "cpu_millicores": 5}],
            "chaosblades": [],
        },
        {
            "ts": "2026-09-01T00:00:10Z",
            "phase": "watch",
            "pods": [{"name": "accounting-pod", "ready": True, "restart_count": 0, "cpu_millicores": 900}],
            "chaosblades": [{"name": "d0-cr", "run_id": "trial-1"}],
        },
        {
            "ts": "2026-09-01T00:05:10Z",
            "phase": "post_recovery",
            "pods": [{"name": "accounting-pod", "ready": True, "restart_count": 0, "cpu_millicores": 5}],
            "chaosblades": [],
        },
        {
            "ts": "2026-09-01T00:05:40Z",
            "phase": "post_recovery",
            "pods": [{"name": "accounting-pod", "ready": True, "restart_count": 2, "cpu_millicores": 4}],
            "chaosblades": [],
        },
    ]
    for row in samples:
        append_jsonl(trial / "oracle-samples.jsonl", row)
    append_jsonl(
        trial / "all-events.jsonl",
        {"ts": "2026-09-01T00:00:05Z", "kind": "tool_call", "tool": "chaos_create_experiment"},
    )
    (trial / "controller-commands.jsonl").write_text("{}\n", encoding="utf-8")

    result = recompute_trial(trial, "deepseek-harness")

    assert result["restart_count_delta"] == 2
    assert result["effect_duration_seconds"] == 300.0
    assert result["recovery_observed"] is True


def test_controller_command_omits_structured_kubernetes_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("RESBENCH_D0_EXECUTION_HOST_ID", "1.94.151.57")

    def runner(_argv, _timeout):
        return subprocess.CompletedProcess(
            [],
            0,
            stdout='{"env":[{"name":"DB","value":"Password=short-secret"}]}',
            stderr="",
        )

    observer = KubectlD0Observer(
        kubeconfig=tmp_path / "kubeconfig",
        artifact_dir=tmp_path,
        trial_id="trial-1",
        runner=runner,
    )
    observer._run(["get", "pods", "-o", "json"])
    row = json.loads((tmp_path / "controller-commands.jsonl").read_text())

    assert "short-secret" not in row["stdout"]
    assert "parsed facts retained" in row["stdout"]
    assert row["execution_host_id"] == "1.94.151.57"
    assert row["started_at"] and row["finished_at"] and row["command_id"]


def test_recompute_distinguishes_created_cr_without_effect(tmp_path):
    trial = tmp_path / "codex"
    trial.mkdir()
    (trial / "result.json").write_text(
        json.dumps({"agent": "codex", "adapter": {"status": "finished"}}),
        encoding="utf-8",
    )
    (trial / "trial.json").write_text(
        json.dumps({"trial_id": "trial-1"}), encoding="utf-8"
    )
    append_jsonl(
        trial / "oracle-samples.jsonl",
        {
            "ts": "2026-09-01T00:00:00Z",
            "phase": "before",
            "pods": [
                {
                    "name": "accounting-pod",
                    "ready": True,
                    "restart_count": 0,
                    "cpu_millicores": 5,
                }
            ],
            "chaosblades": [],
        },
    )
    append_jsonl(
        trial / "oracle-samples.jsonl",
        {
            "ts": "2026-09-01T00:00:10Z",
            "phase": "watch",
            "pods": [
                {
                    "name": "accounting-pod",
                    "ready": True,
                    "restart_count": 1,
                    "cpu_millicores": 20,
                }
            ],
            "chaosblades": [{"name": "d0-cr", "run_id": "trial-1"}],
        },
    )
    append_jsonl(
        trial / "all-events.jsonl",
        {
            "ts": "2026-09-01T00:00:05Z",
            "kind": "tool_call",
            "tool": "chaos_create_experiment",
        },
    )
    (trial / "controller-commands.jsonl").write_text("{}\n", encoding="utf-8")
    (trial / "run-trace.json").write_text(
        json.dumps({"events": [], "final_output": {"status": "completed"}}),
        encoding="utf-8",
    )
    (trial / "stdout.txt").write_text("finished", encoding="utf-8")
    (trial / "stderr.txt").write_text("", encoding="utf-8")

    result = recompute_trial(trial, "codex")

    assert result["injection_observed"] is True
    assert result["effect_observed"] is False
    assert result["status"] == "EFFECT_UNVERIFIED"


def test_observer_attributes_only_current_trial_cr(tmp_path):
    observer = KubectlD0Observer(
        kubeconfig=tmp_path / "kubeconfig",
        artifact_dir=tmp_path,
        trial_id="trial-1",
    )
    observer.state.baseline_cpu = {"accounting-pod": 4}
    observer._apply(
        {
            "ts": "2026-09-01T00:00:10Z",
            "pods": [
                {
                    "name": "accounting-pod",
                    "uid": "uid-1",
                    "ready": True,
                    "restart_count": 0,
                    "cpu_millicores": 900,
                }
            ],
            "chaosblades": [
                {"name": "ours", "run_id": "trial-1", "owner": "chaos_control"},
                {"name": "foreign", "run_id": "another-trial", "owner": "chaos_control"},
            ],
        }
    )

    assert observer.state.new_cr_names == {"ours"}
    assert observer.state.foreign_cr_names == {"foreign"}
    assert observer.state.effect_confirmed_at == "2026-09-01T00:00:10Z"


def test_recompute_marks_foreign_cr_as_case_invalid_not_agent_injection(tmp_path):
    trial = tmp_path / "deepseek-harness"
    trial.mkdir()
    (trial / "trial.json").write_text(
        json.dumps({"trial_id": "trial-1"}), encoding="utf-8"
    )
    (trial / "result.json").write_text(
        json.dumps({"agent": "deepseek-harness", "adapter": {"status": "finished"}}),
        encoding="utf-8",
    )
    for row in (
        {
            "ts": "2026-09-01T00:00:00Z",
            "phase": "before",
            "pods": [{"name": "accounting-pod", "ready": True, "restart_count": 0, "cpu_millicores": 4}],
            "chaosblades": [],
        },
        {
            "ts": "2026-09-01T00:00:10Z",
            "phase": "watch",
            "pods": [{"name": "accounting-pod", "ready": True, "restart_count": 0, "cpu_millicores": 4}],
            "chaosblades": [{"name": "foreign", "run_id": "another-trial"}],
        },
    ):
        append_jsonl(trial / "oracle-samples.jsonl", row)
    append_jsonl(
        trial / "all-events.jsonl",
        {"ts": "2026-09-01T00:00:05Z", "kind": "agent_message"},
    )
    (trial / "controller-commands.jsonl").write_text("{}\n", encoding="utf-8")
    (trial / "run-trace.json").write_text(
        json.dumps({"events": [], "final_output": {"status": "completed"}}),
        encoding="utf-8",
    )
    (trial / "stdout.txt").write_text("finished", encoding="utf-8")
    (trial / "stderr.txt").write_text("", encoding="utf-8")
    (trial / "dsh-session-00.jsonl").write_text("{}\n", encoding="utf-8")

    result = recompute_trial(trial, "deepseek-harness")

    assert result["status"] == "CASE_INVALID"
    assert result["injection_observed"] is False
    assert result["foreign_crs_observed"] == ["foreign"]


def test_known_stuck_owned_cr_can_be_force_finalized(tmp_path, monkeypatch):
    observer = KubectlD0Observer(
        kubeconfig=tmp_path / "kubeconfig",
        artifact_dir=tmp_path,
        trial_id="trial-1",
    )
    observer.state.new_cr_names = {"d0-cr"}
    cr = {
        "metadata": {
            "name": "d0-cr",
            "deletionTimestamp": "2026-09-01T00:05:00Z",
            "finalizers": ["finalizer.chaosblade.io"],
            "labels": {
                "benchmark.owner": "chaos_control",
                "benchmark.run_id": "trial-1",
                "benchmark.target_uid": "uid-1",
            },
        },
        "status": {
            "phase": "Destroying",
            "error": "invalid parameter container-id, can not find container by id",
        },
    }
    monkeypatch.setattr(observer, "_json", lambda _args: cr)
    monkeypatch.setattr(
        observer,
        "snapshot",
        lambda: {
            "pods": [
                {
                    "name": "accounting-pod",
                    "uid": "uid-1",
                    "ready": True,
                    "cpu_millicores": 4,
                }
            ],
            "chaosblades": [{"name": "d0-cr"}],
        },
    )
    monkeypatch.setattr(
        observer,
        "_run",
        lambda _args, **_kwargs: subprocess.CompletedProcess([], 0, "patched", ""),
    )

    outcome = observer._force_finalize_stuck_owned_cr("d0-cr")

    assert outcome == {
        "attempted": True,
        "verified_safe": True,
        "returncode": 0,
        "removed": True,
    }


def test_force_finalizer_rejects_foreign_run(tmp_path, monkeypatch):
    observer = KubectlD0Observer(
        kubeconfig=tmp_path / "kubeconfig",
        artifact_dir=tmp_path,
        trial_id="trial-1",
    )
    observer.state.new_cr_names = {"d0-cr"}
    monkeypatch.setattr(
        observer,
        "_json",
        lambda _args: {
            "metadata": {
                "name": "d0-cr",
                "deletionTimestamp": "2026-09-01T00:05:00Z",
                "finalizers": ["finalizer.chaosblade.io"],
                "labels": {
                    "benchmark.owner": "chaos_control",
                    "benchmark.run_id": "another-trial",
                    "benchmark.target_uid": "uid-1",
                },
            },
            "status": {
                "phase": "Destroying",
                "error": "invalid parameter container-id, can not find container by id",
            },
        },
    )
    monkeypatch.setattr(
        observer,
        "snapshot",
        lambda: {
            "pods": [
                {
                    "name": "accounting-pod",
                    "uid": "uid-1",
                    "ready": True,
                    "cpu_millicores": 4,
                }
            ],
            "chaosblades": [{"name": "d0-cr"}],
        },
    )

    outcome = observer._force_finalize_stuck_owned_cr("d0-cr")

    assert outcome["attempted"] is False
    assert outcome["verified_safe"] is False


def test_redaction_covers_short_passwords_and_capability_handles():
    value = (
        'Password=otelp "RESBENCH_MCP_TOKEN":"abc123" '
        '"cleanup_handle":"cleanup-secret" Authorization:Bearer-token'
    )
    redacted = redact_sensitive_text(value)

    assert "otelp" not in redacted
    assert "abc123" not in redacted
    assert "cleanup-secret" not in redacted


def test_execution_inventory_records_runtime_and_kubernetes_identity(tmp_path, monkeypatch):
    (tmp_path / "harness").mkdir()
    (tmp_path / "harness/harnesses.yaml").write_text(
        """version: test/v1
harnesses:
  bladeai: {entrypoint: {command: blade-ai, mode: service, prompt_transport: adapter_owned}}
  codex: {entrypoint: {command: codex, mode: cli, prompt_transport: stdin}}
  claude-code: {entrypoint: {command: claude, mode: cli, prompt_transport: stdin}}
  deepseek-harness: {entrypoint: {command: dsh, mode: cli, prompt_transport: positional}}
""",
        encoding="utf-8",
    )
    (tmp_path / "harness/models.yaml").write_text(
        """models:
  model-a: {upstream_model: model-a, protocol_candidates: [openai_responses]}
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("harness.d0.inventory.shutil.which", lambda *_args, **_kwargs: "/bin/echo")

    def runner(argv, **_kwargs):
        if argv[-2:] == ["config", "current-context"]:
            return subprocess.CompletedProcess(argv, 0, "remote-context\n", "")
        if "view" in argv:
            return subprocess.CompletedProcess(
                argv, 0, '{"clusters":[{"cluster":{"server":"https://cluster"}}]}', ""
            )
        if "whoami" in argv:
            return subprocess.CompletedProcess(
                argv,
                0,
                '{"status":{"userInfo":{"username":"system:serviceaccount:test:runner","groups":["test"]}}}',
                "",
            )
        return subprocess.CompletedProcess(argv, 0, "tool version 1\n", "")

    campaign_dir = tmp_path / "artifacts/campaign"
    campaign_dir.mkdir(parents=True)
    inventory = collect_execution_inventory(
        repo_root=tmp_path,
        kubeconfig=tmp_path / "kubeconfig",
        artifact_root=tmp_path / "artifacts",
        campaign_dir=campaign_dir,
        host=remote_host("1.94.151.57"),
        models={name: "model-a" for name in AGENTS},
        environment={"PATH": "/bin"},
        runner=runner,
    )

    assert set(inventory["agents"]) == set(AGENTS)
    assert inventory["kubernetes"]["context"] == "remote-context"
    assert inventory["kubernetes"]["api_server_sha256"]
    assert inventory["kubernetes"]["identity"]["username"].endswith(":runner")
    rows = [
        json.loads(line)
        for line in (campaign_dir / "campaign-controller-commands.jsonl")
        .read_text()
        .splitlines()
    ]
    assert len(rows) == 7
    assert all(row["execution_host_id"] == "1.94.151.57" for row in rows)


def test_d0_facade_backend_records_actual_kubectl_without_raw_json(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("RESBENCH_D0_EXECUTION_HOST_ID", "1.94.151.57")

    async def fake_kubectl(_self, _args, *, stdin=None):
        assert stdin is None
        return '{"metadata":{"name":"d0-cr"}}'

    monkeypatch.setattr(KubectlChaosBackend, "_kubectl", fake_kubectl)
    path = tmp_path / "controller-commands.jsonl"
    backend = D0AuditedKubectlBackend("kubectl", str(path))

    result = asyncio.run(
        backend._kubectl(
            ["--kubeconfig", "/secret/path", "get", "chaosblades.chaosblade.io", "-o", "json"]
        )
    )
    row = json.loads(path.read_text())

    assert result.startswith("{")
    assert "/secret/path" not in json.dumps(row)
    assert row["argv"][2] == "<kubeconfig>"
    assert row["execution_host_id"] == "1.94.151.57"
    assert "MCP response and Oracle" in row["stdout"]


def test_facade_stop_removes_controller_private_capabilities(tmp_path):
    trial = tmp_path / "trial"
    trial.mkdir()
    facade = D0ChaosFacade(
        repo_root=tmp_path,
        kubeconfig=tmp_path / "kubeconfig",
        trial_dir=trial,
        trial_id="trial-1",
        target={"namespace": "otel-demo", "name": "accounting-pod", "uid": "uid-1"},
        environment={},
    )
    assert facade.private.is_dir()

    facade.stop()

    assert not facade.private.exists()


def test_controller_cancels_live_agent_before_deadline_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("RESBENCH_D0_EXECUTION_HOST_ID", "1.94.151.57")

    class SlowAdapter:
        name = "codex"

        def __init__(self):
            self.cancelled = threading.Event()

        def update_environment(self, _values):
            return None

        def cancel(self):
            self.cancelled.set()
            return True

        def run(self, *, artifact_dir, event_sink, **_kwargs):
            event_sink(
                {
                    "ts": utc_now(),
                    "actor": "agent",
                    "kind": "tool_call",
                    "tool": "chaos_create_experiment",
                }
            )
            self.cancelled.wait(timeout=5)
            return AdapterResult(
                status="failed",
                started_at=utc_now(),
                finished_at=utc_now(),
                process_status="cancelled",
                artifact_ref=artifact_dir.name,
            )

    class ActiveUnrecoveredObserver(FakeObserver):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.state.recovery_observed_at = None

    cfg = config(tmp_path)
    cfg = D0CampaignConfig(
        **{
            **cfg.__dict__,
            "agents": ("codex",),
            "recovery_deadline_seconds": 1,
        }
    )
    adapter = SlowAdapter()
    report = D0Campaign(
        cfg,
        environment={"RESBENCH_D0_EXECUTION_HOST_ID": "1.94.151.57"},
        adapters={"codex": adapter},
        observer_factory=ActiveUnrecoveredObserver,
        host_evidence_provider=remote_host,
        inventory_provider=fake_inventory,
        facade_factory=FakeFacade,
        bladeai_server_factory=FakeBladeAIServer,
    ).run("d0-deadline-campaign")

    result = report["results"][0]
    assert adapter.cancelled.is_set()
    assert result["controller_deadline"]["recovery_deadline_exceeded"] is True
    assert result["controller_deadline"]["agent_thread_stopped"] is True
    assert result["fallback_cleanup_used"] is True
