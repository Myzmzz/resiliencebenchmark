from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import yaml

from stage2_service import harness_runtime
from stage2_service.bladeai_session import BladeAISessionResult
from stage2_service.contracts import (
    AgentVerdict,
    BladeAINativePermissions,
    CapabilityProfile,
    HarnessKind,
    LifecyclePhase,
    McpServerName,
)
from stage2_service.harness_runtime import NativeHarnessRunner
from stage2_service.contracts import RuntimeTarget, TrialRuntimeContext


class DummySupervisor:
    pass


class PreflightSupervisor:
    def __init__(self):
        self.stopped = False

    def start_trial(self, **_kwargs):
        return {
            "RESBENCH_K8S_MCP_URL": "http://127.0.0.1:18081/mcp",
            "RESBENCH_TELEMETRY_MCP_URL": "http://127.0.0.1:18082/mcp",
            "RESBENCH_SOURCE_MCP_URL": "http://127.0.0.1:18083/mcp",
            "RESBENCH_CHAOS_CONTROL_MCP_URL": "http://127.0.0.1:18084/mcp",
        }

    def stop(self):
        self.stopped = True


class CapturingSupervisor(PreflightSupervisor):
    def __init__(self):
        super().__init__()
        self.started = None

    def start_trial(self, **kwargs):
        self.started = kwargs
        return {
            "RESBENCH_BLADEAI_K8S_MCP_SSE_URL": "http://127.0.0.1:18181/sse",
            "RESBENCH_BLADEAI_SOURCE_MCP_SSE_URL": "http://127.0.0.1:18183/sse",
        }


class PreflightPermissions:
    def runtime_context(self, _trial_id):
        return {
            "mcp_token": "trial-token-" + "x" * 40,
            "mcp_token_state_files": {
                "k8s_ro": "/private/k8s.token",
                "source_ro": "/private/source.token",
            },
            "bladeai_kubeconfig": "/private/bladeai.kubeconfig",
        }


def runner(tmp_path: Path):
    return NativeHarnessRunner(
        repo_root=Path(__file__).resolve().parents[1],
        private_root=tmp_path / "private",
        artifact_root=tmp_path / "artifacts",
        permissions=object(),  # normalization tests do not access permissions
        mcp_supervisor=DummySupervisor(),
        base_environment={},
    )


def capability(harness: HarnessKind) -> CapabilityProfile:
    return CapabilityProfile(
        permission_profile_id="p0-full-authorized",
        harness=harness,
        mcp_servers=(
            McpServerName.K8S_RO,
            McpServerName.TELEMETRY_RO,
            McpServerName.SOURCE_RO,
            *((McpServerName.CHAOS_CONTROL,) if harness is not HarnessKind.BLADEAI else ()),
        ),
        mcp_tools=("k8s_list_resources", "telemetry_jaeger_find_traces"),
        kubernetes_rules=(),
        direct_kubeconfig=harness is HarnessKind.BLADEAI,
        native_permissions=(
            BladeAINativePermissions()
            if harness is HarnessKind.BLADEAI
            else BladeAINativePermissions(
                kubernetes_read=False,
                kubernetes_metrics=False,
                chaosblade_execute=False,
            )
        ),
        allowed_fault_types=("network-delay",),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


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
            "arguments": {
                "namespace": "otel-demo",
                "target_name": "cart-old",
                "target_uid": "old-uid",
            },
        },
    )
    create = runtime._normalize_tool_event(
        **common,
        item={
            "type": "mcp_tool_call",
            "server": "chaos_control",
            "tool": "chaos_create_experiment",
            "arguments": {"target_uid": "new-uid"},
        },
    )

    assert target[0].phase is LifecyclePhase.C2_TARGET
    assert target[0].kind == "target_bound"
    assert target[0].payload["target"]["uid"] == "old-uid"
    assert create[0].phase is LifecyclePhase.C3_INJECT
    assert create[0].payload["target_uid"] == "new-uid"


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


