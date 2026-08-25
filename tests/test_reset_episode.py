from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import reset_episode as reset


def args(**overrides):
    values = {
        "application": "otel-demo",
        "namespace": "otel-demo",
        "cleanup_handle": "cleanup-episode-reset-001",
        "run_id": "episode-reset-check",
        "kubeconfig": None,
        "execute": False,
        "timeout": 900,
        "mcp_timeout": 30,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_otel_baseline_gate_uses_frozen_throughput_floor():
    gate = reset.baseline_gate("otel-demo")
    assert gate["minimumThroughputRps"] == pytest.approx(7.240010246537277)
    assert gate["maximumP95LatencyMs"] == 1000
    assert gate["minimumSuccessRate"] == 0.95


def test_uncalibrated_apps_use_success_and_p95_without_absolute_throughput():
    for application in ("train-ticket", "sock-shop"):
        gate = reset.baseline_gate(application)
        assert gate["minimumThroughputRps"] is None
        assert gate["minimumSuccessRate"] == 0.95
        assert gate["maximumP95LatencyMs"] > 0


def test_plan_contains_only_requested_residual_checks():
    report = reset.plan("otel-demo", "otel-demo", "cleanup-episode-reset-001")
    assert report["residualChecks"] == [
        "ledger-owned ChaosBlade CR absent",
        "workload metrics inside baseline gate",
    ]
    assert report["excludedChecks"] == ["tc qdisc", "iptables", "database snapshot", "Nacos registry"]


def test_dry_run_never_requires_credentials_or_calls_tools():
    report = reset.run_reset(args(), env={})
    assert report["mode"] == "dry-run"
    assert report["phase"] == "plan"


def test_workload_commands_reuse_existing_generators():
    env = {
        reset.TRAIN_IMAGE_ENV: "harbor.example/workload:v1@sha256:" + "a" * 64,
        reset.LOCUST_IMAGE_ENV: "harbor.example/locust:v1@sha256:" + "b" * 64,
    }
    train = reset.workload_command("train-ticket", "reset-check", Path("/tmp/kubeconfig"), env)
    otel = reset.workload_command("otel-demo", "reset-check", Path("/tmp/kubeconfig"), env)
    assert "train_ticket_workload.py" in " ".join(train)
    assert "locust_workload.py" in " ".join(otel)
    assert "--duration-seconds" in train and "60" in train
    assert "--duration-seconds" in otel and "60" in otel


def test_workload_command_requires_digest_image_source_env():
    with pytest.raises(reset.ResetError, match=reset.LOCUST_IMAGE_ENV):
        reset.workload_command("otel-demo", "reset-check", Path("/tmp/kubeconfig"), {})


def test_cleanup_fault_calls_destroy_then_status_and_requires_absence():
    calls = []

    async def caller(url, token, tool, arguments, timeout):
        calls.append((tool, arguments))
        if tool == "chaos_destroy_experiment":
            return {"ok": True, "destroyed": "rb-chaos-001", "verified_absent": True}
        return {"ok": True, "state": "destroyed"}

    result = asyncio.run(
        reset.cleanup_fault(
            "http://127.0.0.1:18084/mcp",
            "t" * 40,
            "cleanup-episode-reset-001",
            timeout=5,
            caller=caller,
        )
    )
    assert result["verifiedAbsent"] is True
    assert [item[0] for item in calls] == ["chaos_destroy_experiment", "chaos_recovery_status"]


def test_cleanup_fault_rejects_unverified_recovery():
    async def caller(url, token, tool, arguments, timeout):
        return {"ok": True, "state": "active"}

    with pytest.raises(reset.ResetError, match="did not verify absence"):
        asyncio.run(
            reset.cleanup_fault(
                "http://127.0.0.1:18084/mcp",
                "t" * 40,
                "cleanup-episode-reset-001",
                timeout=5,
                caller=caller,
            )
        )


@pytest.mark.parametrize(
    "url",
    ["https://example.test/mcp", "http://0.0.0.0:18084/mcp", "http://127.0.0.1:18084/unsafe"],
)
def test_chaos_endpoint_is_loopback_only(url):
    with pytest.raises(reset.ResetError):
        reset.validate_chaos_endpoint(url)


def test_cleanup_workload_extracts_summary_and_removes_job_objects():
    class FakeRunner:
        def __init__(self):
            self.calls = []

        def run(self, argv, *, stdin=None, timeout=300):
            self.calls.append(argv)
            if "logs" in argv:
                return reset.CommandResult(
                    0,
                    '{"requests":480,"failures":0,"successRate":1.0,"errorRate":0.0,"p95LatencyMs":40,"throughputRps":8.0,"minimumThroughputRps":7.24,"qualified":true}',
                    "",
                )
            return reset.CommandResult(0, "", "")

    fake = FakeRunner()
    env = {reset.LOCUST_IMAGE_ENV: "harbor.example/locust:v1@sha256:" + "b" * 64}
    result = reset.wait_cleanup_workload(
        fake,
        "otel-demo",
        "reset-check",
        Path("/tmp/kubeconfig"),
        env,
        120,
    )
    assert result["summary"]["qualified"] is True
    assert result["objectsRemoved"] is True
    assert any("apply" in call and "-f" in call for call in fake.calls)
    assert any("delete" in call and any(item.startswith("pod/rb-summary-") for item in call) for call in fake.calls)
    assert any("delete" in call and "job,configmap" in call for call in fake.calls)
