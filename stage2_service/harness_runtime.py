"""Native subprocess adapters normalized into the Stage-2 C1-C6 event contract."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from scripts.run_harness_trial import (
    DEFAULT_HARNESSES_CONFIG,
    DEFAULT_OUTPUT_SCHEMA,
    SAFE_PATH,
    append_runtime_capability_prompt,
    build_argv,
    child_env_for_harness,
    event_tool_name,
    extract_json_objects,
    extract_provider_reported_model,
    load_json,
    load_yaml,
    render_claude_config,
    render_bladeai_config,
    render_codex_config,
    render_dsh_contract,
    render_prompt,
    redact_json,
    redact_text,
    resolve_formal_harness_command,
    resolve_prompt_file,
    subprocess_streaming_runner,
    validate_final_agent_result,
    validate_formal_harness_runtime,
    validate_prompt_text,
)

from .contracts import (
    AgentVerdict,
    CapabilityProfile,
    HarnessKind,
    HarnessReport,
    LifecycleEvent,
    LifecyclePhase,
)
from .bladeai_session import BladeAIServerProcess, BladeAISessionClient
from .permissions import Stage2PermissionManager
from .mcp_supervisor import McpSupervisor


class HarnessRuntimeError(RuntimeError):
    pass


_BLADEAI_INHERITED_ENV = (
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
)


class NativeHarnessRunner:
    def __init__(
        self,
        *,
        repo_root: Path,
        private_root: Path,
        artifact_root: Path,
        permissions: Stage2PermissionManager,
        mcp_supervisor: McpSupervisor,
        base_environment: Mapping[str, str],
        timeout_seconds: int = 1800,
    ):
        self.repo_root = repo_root.resolve()
        self.private_root = private_root.resolve()
        self.artifact_root = artifact_root.resolve()
        self.permissions = permissions
        self.mcp_supervisor = mcp_supervisor
        self.base_environment = dict(base_environment)
        self.timeout_seconds = timeout_seconds
        self.private_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.artifact_root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def run(
        self,
        *,
        campaign_id: str,
        trial_id: str,
        harness: HarnessKind,
        model_alias: str,
        episode,
        runtime_context,
        capability: CapabilityProfile,
        event_observer,
    ) -> HarnessReport:
        if harness is HarnessKind.BLADEAI:
            permission_runtime = self.permissions.runtime_context(trial_id)
            mcp_environment = self.mcp_supervisor.start_trial(
                trial_id=trial_id,
                harness=harness,
                token=str(permission_runtime["mcp_token"]),
                token_state_files=permission_runtime["mcp_token_state_files"],
                servers=capability.mcp_servers,
            )
            try:
                return self._run_bladeai(
                    campaign_id=campaign_id,
                    trial_id=trial_id,
                    model_alias=model_alias,
                    episode=episode,
                    runtime_context=runtime_context,
                    capability=capability,
                    event_observer=event_observer,
                    mcp_environment=mcp_environment,
                )
            finally:
                self.mcp_supervisor.stop()
        permission_runtime = self.permissions.runtime_context(trial_id)
        mcp_environment = self.mcp_supervisor.start_trial(
            trial_id=trial_id,
            harness=harness,
            token=str(permission_runtime["mcp_token"]),
            token_state_files=permission_runtime["mcp_token_state_files"],
            servers=capability.mcp_servers,
        )
        temporary_trial = tempfile.TemporaryDirectory(
            prefix=f"{trial_id}-", dir=self.private_root
        )
        trial_root = Path(temporary_trial.name)
        artifact_dir = self.artifact_root / campaign_id / trial_id
        artifact_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        codex_home = trial_root / "codex-home"
        claude_home = trial_root / "claude-home"
        dsh_home = trial_root / "dsh-home"
        public_data = episode.public.model_dump(mode="json")
        harnesses = load_yaml(self.repo_root / DEFAULT_HARNESSES_CONFIG)
        common = resolve_prompt_file(harnesses, "common_task", self.repo_root)
        selected = resolve_prompt_file(harnesses, "full_lifecycle", self.repo_root)
        allowed_mcp_servers = tuple(server.value for server in capability.mcp_servers)
        permission_surface_ref = self._write_permission_surface(
            artifact_dir, capability
        )
        env = {
            **self.base_environment,
            **mcp_environment,
            "RESBENCH_MCP_TOKEN": str(permission_runtime["mcp_token"]),
            "RESBENCH_BASELINE_GATE_TOKEN": runtime_context.baseline_capability,
            "RESBENCH_CLEANUP_HANDLE": runtime_context.cleanup_handle,
            "RESBENCH_AUTHORIZED_TARGET_JSON": json.dumps(
                runtime_context.target.model_dump(mode="json"),
                separators=(",", ":"),
                sort_keys=True,
            ),
            "RESBENCH_MAIN_FAULT_JSON": json.dumps(
                runtime_context.main_fault,
                separators=(",", ":"),
                sort_keys=True,
            ),
            "RESBENCH_AUTHORIZED_RUN_ID": trial_id,
        }
        prompt = render_prompt(common, selected, public_data)
        if "chaos_control" in allowed_mcp_servers:
            prompt = append_runtime_capability_prompt(prompt, env)
        validate_prompt_text(prompt)
        render_codex_config(
            self.repo_root, codex_home, env, allowed_mcp_servers
        )
        mcp_config = render_claude_config(
            self.repo_root, claude_home, allowed_mcp_servers
        )
        render_dsh_contract(
            self.repo_root,
            dsh_home,
            env,
            model_alias,
            allowed_mcp_servers,
        )
        homes = {
            "CODEX_HOME": str(codex_home),
            "CLAUDE_CONFIG_DIR": str(claude_home),
            "DSH_HOME": str(dsh_home),
        }
        child_env = child_env_for_harness(
            harness.value, env, homes, allowed_mcp_servers
        )
        paths = {
            "output_schema_file": self.repo_root / DEFAULT_OUTPUT_SCHEMA,
            "codex_last_message_file": trial_root / "codex-last-message.json",
            "mcp_config_file": mcp_config,
        }
        registry = harnesses.get("harnesses", {})
        definition = registry.get(harness.value)
        if not isinstance(definition, Mapping):
            raise HarnessRuntimeError(f"Harness is not registered: {harness.value}")
        argv, stdin, fail_closed = build_argv(
            harness.value,
            definition,
            model_alias,
            prompt,
            paths,
            allowed_mcp_servers,
        )
        if fail_closed:
            raise HarnessRuntimeError(fail_closed)
        try:
            validate_formal_harness_runtime(
                harness.value,
                argv,
                child_env,
                homes,
                paths,
                trial_root,
                str(permission_runtime["mcp_token"]),
                allowed_mcp_servers,
            )
            executable = resolve_formal_harness_command(harness.value, argv[0])
        except ValueError as exc:
            self.mcp_supervisor.stop()
            temporary_trial.cleanup()
            return HarnessReport(
                status="failed",
                agent_verdict=AgentVerdict.CASE_INVALID,
                lifecycle_events=(),
                artifact_refs=(
                    f"{campaign_id}/{trial_id}/{permission_surface_ref}",
                ),
                final_output={
                    "process_succeeded": False,
                    "platform_preflight_failed": True,
                    "permission_profile_id": capability.permission_profile_id,
                    "reason": str(exc),
                },
            )
        argv = [executable, *argv[1:]]
        lifecycle: list[LifecycleEvent] = []
        self._emit(
            lifecycle,
            event_observer,
            campaign_id,
            trial_id,
            harness,
            LifecyclePhase.C1_PLAN,
            "plan_committed",
            {
                "capabilities": self._planned_capabilities(harness, capability),
                "source": "fixed_episode_and_capability_profile",
            },
        )

        target_binding_seen = False

        def observe_line(line: bytes) -> None:
            nonlocal target_binding_seen
            for item in extract_json_objects(line.decode("utf-8", errors="replace")):
                for event in self._normalize_tool_event(
                    campaign_id, trial_id, harness, item
                ):
                    if event.kind == "target_bound":
                        if target_binding_seen:
                            target = event.payload.get("target") or {}
                            event = event.model_copy(
                                update={
                                    "kind": "target_reconfirmed",
                                    "payload": {
                                        **event.payload,
                                        "uid": target.get("uid"),
                                    },
                                }
                            )
                        else:
                            target_binding_seen = True
                    lifecycle.append(event)
                    event_observer(event)

        try:
            result = subprocess_streaming_runner(
                argv,
                stdin,
                child_env,
                self.timeout_seconds,
                observe_line,
            )
        finally:
            self.mcp_supervisor.stop()
        (artifact_dir / "stdout.txt").write_bytes(result.stdout)
        (artifact_dir / "stderr.txt").write_bytes(result.stderr)
        output_schema = load_json(self.repo_root / DEFAULT_OUTPUT_SCHEMA)
        ref, validation_error = validate_final_agent_result(
            result.stdout,
            [paths["codex_last_message_file"]],
            artifact_dir,
            output_schema,
            env,
        )
        status = (
            "timeout"
            if result.timed_out
            else "failed"
            if result.returncode != 0 or validation_error
            else "completed"
        )
        verdict = (
            AgentVerdict.PASS
            if status == "completed"
            else AgentVerdict.INCONCLUSIVE
            if validation_error
            else AgentVerdict.FAIL
        )
        final_output: dict[str, Any] = {
            "returncode": result.returncode,
            "validation_error": validation_error,
            "process_succeeded": result.returncode == 0 and not result.timed_out,
            "permission_profile_id": capability.permission_profile_id,
            "mcp_servers": list(allowed_mcp_servers),
        }
        if ref:
            final_output["agent_result_ref"] = ref
            final_output["agent_result"] = json.loads(
                (artifact_dir / ref).read_text(encoding="utf-8")
            )
        temporary_trial.cleanup()
        return HarnessReport(
            status=status,
            agent_verdict=verdict,
            lifecycle_events=tuple(lifecycle),
            provider_reported_model=extract_provider_reported_model(
                result.stdout.decode("utf-8", errors="replace")
            ),
            artifact_refs=(
                f"{campaign_id}/{trial_id}/stdout.txt",
                f"{campaign_id}/{trial_id}/stderr.txt",
                f"{campaign_id}/{trial_id}/{permission_surface_ref}",
                *((f"{campaign_id}/{trial_id}/{ref}",) if ref else ()),
            ),
            final_output=final_output,
        )

    def _run_bladeai(
        self,
        *,
        campaign_id,
        trial_id,
        model_alias,
        episode,
        runtime_context,
        capability,
        event_observer,
        mcp_environment,
    ) -> HarnessReport:
        permission_runtime = self.permissions.runtime_context(trial_id)
        kubeconfig = permission_runtime.get("bladeai_kubeconfig")
        if not kubeconfig:
            raise HarnessRuntimeError("BladeAI trial has no direct scoped kubeconfig")
        temporary_root = tempfile.TemporaryDirectory(
            prefix=f"{trial_id}-bladeai-", dir=self.private_root
        )
        root = Path(temporary_root.name)
        artifact_dir = self.artifact_root / campaign_id / trial_id
        artifact_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        permission_surface_ref = self._write_permission_surface(
            artifact_dir, capability
        )
        lifecycle: list[LifecycleEvent] = []
        events_path = artifact_dir / "bladeai-events.jsonl"
        final_path = artifact_dir / "stdout.txt"
        server_log = artifact_dir / "bladeai-server.log"
        bladeai_python = Path(
            self.base_environment.get("STAGE2_BLADEAI_PYTHON", "")
        )
        if not bladeai_python.is_file():
            temporary_root.cleanup()
            raise HarnessRuntimeError(
                "environment-provided BladeAI Python runtime is unavailable"
            )

        supplied_blade = Path(
            self.base_environment.get("STAGE2_BLADEAI_CHAOSBLADE_ROOT", "")
        )
        blade_path = ""
        if supplied_blade.is_dir() and (supplied_blade / "blade").is_file():
            blade_runtime = root / "chaosblade"
            shutil.copytree(supplied_blade, blade_runtime)
            (blade_runtime / "blade").chmod(0o755)
            blade_path = str(blade_runtime / "blade")
        elif capability.native_permissions.chaosblade_execute:
            temporary_root.cleanup()
            raise HarnessRuntimeError(
                "BladeAI native chaos permission is enabled but its runtime is unavailable"
            )

        allowed_mcp_servers = tuple(server.value for server in capability.mcp_servers)
        mcp_config = render_bladeai_config(
            self.repo_root, root, allowed_mcp_servers
        )
        public_data = episode.public.model_dump(mode="json")
        harnesses = load_yaml(self.repo_root / DEFAULT_HARNESSES_CONFIG)
        common = resolve_prompt_file(harnesses, "common_task", self.repo_root)
        selected = resolve_prompt_file(harnesses, "full_lifecycle", self.repo_root)
        prompt_env = {
            **self.base_environment,
            **mcp_environment,
            "RESBENCH_MCP_TOKEN": str(permission_runtime["mcp_token"]),
            "RESBENCH_BASELINE_GATE_TOKEN": runtime_context.baseline_capability,
            "RESBENCH_CLEANUP_HANDLE": runtime_context.cleanup_handle,
            "RESBENCH_AUTHORIZED_TARGET_JSON": json.dumps(
                runtime_context.target.model_dump(mode="json"),
                separators=(",", ":"),
                sort_keys=True,
            ),
            "RESBENCH_MAIN_FAULT_JSON": json.dumps(
                runtime_context.main_fault,
                separators=(",", ":"),
                sort_keys=True,
            ),
            "RESBENCH_AUTHORIZED_RUN_ID": trial_id,
        }
        prompt = render_prompt(common, selected, public_data)
        if "chaos_control" in allowed_mcp_servers:
            prompt = append_runtime_capability_prompt(prompt, prompt_env)
        validate_prompt_text(prompt)
        env = {
            **{
                key: os.environ[key]
                for key in _BLADEAI_INHERITED_ENV
                if os.environ.get(key)
            },
            **mcp_environment,
            "BLADE_AI_LLM_API_KEY": self.base_environment.get(
                "RESBENCH_LLM_API_KEY", ""
            ),
            "BLADE_AI_API_BASE_URL": self.base_environment.get(
                "RESBENCH_LLM_BASE_URL", ""
            ),
            "BLADE_AI_MODEL_NAME": model_alias,
            "BLADE_AI_KUBECONFIG_PATH": str(kubeconfig),
            "BLADE_AI_MEMORY_DIR": str(root / "memory"),
            "BLADE_AI_CONFIRMATION_REQUIRED": "false",
            "BLADE_AI_POSTMORTEM_ENABLED": "false",
            "BLADE_AI_MCP_ENABLED": "true" if allowed_mcp_servers else "false",
            "BLADE_AI_MCP_CONFIG_PATH": str(mcp_config),
            "HOME": str(root),
            "USER": "resbench",
            "LOGNAME": "resbench",
            "PATH": SAFE_PATH,
        }
        if allowed_mcp_servers:
            env["RESBENCH_MCP_TOKEN"] = str(permission_runtime["mcp_token"])
        if blade_path:
            env["BLADE_AI_BLADE_PATH"] = blade_path

        def observe(item: dict[str, Any]) -> None:
            safe_item = redact_json(item, prompt_env)
            with events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(safe_item, ensure_ascii=False) + "\n")
            for event in self._normalize_bladeai_session_event(
                campaign_id, trial_id, safe_item, runtime_context
            ):
                lifecycle.append(event)
                event_observer(event)

        try:
            with BladeAIServerProcess(
                python=bladeai_python,
                environment=env,
                log_path=server_log,
            ) as server:
                session = BladeAISessionClient(
                    server.base_url, timeout_seconds=self.timeout_seconds
                ).run(
                    prompt=prompt,
                    namespace="otel-demo",
                    model_name=model_alias,
                    event_observer=observe,
                )
            final_path.write_text(session.assistant_text, encoding="utf-8")
            final_path.chmod(0o600)
            events_path.touch(exist_ok=True)
            events_path.chmod(0o600)
            output_schema = load_json(self.repo_root / DEFAULT_OUTPUT_SCHEMA)
            ref, validation_error = validate_final_agent_result(
                session.assistant_text.encode("utf-8"),
                [],
                artifact_dir,
                output_schema,
                prompt_env,
            )
            status = (
                "completed"
                if session.completed and not validation_error
                else "failed"
            )
            verdict = (
                AgentVerdict.PASS
                if status == "completed"
                else AgentVerdict.INCONCLUSIVE
                if validation_error
                else AgentVerdict.FAIL
            )
            final_output: dict[str, Any] = {
                "process_succeeded": session.completed,
                "session_id": session.session_id,
                "validation_error": validation_error,
                "permission_profile_id": capability.permission_profile_id,
                "mcp_servers": list(allowed_mcp_servers),
                "native_permissions": capability.native_permissions.model_dump(
                    mode="json"
                ),
            }
            if session.error:
                final_output["error"] = session.error
            if ref:
                final_output["agent_result_ref"] = ref
                final_output["agent_result"] = json.loads(
                    (artifact_dir / ref).read_text(encoding="utf-8")
                )
            return HarnessReport(
                status=status,
                agent_verdict=verdict,
                lifecycle_events=tuple(lifecycle),
                artifact_refs=(
                    f"{campaign_id}/{trial_id}/stdout.txt",
                    f"{campaign_id}/{trial_id}/bladeai-events.jsonl",
                    f"{campaign_id}/{trial_id}/bladeai-server.log",
                    f"{campaign_id}/{trial_id}/{permission_surface_ref}",
                    *((f"{campaign_id}/{trial_id}/{ref}",) if ref else ()),
                ),
                final_output=final_output,
            )
        finally:
            if server_log.is_file():
                server_log.write_text(
                    redact_text(
                        server_log.read_text(encoding="utf-8", errors="replace"),
                        prompt_env,
                    ),
                    encoding="utf-8",
                )
                server_log.chmod(0o600)
            temporary_root.cleanup()

    @staticmethod
    def _write_permission_surface(
        artifact_dir: Path, capability: CapabilityProfile
    ) -> str:
        name = "permission-surface.json"
        path = artifact_dir / name
        path.write_text(
            json.dumps(
                {
                    "schema_version": "stage2-permission-surface.v1",
                    "permission_profile_id": capability.permission_profile_id,
                    "harness": capability.harness.value,
                    "mcp_servers": [server.value for server in capability.mcp_servers],
                    "mcp_tools": list(capability.mcp_tools),
                    "native_permissions": capability.native_permissions.model_dump(
                        mode="json"
                    ),
                    "kubernetes_rules": [
                        rule.model_dump(mode="json")
                        for rule in capability.kubernetes_rules
                    ],
                    "direct_kubeconfig": capability.direct_kubeconfig,
                    "allowed_fault_types": list(capability.allowed_fault_types),
                    "expires_at": capability.expires_at.isoformat(),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        return name

    def _normalize_bladeai_session_event(
        self,
        campaign_id: str,
        trial_id: str,
        item: Mapping[str, Any],
        runtime_context,
    ) -> list[LifecycleEvent]:
        event_type = str(item.get("type") or "")
        node = str(item.get("node") or "")
        phase_by_node = {
            "intent_clarification": LifecyclePhase.C1_PLAN,
            "plan_builder": LifecyclePhase.C1_PLAN,
            "agent_loop": LifecyclePhase.C1_PLAN,
            "baseline_capture": LifecyclePhase.C2_TARGET,
            "safety_check": LifecyclePhase.C5_SAFETY,
            "confirmation_gate": LifecyclePhase.C5_SAFETY,
            "intent_confirm": LifecyclePhase.C5_SAFETY,
            "execute_loop": LifecyclePhase.C3_INJECT,
            "direct_execute": LifecyclePhase.C3_INJECT,
            "verifier_loop": LifecyclePhase.C4_EFFECT,
            "finalize_verification": LifecyclePhase.C4_EFFECT,
            "execute_destroy": LifecyclePhase.C6_RECOVERY,
            "recover_verifier_loop": LifecyclePhase.C6_RECOVERY,
            "finalize_recover_verification": LifecyclePhase.C6_RECOVERY,
        }
        if event_type == "node_start" and node in phase_by_node:
            return [
                self._event(
                    campaign_id,
                    trial_id,
                    HarnessKind.BLADEAI,
                    phase_by_node[node],
                    "phase_started",
                    {"node": node, "source": "bladeai_session_sse"},
                )
            ]
        if event_type == "tool_start":
            tool = str(item.get("tool_name") or item.get("tool") or "")
            arguments = _tool_arguments(item)
            normalized = self._normalize_tool_event(
                campaign_id,
                trial_id,
                HarnessKind.BLADEAI,
                {
                    "type": "tool_call",
                    "tool": tool,
                    "arguments": arguments,
                    "status": "started",
                },
            )
            if tool in {"blade_create", "kubectl"}:
                normalized.append(
                    self._event(
                        campaign_id,
                        trial_id,
                        HarnessKind.BLADEAI,
                        LifecyclePhase.C3_INJECT,
                        "main_fault_requested",
                        {
                            "tool": tool,
                            "target_uid": runtime_context.target.uid,
                            "arguments": arguments,
                        },
                    )
                )
            elif tool == "blade_destroy":
                normalized.append(
                    self._event(
                        campaign_id,
                        trial_id,
                        HarnessKind.BLADEAI,
                        LifecyclePhase.C6_RECOVERY,
                        "recovery_requested",
                        {"tool": tool},
                    )
                )
            elif not normalized:
                normalized.append(
                    self._event(
                        campaign_id,
                        trial_id,
                        HarnessKind.BLADEAI,
                        phase_by_node.get(node, _phase_for_tool(tool)),
                        "tool_called",
                        {"tool": tool, "arguments": arguments},
                    )
                )
            return normalized
        if event_type == "tool_end" and _permission_denied(item):
            tool = str(item.get("tool_name") or item.get("tool") or "")
            return [
                self._event(
                    campaign_id,
                    trial_id,
                    HarnessKind.BLADEAI,
                    phase_by_node.get(node, _phase_for_tool(tool)),
                    "permission_denied",
                    {"capability": _capability_for_tool(tool), "tool": tool},
                )
            ]
        if event_type == "harness_approval":
            return [
                self._event(
                    campaign_id,
                    trial_id,
                    HarnessKind.BLADEAI,
                    LifecyclePhase.C5_SAFETY,
                    "harness_approval_answered",
                    {
                        "node": item.get("node"),
                        "answer": item.get("answer"),
                        "delivered": item.get("delivered"),
                    },
                )
            ]
        if event_type == "done":
            return [
                self._event(
                    campaign_id,
                    trial_id,
                    HarnessKind.BLADEAI,
                    LifecyclePhase.C6_RECOVERY,
                    "agent_finished",
                    {"source": "bladeai_session_sse"},
                )
            ]
        return []

    @staticmethod
    def _planned_capabilities(
        harness: HarnessKind, capability: CapabilityProfile
    ) -> list[str]:
        by_server = {
            "k8s_ro": "mcp.k8s.read",
            "telemetry_ro": "mcp.telemetry.read",
            "source_ro": "mcp.source.read",
            "chaos_control": "mcp.chaos.create",
        }
        planned = [by_server[server.value] for server in capability.mcp_servers]
        if harness is HarnessKind.BLADEAI:
            native = capability.native_permissions
            if native.kubernetes_read:
                planned.append("k8s.read")
            if native.kubernetes_metrics:
                planned.append("metrics.k8s.io")
            if native.chaosblade_execute:
                planned.append("native.blade.create")
        return planned

    def _normalize_tool_event(
        self,
        campaign_id: str,
        trial_id: str,
        harness: HarnessKind,
        item: Mapping[str, Any],
    ) -> list[LifecycleEvent]:
        tool = event_tool_name(item) or ""
        arguments = _tool_arguments(item)
        status = str(item.get("status") or "").lower()
        output: list[LifecycleEvent] = []
        if tool.endswith("chaos_validate_plan"):
            target = {
                "namespace": arguments.get("namespace"),
                "name": arguments.get("target_name"),
                "uid": arguments.get("target_uid"),
            }
            if all(target.values()):
                output.append(
                    self._event(
                        campaign_id,
                        trial_id,
                        harness,
                        LifecyclePhase.C2_TARGET,
                        "target_bound",
                        {"target": target, "tool": tool},
                    )
                )
        elif tool.endswith("chaos_create_experiment"):
            output.append(
                self._event(
                    campaign_id,
                    trial_id,
                    harness,
                    LifecyclePhase.C3_INJECT,
                    "main_fault_requested",
                    {
                        "target_uid": arguments.get("target_uid"),
                        "tool": tool,
                        "status": status,
                    },
                )
            )
        elif tool.endswith(("telemetry_prom_metric_range", "chaos_get_experiment")):
            output.append(
                self._event(
                    campaign_id,
                    trial_id,
                    harness,
                    LifecyclePhase.C4_EFFECT,
                    "effect_check_started",
                    {"tool": tool},
                )
            )
        elif tool.endswith("chaos_destroy_experiment"):
            output.extend(
                (
                    self._event(
                        campaign_id,
                        trial_id,
                        harness,
                        LifecyclePhase.C5_SAFETY,
                        "safe_stop",
                        {"tool": tool},
                    ),
                    self._event(
                        campaign_id,
                        trial_id,
                        harness,
                        LifecyclePhase.C6_RECOVERY,
                        "recovery_requested",
                        {"tool": tool, "status": status},
                    ),
                )
            )
        if status in {"failed", "error"} and _permission_denied(item):
            output.append(
                self._event(
                    campaign_id,
                    trial_id,
                    harness,
                    _phase_for_tool(tool),
                    "permission_denied",
                    {"capability": _capability_for_tool(tool), "tool": tool},
                )
            )
        return output

    @staticmethod
    def _emit(events, observer, campaign_id, trial_id, harness, phase, kind, payload):
        event = NativeHarnessRunner._event(
            campaign_id, trial_id, harness, phase, kind, payload
        )
        events.append(event)
        observer(event)

    @staticmethod
    def _event(campaign_id, trial_id, harness, phase, kind, payload):
        digest = hashlib.sha256(
            f"{trial_id}\x1f{phase.value}\x1f{kind}\x1f{json.dumps(payload, sort_keys=True)}".encode()
        ).hexdigest()[:16]
        return LifecycleEvent(
            event_id=f"{trial_id}-{phase.value.lower()}-{digest}",
            campaign_id=campaign_id,
            trial_id=trial_id,
            harness=harness,
            phase=phase,
            kind=kind,
            payload=dict(payload),
        )


def _tool_arguments(item: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("arguments", "args", "input"):
        value = item.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    nested = item.get("item")
    if isinstance(nested, Mapping):
        return _tool_arguments(nested)
    return {}


def _permission_denied(item: Mapping[str, Any]) -> bool:
    text = json.dumps(item, ensure_ascii=False).lower()
    return any(marker in text for marker in ("forbidden", "permission denied", "unauthorized", "401", "403"))


def _capability_for_tool(tool: str) -> str:
    if tool.endswith("chaos_create_experiment"):
        return "mcp.chaos.create"
    if "telemetry" in tool:
        return "mcp.telemetry.read"
    if "k8s" in tool:
        return "mcp.k8s.read"
    return f"mcp.{tool}"


def _phase_for_tool(tool: str) -> LifecyclePhase:
    if "chaos_create" in tool:
        return LifecyclePhase.C3_INJECT
    if "chaos_destroy" in tool:
        return LifecyclePhase.C6_RECOVERY
    if "telemetry" in tool or "chaos_get" in tool:
        return LifecyclePhase.C4_EFFECT
    return LifecyclePhase.C2_TARGET
