from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import yaml

from stage2_service.bladeai_mcp_guard import (
    BladeAIMcpGuardError,
    BladeAIMcpGuardPatch,
    build_allowed_mcp_guard_tool_names,
)


def test_build_allowed_mcp_guard_tools_intersects_policy_and_connected_tools(
    monkeypatch,
    tmp_path: Path,
):
    _install_fake_safe_tool_name(monkeypatch)
    policy_path = tmp_path / "mcp-tools.yaml"
    policy_path.write_text(
        yaml.safe_dump(
            {
                "tools": {
                    "k8s_ro": {
                        "mode": "read_only",
                        "allowed_operations": ["k8s_get_resource"],
                    },
                    "harness_channel": {
                        "mode": "controlled_write",
                        "allowed_operations": ["harness_confirm"],
                    },
                    "code_sandbox": {
                        "mode": "controlled_write",
                        "allowed_operations": ["run_python"],
                    },
                    "chaos_control": {
                        "mode": "controlled_write",
                        "allowed_operations": ["chaos_create_experiment"],
                    },
                    "chaos_mesh_control": {
                        "mode": "controlled_write",
                        "allowed_operations": ["chaos_mesh_create_experiment"],
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    manager = _FakeMcpManager(
        connected_servers=[
            "k8s_ro",
            "harness_channel",
            "code_sandbox",
            "chaos_control",
            "chaos_mesh_control",
        ],
        tools_by_phase={
            "phase1": [
                "k8s_ro__k8s_get_resource",
                "harness_channel__harness_confirm",
                "chaos_control__chaos_create_experiment",
            ],
            "phase2": [
                "code_sandbox__run_python",
                "chaos_mesh_control__chaos_mesh_create_experiment",
            ],
        },
    )

    allowed = build_allowed_mcp_guard_tool_names(manager, policy_path)

    assert allowed == frozenset({
        "k8s_ro__k8s_get_resource",
        "harness_channel__harness_confirm",
        "code_sandbox__run_python",
    })


def test_bladeai_mcp_guard_patch_restores_all_import_aliases(monkeypatch):
    modules = _install_fake_guard_modules(monkeypatch)
    original = modules["classifier"].infer_effective_target
    patch = BladeAIMcpGuardPatch({"k8s_ro__k8s_get_resource"})

    patch.install()

    assert (
        modules["classifier"]
        .infer_effective_target("k8s_ro__k8s_get_resource", {})
        .scope
        == "__readonly__"
    )
    assert (
        modules["target_guard"]
        .infer_effective_target("k8s_ro__k8s_get_resource", {})
        .scope
        == "__readonly__"
    )
    assert (
        modules["phase1_screener"]
        .infer_effective_target("k8s_ro__k8s_get_resource", {})
        .scope
        == "__readonly__"
    )
    assert (
        modules["tool_screener"]
        .infer_effective_target("k8s_ro__k8s_get_resource", {})
        .scope
        == "__readonly__"
    )
    assert (
        modules["classifier"]
        .infer_effective_target("chaos_control__chaos_create_experiment", {})
        .scope
        == "__unknown__"
    )

    patch.restore()

    assert modules["classifier"].infer_effective_target is original
    assert modules["target_guard"].infer_effective_target is original
    assert modules["phase1_screener"].infer_effective_target is original
    assert modules["tool_screener"].infer_effective_target is original


def test_bladeai_mcp_guard_fails_when_sdk_symbols_are_missing(monkeypatch):
    modules = _install_fake_guard_modules(monkeypatch)
    delattr(modules["tool_screener"], "infer_effective_target")
    patch = BladeAIMcpGuardPatch({"k8s_ro__k8s_get_resource"})

    try:
        try:
            patch.install()
        except BladeAIMcpGuardError as exc:
            assert "tool_screener.infer_effective_target" in str(exc)
        else:
            raise AssertionError("expected BladeAIMcpGuardError")
    finally:
        patch.restore()


class _FakeMcpManager:
    def __init__(self, *, connected_servers, tools_by_phase):
        self._clients = [SimpleNamespace(name=name) for name in connected_servers]
        self._tools_by_phase = {
            phase: [SimpleNamespace(name=name) for name in names]
            for phase, names in tools_by_phase.items()
        }

    def tools_for_phase(self, phase: str):
        return self._tools_by_phase.get(phase, [])


def _install_fake_safe_tool_name(monkeypatch) -> None:
    _install_package(monkeypatch, "chaos_agent")
    _install_package(monkeypatch, "chaos_agent.mcp")
    adapter = ModuleType("chaos_agent.mcp.adapter")
    adapter._safe_tool_name = lambda server, tool: f"{server}__{tool}"
    monkeypatch.setitem(sys.modules, "chaos_agent.mcp.adapter", adapter)
    sys.modules["chaos_agent.mcp"].adapter = adapter


def _install_fake_guard_modules(monkeypatch) -> dict[str, ModuleType]:
    _install_package(monkeypatch, "chaos_agent")
    _install_package(monkeypatch, "chaos_agent.agent")
    _install_package(monkeypatch, "chaos_agent.agent.nodes")
    _install_package(monkeypatch, "chaos_agent.agent.target_guard")

    class EffectiveTarget:
        def __init__(
            self,
            *,
            scope: str,
            namespace: str,
            confidence: Any = None,
            raw_command: str = "",
        ) -> None:
            self.scope = scope
            self.namespace = namespace
            self.confidence = confidence
            self.raw_command = raw_command

    def original(tool_name, tool_args, *, skill_script_allowed=False):
        return EffectiveTarget(
            scope="__unknown__",
            namespace="",
            raw_command=f"original:{tool_name}:{skill_script_allowed}",
        )

    classifier = ModuleType("chaos_agent.agent.target_guard.classifier")
    classifier.SCOPE_READONLY = "__readonly__"
    classifier.infer_effective_target = original

    types = ModuleType("chaos_agent.agent.target_guard.types")
    types.ConfidenceLevel = SimpleNamespace(HIGH="high")
    types.EffectiveTarget = EffectiveTarget

    target_guard = sys.modules["chaos_agent.agent.target_guard"]
    target_guard.infer_effective_target = original

    phase1_screener = ModuleType("chaos_agent.agent.nodes.phase1_screener")
    phase1_screener.infer_effective_target = original

    tool_screener = ModuleType("chaos_agent.agent.nodes.tool_screener")
    tool_screener.infer_effective_target = original

    monkeypatch.setitem(
        sys.modules,
        "chaos_agent.agent.target_guard.classifier",
        classifier,
    )
    monkeypatch.setitem(sys.modules, "chaos_agent.agent.target_guard.types", types)
    monkeypatch.setitem(
        sys.modules,
        "chaos_agent.agent.nodes.phase1_screener",
        phase1_screener,
    )
    monkeypatch.setitem(
        sys.modules,
        "chaos_agent.agent.nodes.tool_screener",
        tool_screener,
    )
    sys.modules["chaos_agent.agent.target_guard"].classifier = classifier
    sys.modules["chaos_agent.agent.target_guard"].types = types
    sys.modules["chaos_agent.agent.nodes"].phase1_screener = phase1_screener
    sys.modules["chaos_agent.agent.nodes"].tool_screener = tool_screener
    return {
        "classifier": classifier,
        "target_guard": target_guard,
        "phase1_screener": phase1_screener,
        "tool_screener": tool_screener,
    }


def _install_package(monkeypatch, name: str) -> ModuleType:
    package = ModuleType(name)
    package.__path__ = []
    monkeypatch.setitem(sys.modules, name, package)
    if "." in name:
        parent_name, _, child_name = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None:
            setattr(parent, child_name, package)
    return package