def test_formal_preflight_failure_is_case_invalid_and_cleans_runtime(
    tmp_path: Path, monkeypatch
):
    supervisor = PreflightSupervisor()
    runtime = NativeHarnessRunner(
        repo_root=Path(__file__).resolve().parents[1],
        private_root=tmp_path / "private",
        artifact_root=tmp_path / "artifacts",
        permissions=PreflightPermissions(),
        mcp_supervisor=supervisor,
        base_environment={
            "RESBENCH_LLM_BASE_URL": "https://gateway.example/v1",
            "RESBENCH_LLM_API_KEY": "test-key",
            "RESBENCH_CHAOS_CONTROLLER_TOKEN_REF": "runtime://controller/token",
            "RESBENCH_CHAOS_CONTROLLER_POD_UID": "controller-pod-uid",
        },
    )
    public_data = yaml.safe_load(
        (
            Path(__file__).resolve().parents[1]
            / "tasks/episodes/otel-demo/EPI-OTEL-CART-DEADLINE-001/episode-public.yaml"
        ).read_text(encoding="utf-8")
    )
    episode = SimpleNamespace(
        public=SimpleNamespace(model_dump=lambda **_kwargs: public_data)
    )
    context = TrialRuntimeContext(
        trial_id="campaign-1234567890abcdef-codex-t1",
        episode_id="EPI-OTEL-CART-DEADLINE-001",
        target=RuntimeTarget(
            namespace="otel-demo", component="cart", name="cart", uid="uid-current"
        ),
        main_fault={"fault_type": "network-delay"},
        cleanup_handle="cleanup-" + "a" * 36,
        baseline_capability="b" * 40,
    )
    monkeypatch.setattr(
        harness_runtime,
        "resolve_formal_harness_command",
        lambda *_args: (_ for _ in ()).throw(ValueError("polluted formal launcher")),
    )

    report = runtime.run(
        campaign_id="campaign-1234567890abcdef",
        trial_id=context.trial_id,
        harness=HarnessKind.CODEX,
        model_alias="gpt-5.6",
        episode=episode,
        runtime_context=context,
        capability=capability(HarnessKind.CODEX),
        event_observer=lambda _event: None,
    )

    assert report.status == "failed"
    assert report.agent_verdict is AgentVerdict.CASE_INVALID
    assert report.final_output["platform_preflight_failed"] is True
    assert report.final_output["process_succeeded"] is False
    assert supervisor.stopped is True
    assert not any((tmp_path / "private").iterdir())


def test_bladeai_session_events_map_to_native_lifecycle_contract(tmp_path: Path):
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
    events = runner(tmp_path)._normalize_bladeai_session_event(
        "campaign-1234567890abcdef",
        runtime.trial_id,
        {
            "type": "tool_start",
            "node": "execute_loop",
            "tool_name": "blade_create",
            "input": {"namespace": "otel-demo", "names": ["cart"]},
        },
        runtime,
    )

    assert events[0].phase is LifecyclePhase.C3_INJECT
    assert events[0].kind == "main_fault_requested"
    assert events[0].payload["target_uid"] == "uid-current"


