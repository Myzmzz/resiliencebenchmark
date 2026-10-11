from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from stage2_service.reset import OtelDemoResetter, ResetError

PREFLIGHT_REPORT = json.dumps(
    {
        "schemaVersion": "resiliencebenchmark.application_deploy/v1",
        "modeExecution": "server-dry-run",
        "result": "server-dry-run-passed",
        "serverDryRun": {
            "checks": ["helm upgrade --install --dry-run=server"],
            "notSimulated": ["helm --wait readiness"],
        },
    }
)
# What deploy_application.py prints on stderr when the incident's namespace
# patch is refused during the preflight.
FORBIDDEN_PREFLIGHT_STDERR = json.dumps(
    {
        "schemaVersion": "resiliencebenchmark.application_deploy/v1",
        "phase": "failed",
        "error": (
            "command failed: kubectl --kubeconfig /k --request-timeout=30s apply "
            "--server-side --field-manager=helm --dry-run=server -o name -f -: "
            'Error from server (Forbidden): namespaces "otel-demo" is forbidden: User '
            '"system:serviceaccount:resiliencebenchmark-system:resbench-stage2-controller" '
            'cannot patch resource "namespaces" in API group "" in the namespace "otel-demo"'
        ),
    },
    indent=2,
)


class Runner:
    """Fake reset runner; ``preflight`` answers the ``--server-dry-run`` call.

    ``preflight`` is a CompletedProcess to return or an exception to raise;
    by default the preflight passes.
    """

    def __init__(self, preflight=None):
        self.calls = []
        self.preflight = preflight

    def run(self, argv, *, env, timeout):
        self.calls.append((argv, env, timeout))
        if "--server-dry-run" in argv:
            if isinstance(self.preflight, BaseException):
                raise self.preflight
            if self.preflight is not None:
                return self.preflight
            return subprocess.CompletedProcess(argv, 0, PREFLIGHT_REPORT, "")
        return subprocess.CompletedProcess(argv, 0, "ok", "")


class Gate:
    def qualify(self, _episode):
        return {
            "qualified": True,
            "built_in_load_generator_desired": 1,
            "built_in_load_generator_ready": 1,
            "active_chaosblade_count": 0,
        }


class Traffic:
    def __init__(self):
        self.reset_calls = 0
        self.wait_calls = 0

    def current(self):
        return {
            "application_owned": True,
            "load_generator_ready": True,
            "traffic_observed": True,
            "business_healthy": True,
            "success_rate": 1.0,
            "p95_ms": 100,
        }

    def reset_and_wait_healthy(self, **_kwargs):
        self.reset_calls += 1
        return self.current()

    def wait_until_healthy(self, **_kwargs):
        self.wait_calls += 1
        return self.current()


def test_resetter_uninstalls_and_reinstalls_application_without_managing_separate_workload(tmp_path: Path):
    repo = Path(__file__).resolve().parents[1]
    kubeconfig = tmp_path / "kubeconfig"
    runtime = tmp_path / "runtime.env"
    chart = tmp_path / "opentelemetry-demo-0.40.5.tgz"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    runtime.write_text("HARBOR_REGISTRY=registry.example\n", encoding="utf-8")
    chart.write_bytes(b"pinned chart fixture")
    runner = Runner()
    result = OtelDemoResetter(
        repo_root=repo,
        kubeconfig=kubeconfig,
        runtime_env_file=runtime,
        chart_file=chart,
        environment_gate=Gate(),
        traffic_evidence=Traffic(),
        runner=runner,
    ).reset("campaign-1234567890abcdef-codex-t1", object())

    assert result["verified"] is True
    preflight = runner.calls[0][0]
    assert "--server-dry-run" in preflight and "--execute" not in preflight
    assert runner.calls[1][0][:3] == ["helm", "uninstall", "otel-demo"]
    deploy = runner.calls[2][0]
    assert "deploy_application.py" in " ".join(deploy)
    assert "--execute" in deploy
    assert "locust_workload.py" not in " ".join(deploy)
    assert runner.calls[2][1]["OTEL_DEMO_CHART_FILE"] == str(chart)
    # The preflight is the reinstall invocation with only the mode flag swapped.
    assert ["--execute" if item == "--server-dry-run" else item for item in preflight] == deploy
    assert runner.calls[0][1]["OTEL_DEMO_CHART_FILE"] == str(chart)
    assert result["reinstall_preflight"]["passed"] is True
    assert result["reinstall_preflight"]["exit_code"] == 0
    assert "--server-dry-run" in result["reinstall_preflight"]["command"]


