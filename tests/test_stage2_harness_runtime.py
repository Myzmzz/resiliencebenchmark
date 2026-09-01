from __future__ import annotations

from pathlib import Path

import pytest

from stage2_service.contracts import HarnessKind, LifecyclePhase
from stage2_service.harness_runtime import (
    HarnessRuntimeError,
    NativeHarnessRunner,
    _bladeai_fault_parts,
    _normalize_bladeai_event,
)
from stage2_service.contracts import RuntimeTarget, TrialRuntimeContext


class DummySupervisor:
    pass


def runner(tmp_path: Path):
    return NativeHarnessRunner(
        repo_root=Path(__file__).resolve().parents[1],
        private_root=tmp_path / "private",
        artifact_root=tmp_path / "artifacts",
        permissions=object(),  # normalization tests do not access permissions
        mcp_supervisor=DummySupervisor(),
        base_environment={},
    )


def test_codex_runtime_requires_isolated_codex_eval(tmp_path: Path):
    executable = tmp_path / "codex-eval"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    runtime = NativeHarnessRunner(
        repo_root=Path(__file__).resolve().parents[1],
        private_root=tmp_path / "private",
        artifact_root=tmp_path / "artifacts",
        permissions=object(),
        mcp_supervisor=DummySupervisor(),
        base_environment={"RESBENCH_CODEX_EVAL_BIN": str(executable)},
    )

    assert runtime._resolve_executable(HarnessKind.CODEX, "codex") == str(
        executable.resolve()
    )

    with pytest.raises(HarnessRuntimeError, match="global codex fallback is forbidden"):
        runner(tmp_path)._resolve_executable(HarnessKind.CODEX, "codex")


def test_normalizes_target_binding_and_main_fault_request(tmp_path: Path):
    runtime = runner(tmp_path)
    common = {
        "campaign_id": "campaign-1234567890abcdef",
        "trial_id": "campaign-1234567890abcdef-codex-t2",
        "harness": HarnessKind.CODEX,
    }
    target = runtime._normalize_tool_event(
        **common,
        item={
            "type": "mcp_tool_call",
            "server": "chaos_control",
            "tool": "chaos_validate_plan",
            "status": "completed",
            "arguments": {
                "namespace": "otel-demo",
                "target_name": "cart-old",
                "target_uid": "old-uid",
            },
            "result": {"structured_content": {"ok": True}},
        },
    )
    create = runtime._normalize_tool_event(
        **common,
        item={
            "type": "mcp_tool_call",
            "server": "chaos_control",
            "tool": "chaos_create_experiment",
            "status": "in_progress",
            "arguments": {"target_uid": "new-uid"},
        },
    )

    assert target[0].phase is LifecyclePhase.C2_TARGET
    assert target[0].kind == "target_bound"
    assert target[0].payload["target"]["uid"] == "old-uid"
    assert target[1].kind == "plan_validated"
    assert create[0].phase is LifecyclePhase.C3_INJECT
    assert create[0].payload["target_uid"] == "new-uid"


def test_main_fault_running_requires_explicit_successful_create_result(tmp_path: Path):
    runtime = runner(tmp_path)
    common = {
        "campaign_id": "campaign-1234567890abcdef",
        "trial_id": "campaign-1234567890abcdef-codex-t4",
        "harness": HarnessKind.CODEX,
    }
    rejected = runtime._normalize_tool_event(
        **common,
        item={
            "type": "mcp_tool_call",
            "server": "chaos_control",
            "tool": "chaos_create_experiment",
            "status": "completed",
            "arguments": {"target_uid": "uid-current"},
            "result": {
                "structured_content": {
                    "ok": False,
                    "error": {"code": "CONCURRENCY_BUDGET_EXCEEDED"},
                }
            },
        },
    )
    accepted = runtime._normalize_tool_event(
        **common,
        item={
            "type": "mcp_tool_call",
            "server": "chaos_control",
            "tool": "chaos_create_experiment",
            "status": "completed",
            "arguments": {"target_uid": "uid-current"},
            "result": {"structured_content": {"ok": True}},
        },
    )

    assert all(event.kind != "main_fault_running" for event in rejected)
    assert [event.kind for event in accepted] == ["main_fault_running"]


def test_normalizes_permission_denial_on_selected_tool(tmp_path: Path):
    events = runner(tmp_path)._normalize_tool_event(
        campaign_id="campaign-1234567890abcdef",
        trial_id="campaign-1234567890abcdef-codex-t3",
        harness=HarnessKind.CODEX,
        item={
            "type": "mcp_tool_call",
            "server": "k8s_ro",
            "tool": "k8s_list_resources",
            "status": "failed",
            "error": "403 Forbidden",
        },
    )

    denied = next(item for item in events if item.kind == "permission_denied")
    assert denied.payload["capability"] == "mcp.k8s.read"


def test_ignores_nested_permission_payload_without_tool_identity(tmp_path: Path):
    events = runner(tmp_path)._normalize_tool_event(
        campaign_id="campaign-1234567890abcdef",
        trial_id="campaign-1234567890abcdef-codex-t5",
        harness=HarnessKind.CODEX,
        item={"error": "401 Unauthorized"},
    )

    assert events == []


def test_bladeai_native_l4_events_map_to_c1_c6_contract():
    runtime = TrialRuntimeContext(
        trial_id="campaign-1234567890abcdef-bladeai-t1",
        episode_id="EPI-OTEL-CART-DEADLINE-001",
        target=RuntimeTarget(
            namespace="otel-demo", component="cart", name="cart", uid="uid-current"
        ),
        main_fault={"fault_type": "network-delay"},
        cleanup_handle="cleanup-" + "a" * 36,
        baseline_capability="b" * 40,
    )
    events = _normalize_bladeai_event(
        "campaign-1234567890abcdef",
        runtime.trial_id,
        {
            "type": "stage2_bladeai_event",
            "kind": "step_start",
            "payload": {"name": "auto_recover"},
        },
        runtime,
    )

    assert _bladeai_fault_parts("network-delay") == ("pod", "network", "delay")
    assert events[0].phase is LifecyclePhase.C6_RECOVERY
    assert events[0].kind == "recovery_requested"
