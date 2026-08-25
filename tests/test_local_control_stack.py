from __future__ import annotations

from pathlib import Path

from scripts.local_control_stack import build_stack_environment, redacted_plan


def test_local_stack_plan_keeps_chaos_disabled_and_token_redacted(tmp_path: Path) -> None:
    token = "local-stack-token-with-at-least-thirty-two-chars"
    envs = build_stack_environment(
        k8s_kubeconfig=tmp_path / "k8s.kubeconfig",
        chaos_kubeconfig=tmp_path / "chaos.kubeconfig",
        source_root=tmp_path / "sources",
        runtime_root=tmp_path / "runtime",
        token=token,
    )

    plan = redacted_plan(envs, tmp_path / "runtime")

    assert envs["chaos_control"]["RESBENCH_CHAOS_EXECUTE_ENABLED"] == "false"
    assert plan["chaos_writes_enabled"] is False
    assert token not in str(plan)
    assert {item["name"] for item in plan["mcp_servers"]} == {
        "k8s_ro",
        "telemetry_ro",
        "source_ro",
        "chaos_control",
    }