def test_verify_only_uses_fresh_snapshot_without_repeating_recovery_loop(tmp_path: Path):
    repo = Path(__file__).resolve().parents[1]
    kubeconfig = tmp_path / "kubeconfig"
    runtime = tmp_path / "runtime.env"
    chart = tmp_path / "opentelemetry-demo-0.40.5.tgz"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    runtime.write_text("HARBOR_REGISTRY=registry.example\n", encoding="utf-8")
    chart.write_bytes(b"pinned chart fixture")
    traffic = Traffic()
    runner = Runner()

    result = OtelDemoResetter(
        repo_root=repo,
        kubeconfig=kubeconfig,
        runtime_env_file=runtime,
        chart_file=chart,
        environment_gate=Gate(),
        traffic_evidence=traffic,
        runner=runner,
        verify_only=True,
    ).reset("campaign-1234567890abcdef-codex-t2", object())

    assert result["verified"] is True
    assert traffic.reset_calls == 0
    assert runner.calls == []


def test_fresh_environment_verification_supersedes_prior_insufficient_recovery(tmp_path: Path):
    repo = Path(__file__).resolve().parents[1]
    kubeconfig = tmp_path / "kubeconfig"
    runtime = tmp_path / "runtime.env"
    chart = tmp_path / "opentelemetry-demo-0.40.5.tgz"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    runtime.write_text("HARBOR_REGISTRY=registry.example\n", encoding="utf-8")
    chart.write_bytes(b"pinned chart fixture")

    result = OtelDemoResetter(
        repo_root=repo,
        kubeconfig=kubeconfig,
        runtime_env_file=runtime,
        chart_file=chart,
        environment_gate=Gate(),
        traffic_evidence=Traffic(),
        runner=Runner(),
        verify_only=True,
    ).reset(
        "campaign-1234567890abcdef-codex-t2b",
        object(),
        {
            "main_fault_ever_active": True,
            "fault_absent": True,
            "fault_cleanup_verified": True,
            "business_recovery_verified": False,
            "cleanup_attempted": True,
            "cleanup_verified": True,
        },
    )

    assert result["prior_trial_recovery_verified"] is False
    assert result["traffic_recovery"]["business_healthy"] is True
    assert result["verified"] is True
    assert result["reset_policy"]["allows_next_trial"] is True


def test_verify_only_retries_an_insufficient_zero_request_snapshot(tmp_path: Path):
    class InitiallyEmptyTraffic(Traffic):
        def current(self):
            return {
                "application_owned": True,
                "load_generator_ready": True,
                "traffic_observed": False,
                "business_healthy": False,
                "target_requests": 0,
                "sample_status": "insufficient",
            }

        def wait_until_healthy(self, **_kwargs):
            self.wait_calls += 1
            return {
                "application_owned": True,
                "load_generator_ready": True,
                "traffic_observed": True,
                "business_healthy": True,
                "target_requests": 12,
                "sample_status": "healthy",
            }

    repo = Path(__file__).resolve().parents[1]
    kubeconfig = tmp_path / "kubeconfig"
    runtime = tmp_path / "runtime.env"
    chart = tmp_path / "opentelemetry-demo-0.40.5.tgz"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    runtime.write_text("HARBOR_REGISTRY=registry.example\n", encoding="utf-8")
    chart.write_bytes(b"pinned chart fixture")
    traffic = InitiallyEmptyTraffic()

    result = OtelDemoResetter(
        repo_root=repo,
        kubeconfig=kubeconfig,
        runtime_env_file=runtime,
        chart_file=chart,
        environment_gate=Gate(),
        traffic_evidence=traffic,
        runner=Runner(),
        verify_only=True,
    ).reset("campaign-1234567890abcdef-codex-t3", object())

    assert result["verified"] is True
    assert result["verification_source"] == "bounded_current_traffic"
    assert traffic.wait_calls == 1


