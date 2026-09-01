from __future__ import annotations

import subprocess
from pathlib import Path

from stage2_service.reset import OtelDemoResetter


class Runner:
    def __init__(self):
        self.calls = []

    def run(self, argv, *, env, timeout):
        self.calls.append((argv, env, timeout))
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
    assert runner.calls[0][0][:3] == ["helm", "uninstall", "otel-demo"]
    deploy = runner.calls[1][0]
    assert "deploy_application.py" in " ".join(deploy)
    assert "--execute" in deploy
    assert "locust_workload.py" not in " ".join(deploy)
    assert runner.calls[1][1]["OTEL_DEMO_CHART_FILE"] == str(chart)


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