def test_bladeai_uses_the_shared_trial_permission_and_mcp_preparation(
    tmp_path: Path, monkeypatch
):
    supervisor = CapturingSupervisor()
    runtime = NativeHarnessRunner(
        repo_root=Path(__file__).resolve().parents[1],
        private_root=tmp_path / "private",
        artifact_root=tmp_path / "artifacts",
        permissions=PreflightPermissions(),
        mcp_supervisor=supervisor,
        base_environment={},
    )
    selected = capability(HarnessKind.BLADEAI).model_copy(
        update={
            "mcp_servers": (
                McpServerName.K8S_RO,
                McpServerName.SOURCE_RO,
            )
        }
    )
    captured = {}

    def fake_bladeai(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(status="completed")

    monkeypatch.setattr(runtime, "_run_bladeai", fake_bladeai)
    context = TrialRuntimeContext(
        trial_id="campaign-1234567890abcdef-bladeai-static",
        episode_id="EPI-OTEL-CART-DEADLINE-001",
        target=RuntimeTarget(
            namespace="otel-demo", component="cart", name="cart", uid="uid-current"
        ),
        main_fault={"fault_type": "network-delay"},
        cleanup_handle="cleanup-" + "a" * 36,
        baseline_capability="b" * 40,
    )

    report = runtime.run(
        campaign_id="campaign-1234567890abcdef",
        trial_id=context.trial_id,
        harness=HarnessKind.BLADEAI,
        model_alias="gpt-5.6-sol",
        episode=SimpleNamespace(),
        runtime_context=context,
        capability=selected,
        event_observer=lambda _event: None,
    )

    assert report.status == "completed"
    assert supervisor.started["servers"] == selected.mcp_servers
    assert supervisor.started["harness"] is HarnessKind.BLADEAI
    assert captured["mcp_environment"]["RESBENCH_BLADEAI_K8S_MCP_SSE_URL"].endswith("/sse")
    assert supervisor.stopped is True


def test_permission_surface_artifact_records_effective_static_profile(tmp_path: Path):
    selected = capability(HarnessKind.BLADEAI).model_copy(
        update={
            "permission_profile_id": "p1-no-telemetry-mcp",
            "mcp_servers": (McpServerName.K8S_RO, McpServerName.SOURCE_RO),
        }
    )

    name = NativeHarnessRunner._write_permission_surface(tmp_path, selected)
    path = tmp_path / name
    document = json.loads(path.read_text(encoding="utf-8"))

    assert document["permission_profile_id"] == "p1-no-telemetry-mcp"
    assert document["mcp_servers"] == ["k8s_ro", "source_ro"]
    assert document["native_permissions"]["kubernetes_metrics"] is True
    assert path.stat().st_mode & 0o777 == 0o600


def test_bladeai_static_profile_uses_private_home_and_does_not_inherit_host_permissions(
    tmp_path: Path, monkeypatch
):
    python = tmp_path / "bladeai-python"
    python.write_text("runtime", encoding="utf-8")
    blade_root = tmp_path / "blade-runtime"
    blade_root.mkdir()
    (blade_root / "blade").write_text("runtime", encoding="utf-8")
    captured = {}

    class Server:
        def __init__(self, *, python, environment, log_path):
            captured["environment"] = dict(environment)
            captured["mcp"] = json.loads(
                (Path(environment["HOME"]) / ".blade-ai/mcp.json").read_text(
                    encoding="utf-8"
                )
            )
            captured["home"] = Path(environment["HOME"])
            self.base_url = "http://127.0.0.1:19091"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class Client:
        def __init__(self, base_url, *, timeout_seconds):
            assert base_url == "http://127.0.0.1:19091"

        def run(self, **kwargs):
            kwargs["event_observer"](
                {
                    "type": "tool_start",
                    "node": "intent_clarification",
                    "tool_name": "k8s_ro__k8s_list_resources",
                    "input": {"namespace": "otel-demo", "resource": "pods"},
                }
            )
            final = {
                "status": "completed",
                "suspected_defect": "static permission profile qualification",
                "evidence": [
                    {
                        "source": "k8s_ro",
                        "summary": "profile-selected tool returned current data",
                        "observed_at": "2026-09-05T00:00:00Z",
                        "artifact_ref": "k8s://otel-demo/pods",
                    }
                ],
                "actions_taken": ["queried the selected MCP server"],
                "recovery_check": "no mutation was requested",
                "remaining_risk": "manual integration remains pending",
            }
            return BladeAISessionResult(
                session_id="sess-static",
                completed=True,
                assistant_text=json.dumps(final),
                events=(),
                state={},
            )

    permissions = PreflightPermissions()
    runtime = NativeHarnessRunner(
        repo_root=Path(__file__).resolve().parents[1],
        private_root=tmp_path / "private",
        artifact_root=tmp_path / "artifacts",
        permissions=permissions,
        mcp_supervisor=DummySupervisor(),
        base_environment={
            "STAGE2_BLADEAI_PYTHON": str(python),
            "STAGE2_BLADEAI_CHAOSBLADE_ROOT": str(blade_root),
            "RESBENCH_LLM_BASE_URL": "https://gateway.example/v1",
            "RESBENCH_LLM_API_KEY": "test-key",
        },
    )
    monkeypatch.setattr(harness_runtime, "BladeAIServerProcess", Server)
    monkeypatch.setattr(harness_runtime, "BladeAISessionClient", Client)
    monkeypatch.setenv("KUBECONFIG", "/root/.kube/global")
    monkeypatch.setenv("BLADE_AI_MCP_ENABLED", "global-value")
    selected = capability(HarnessKind.BLADEAI).model_copy(
        update={
            "permission_profile_id": "p1-no-telemetry-mcp",
            "mcp_servers": (McpServerName.K8S_RO, McpServerName.SOURCE_RO),
        }
    )
    public_data = yaml.safe_load(
        (
            Path(__file__).resolve().parents[1]
            / "tasks/episodes/otel-demo/EPI-OTEL-CART-DEADLINE-001/episode-public.yaml"
        ).read_text(encoding="utf-8")
    )
    context = TrialRuntimeContext(
        trial_id="campaign-1234567890abcdef-bladeai-private",
        episode_id="EPI-OTEL-CART-DEADLINE-001",
        target=RuntimeTarget(
            namespace="otel-demo", component="cart", name="cart", uid="uid-current"
        ),
        main_fault={"fault_type": "network-delay"},
        cleanup_handle="cleanup-" + "a" * 36,
        baseline_capability="b" * 40,
    )

    report = runtime._run_bladeai(
        campaign_id="campaign-1234567890abcdef",
        trial_id=context.trial_id,
        model_alias="gpt-5.6-sol",
        episode=SimpleNamespace(
            public=SimpleNamespace(model_dump=lambda **_kwargs: public_data)
        ),
        runtime_context=context,
        capability=selected,
        event_observer=lambda _event: None,
        mcp_environment={
            "RESBENCH_BLADEAI_K8S_MCP_SSE_URL": "http://127.0.0.1:18181/sse",
            "RESBENCH_BLADEAI_SOURCE_MCP_SSE_URL": "http://127.0.0.1:18183/sse",
        },
    )

    assert report.status == "completed"
    assert set(captured["mcp"]["mcpServers"]) == {"k8s_ro", "source_ro"}
    assert captured["environment"]["BLADE_AI_MCP_ENABLED"] == "true"
    assert captured["environment"]["RESBENCH_MCP_TOKEN"].startswith("trial-token-")
    assert captured["environment"].get("KUBECONFIG") is None
    assert captured["environment"]["BLADE_AI_MCP_ENABLED"] != "global-value"
    assert not captured["home"].exists()
    surface = json.loads(
        (
            tmp_path
            / "artifacts/campaign-1234567890abcdef"
            / context.trial_id
            / "permission-surface.json"
        ).read_text(encoding="utf-8")
    )
    assert surface["mcp_servers"] == ["k8s_ro", "source_ro"]