def _full_reinstall_resetter(
    tmp_path: Path, runner: Runner, traffic: Traffic | None = None
) -> OtelDemoResetter:
    repo = Path(__file__).resolve().parents[1]
    kubeconfig = tmp_path / "kubeconfig"
    runtime = tmp_path / "runtime.env"
    chart = tmp_path / "opentelemetry-demo-0.40.5.tgz"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    runtime.write_text("HARBOR_REGISTRY=registry.example\n", encoding="utf-8")
    chart.write_bytes(b"pinned chart fixture")
    return OtelDemoResetter(
        repo_root=repo,
        kubeconfig=kubeconfig,
        runtime_env_file=runtime,
        chart_file=chart,
        environment_gate=Gate(),
        traffic_evidence=traffic or Traffic(),
        runner=runner,
        timeout_seconds=120,
    )


def test_forbidden_reinstall_preflight_leaves_otel_demo_installed(tmp_path: Path):
    runner = Runner(
        preflight=subprocess.CompletedProcess([], 1, "", FORBIDDEN_PREFLIGHT_STDERR)
    )
    traffic = Traffic()

    with pytest.raises(ResetError) as caught:
        _full_reinstall_resetter(tmp_path, runner, traffic).reset(
            "campaign-1234567890abcdef-codex-t4",
            object(),
            {"reset_tier": "T3_FULL_REINSTALL"},
        )

    assert len(runner.calls) == 1
    assert "--server-dry-run" in runner.calls[0][0]
    assert not any(argv[:2] == ["helm", "uninstall"] for argv, _env, _timeout in runner.calls)
    message = str(caught.value)
    assert message.startswith(
        "reinstall preflight failed; OTel Demo was left installed: exit code 1"
    )
    assert 'cannot patch resource "namespaces"' in message
    evidence = caught.value.evidence
    assert evidence["stage"] == "reinstall_preflight"
    assert evidence["uninstall_attempted"] is False
    assert evidence["uninstalled"] is False
    preflight = evidence["reinstall_preflight"]
    assert preflight["passed"] is False
    assert preflight["exit_code"] == 1
    assert 'cannot patch resource "namespaces"' in preflight["stderr_excerpt"]
    assert "--server-dry-run" in preflight["command"]
    assert "--execute" not in preflight["command"]
    assert traffic.reset_calls == 0 and traffic.wait_calls == 0


@pytest.mark.parametrize(
    ("preflight", "reason"),
    [
        (
            subprocess.TimeoutExpired(
                ["deploy_application.py"], 300, stderr=b"helm: context deadline exceeded"
            ),
            "timed out",
        ),
        (
            subprocess.CompletedProcess([], 0, "NAME: otel-demo\nSTATUS: deployed\n", ""),
            "not a passing deploy_application.py server dry-run report",
        ),
        (
            subprocess.CompletedProcess(
                [], 0, json.dumps({"modeExecution": "dry-run", "actions": []}), ""
            ),
            "not a passing deploy_application.py server dry-run report",
        ),
        (OSError("python executable missing"), "could not run (OSError)"),
        (
            subprocess.CompletedProcess(
                [], 2, "", "Traceback: KeyError while rendering password=hunter2"
            ),
            "exit code 2",
        ),
    ],
)
def test_any_reinstall_preflight_failure_blocks_the_uninstall(
    tmp_path: Path, preflight, reason
):
    runner = Runner(preflight=preflight)

    with pytest.raises(ResetError, match=re.escape(reason)) as caught:
        _full_reinstall_resetter(tmp_path, runner).reset(
            "campaign-1234567890abcdef-codex-t5", object()
        )

    assert [argv for argv, _env, _timeout in runner.calls if argv[:2] == ["helm", "uninstall"]] == []
    evidence = caught.value.evidence
    assert evidence["uninstalled"] is False
    assert evidence["reinstall_preflight"]["passed"] is False
    assert evidence["reinstall_preflight"]["timed_out"] is (reason == "timed out")
    assert "hunter2" not in json.dumps(evidence)
    assert "hunter2" not in str(caught.value)


