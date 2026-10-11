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


def _stage1_environments(tmp_path: Path, **options: object) -> dict[str, dict[str, str]]:
    return build_stack_environment(
        k8s_kubeconfig=tmp_path / "k8s.kubeconfig",
        chaos_kubeconfig=tmp_path / "chaos.kubeconfig",
        source_root=tmp_path / "sources",
        runtime_root=tmp_path / "runtime",
        token="local-stack-token-with-at-least-thirty-two-chars",
        **options,
    )


def test_local_stack_defaults_keep_raw_telemetry_off_and_stage2_jaeger_allowlist(tmp_path: Path) -> None:
    telemetry_env = _stage1_environments(tmp_path)["telemetry_ro"]

    assert telemetry_env["RESBENCH_TELEMETRY_ALLOW_RAW_QUERIES"] == "false"
    assert telemetry_env["RESBENCH_JAEGER_ALLOWED_SERVICES"] == "frontend,frontend-proxy,checkout,cart,payment,shipping"


def test_local_stack_stage1_options_enable_raw_telemetry_and_custom_jaeger_allowlist(tmp_path: Path) -> None:
    telemetry_env = _stage1_environments(
        tmp_path,
        allow_raw_telemetry=True,
        jaeger_allowed_services="frontend,cart,product-catalog",
    )["telemetry_ro"]

    assert telemetry_env["RESBENCH_TELEMETRY_ALLOW_RAW_QUERIES"] == "true"
    assert telemetry_env["RESBENCH_JAEGER_ALLOWED_SERVICES"] == "frontend,cart,product-catalog"


def test_bladeai_sse_ports_are_only_bound_when_requested(tmp_path: Path) -> None:
    from scripts.local_control_stack import BLADEAI_SSE_PORTS, PORTS, LocalStack

    stack_arguments = dict(
        port_forward_kubeconfig=tmp_path / "port-forward.kubeconfig",
        controller_kubeconfig=tmp_path / "controller.kubeconfig",
        codex_auth_file=None,
        source_root=tmp_path / "sources",
        runtime_root=tmp_path / "runtime",
        environments=_stage1_environments(tmp_path),
    )

    assert LocalStack(**stack_arguments)._all_ports() == PORTS
    assert set(LocalStack(**stack_arguments, bladeai_sse=True)._all_ports()) == set(PORTS) | set(BLADEAI_SSE_PORTS.values())