def test_passing_preflight_then_uninstalls_and_reinstalls_on_full_reinstall_tier(
    tmp_path: Path,
):
    runner = Runner()

    result = _full_reinstall_resetter(tmp_path, runner).reset(
        "campaign-1234567890abcdef-codex-t6",
        object(),
        {"reset_tier": "T3_FULL_REINSTALL"},
    )

    steps = [argv for argv, _env, _timeout in runner.calls]
    assert len(steps) == 3
    assert "--server-dry-run" in steps[0]
    assert steps[1][:2] == ["helm", "uninstall"]
    assert "--execute" in steps[2]
    assert runner.calls[0][2] == runner.calls[2][2] == 300
    assert result["verified"] is True
    assert result["reset_policy"]["tier"] == "T3_FULL_REINSTALL"
    assert result["reinstall_preflight"]["checks"] == [
        "helm upgrade --install --dry-run=server"
    ]
    assert result["reinstall_preflight"]["not_simulated"] == ["helm --wait readiness"]


def test_reinstall_failure_after_passing_preflight_records_the_uninstall(
    tmp_path: Path,
):
    class FailingReinstallRunner(Runner):
        def run(self, argv, *, env, timeout):
            if "--execute" in argv:
                self.calls.append((argv, env, timeout))
                return subprocess.CompletedProcess(
                    argv, 1, "", "helm: timed out waiting for the condition"
                )
            return super().run(argv, env=env, timeout=timeout)

    runner = FailingReinstallRunner()

    with pytest.raises(ResetError, match="OTel Demo reinstallation failed") as caught:
        _full_reinstall_resetter(tmp_path, runner).reset(
            "campaign-1234567890abcdef-codex-t7", object()
        )

    assert [argv[:2] for argv, _env, _timeout in runner.calls][1] == ["helm", "uninstall"]
    assert caught.value.evidence["stage"] == "reinstall"
    assert caught.value.evidence["uninstalled"] is True
    assert caught.value.evidence["reinstall_preflight"]["passed"] is True


def test_replica_reset_targets_its_own_namespace(tmp_path: Path, monkeypatch):
    """Without --namespace the reinstall would land on the full system's namespace."""
    monkeypatch.setenv("RESBENCH_APPLICATION_NAMESPACE", "otel-demo-04")
    repo = Path(__file__).resolve().parents[1]
    kubeconfig = tmp_path / "kubeconfig"
    runtime = tmp_path / "runtime.env"
    chart = tmp_path / "opentelemetry-demo-0.40.5.tgz"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    runtime.write_text("HARBOR_REGISTRY=registry.example\n", encoding="utf-8")
    chart.write_bytes(b"pinned chart fixture")
    runner = Runner()

    result = OtelDemoResetter(
        repo_root=repo,
        kubeconfig=kubeconfig,
        runtime_env_file=runtime,
        chart_file=chart,
        environment_gate=Gate(),
        traffic_evidence=Traffic(),
        runner=runner,
    ).reset("campaign-1234567890abcdef-codex-t1", object())

    assert result["verified"] is True
    preflight, uninstall, deploy = (call[0] for call in runner.calls[:3])
    assert uninstall[:5] == ["helm", "uninstall", "otel-demo", "--namespace", "otel-demo-04"]
    for argv in (preflight, deploy):
        # The replica shares the bundle of the system it copies, in its own namespace.
        assert argv[argv.index("--application") + 1] == "otel-demo"
        assert argv[argv.index("--namespace") + 1] == "otel-demo-04"
    assert ["--execute" if item == "--server-dry-run" else item for item in preflight] == deploy


def test_default_reset_still_names_the_single_system(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("RESBENCH_APPLICATION_NAMESPACE", raising=False)
    repo = Path(__file__).resolve().parents[1]
    kubeconfig = tmp_path / "kubeconfig"
    runtime = tmp_path / "runtime.env"
    chart = tmp_path / "opentelemetry-demo-0.40.5.tgz"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    runtime.write_text("HARBOR_REGISTRY=registry.example\n", encoding="utf-8")
    chart.write_bytes(b"pinned chart fixture")
    runner = Runner()

    OtelDemoResetter(
        repo_root=repo,
        kubeconfig=kubeconfig,
        runtime_env_file=runtime,
        chart_file=chart,
        environment_gate=Gate(),
        traffic_evidence=Traffic(),
        runner=runner,
    ).reset("campaign-1234567890abcdef-codex-t1", object())

    uninstall = runner.calls[1][0]
    deploy = runner.calls[2][0]
    assert uninstall[:5] == ["helm", "uninstall", "otel-demo", "--namespace", "otel-demo"]
    assert deploy[deploy.index("--namespace") + 1] == "otel-demo"
