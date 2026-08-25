"""Native subprocess adapters normalized into the Stage-2 C1-C6 event contract."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
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
    load_json,
    load_yaml,
    render_claude_config,
    render_codex_config,
    render_dsh_contract,
    render_prompt,
    resolve_prompt_file,
    subprocess_streaming_runner,
    validate_final_agent_result,
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
from .permissions import Stage2PermissionManager
from .mcp_supervisor import McpSupervisor


class HarnessRuntimeError(RuntimeError):
    pass


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
            return self._run_bladeai(
                campaign_id=campaign_id,
                trial_id=trial_id,
                model_alias=model_alias,
                episode=episode,
                runtime_context=runtime_context,
                capability=capability,
                event_observer=event_observer,
            )
        permission_runtime = self.permissions.runtime_context(trial_id)
        mcp_environment = self.mcp_supervisor.start_trial(
            trial_id=trial_id,
            harness=harness,
            token=str(permission_runtime["mcp_token"]),
            token_state_files=permission_runtime["mcp_token_state_files"],
        )
        trial_root = Path(
            tempfile.mkdtemp(prefix=f"{trial_id}-", dir=self.private_root)
        )
        artifact_dir = self.artifact_root / campaign_id / trial_id
        artifact_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        codex_home = trial_root / "codex-home"
        claude_home = trial_root / "claude-home"
        dsh_home = trial_root / "dsh-home"
        public_data = episode.public.model_dump(mode="json")
        harnesses = load_yaml(self.repo_root / DEFAULT_HARNESSES_CONFIG)
        common = resolve_prompt_file(harnesses, "common_task", self.repo_root)
        selected = resolve_prompt_file(harnesses, "full_lifecycle", self.repo_root)
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
        prompt = append_runtime_capability_prompt(prompt, env)
        validate_prompt_text(prompt)
        render_codex_config(self.repo_root, codex_home, env)
        mcp_config = render_claude_config(self.repo_root, claude_home)
        render_dsh_contract(self.repo_root, dsh_home, env, model_alias)
        child_env = child_env_for_harness(
            harness.value,
            env,
            {
                "CODEX_HOME": str(codex_home),
                "CLAUDE_CONFIG_DIR": str(claude_home),
                "DSH_HOME": str(dsh_home),
            },
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
            harness.value, definition, model_alias, prompt, paths
        )
        if fail_closed:
            raise HarnessRuntimeError(fail_closed)
        executable = shutil.which(argv[0], path=SAFE_PATH)
        if not executable:
            raise HarnessRuntimeError(f"Harness executable is unavailable: {argv[0]}")
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
        }
        if ref:
            final_output["agent_result_ref"] = ref
            final_output["agent_result"] = json.loads(
                (artifact_dir / ref).read_text(encoding="utf-8")
            )
        shutil.rmtree(trial_root, ignore_errors=True)
        return HarnessReport(
            status=status,
            agent_verdict=verdict,
            lifecycle_events=tuple(lifecycle),
            artifact_refs=(
                f"{campaign_id}/{trial_id}/stdout.txt",
                f"{campaign_id}/{trial_id}/stderr.txt",
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
    ) -> HarnessReport:
        del model_alias, capability
        permission_runtime = self.permissions.runtime_context(trial_id)
        kubeconfig = permission_runtime.get("bladeai_kubeconfig")
        if not kubeconfig:
            raise HarnessRuntimeError("BladeAI trial has no direct scoped kubeconfig")
        lifecycle: list[LifecycleEvent] = []
        self._emit(
            lifecycle,
            event_observer,
            campaign_id,
            trial_id,
            HarnessKind.BLADEAI,
            LifecyclePhase.C1_PLAN,
            "plan_committed",
            {
                "capabilities": [
                    "metrics.k8s.io",
                    "mcp.telemetry.read",
                    "native.blade.create",
                ],
                "source": "BladeAI L4 fixed task",
            },
        )
        self._emit(
            lifecycle,
            event_observer,
            campaign_id,
            trial_id,
            HarnessKind.BLADEAI,
            LifecyclePhase.C2_TARGET,
            "target_bound",
            {"target": runtime_context.target.model_dump(mode="json")},
        )
        root = Path(tempfile.mkdtemp(prefix=f"{trial_id}-bladeai-", dir=self.private_root))
        blade_runtime = root / "chaosblade"
        bundled_blade = Path("/opt/blade-ai/vendor/chaosblade")
        if not (bundled_blade / "blade").is_file():
            raise HarnessRuntimeError("bundled ChaosBlade runtime is missing")
        shutil.copytree(bundled_blade, blade_runtime)
        (blade_runtime / "blade").chmod(0o755)
        artifact_dir = self.artifact_root / campaign_id / trial_id
        artifact_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        request_path = root / "request.json"
        fault = runtime_context.main_fault
        fault_type = str(fault.get("fault_type") or "")
        scope, target, action = _bladeai_fault_parts(fault_type)
        request = {
            "trial_id": trial_id,
            "intent": episode.public.objective,
            "target": runtime_context.target.model_dump(mode="json"),
            "fault_scope": scope,
            "fault_target": target,
            "fault_action": action,
            "params": dict(fault.get("parameters") or {}),
            "duration": int(fault.get("duration_seconds") or 600),
            "kubeconfig": str(kubeconfig),
        }
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        request_path.chmod(0o600)
        env = {
            **os.environ,
            "BLADE_AI_LLM_API_KEY": self.base_environment.get(
                "RESBENCH_LLM_API_KEY", ""
            ),
            "BLADE_AI_API_BASE_URL": self.base_environment.get(
                "RESBENCH_LLM_BASE_URL", ""
            ),
            "BLADE_AI_MODEL_NAME": self.base_environment.get(
                "STAGE2_BLADEAI_MODEL", "gpt-5.6"
            ),
            "BLADE_AI_KUBECONFIG_PATH": str(kubeconfig),
            "BLADE_AI_BLADE_PATH": str(blade_runtime / "blade"),
            "BLADE_AI_MEMORY_DIR": str(root / "memory"),
            "HOME": str(root),
            "USER": "resbench",
            "LOGNAME": "resbench",
            "PATH": SAFE_PATH,
            "PYTHONPATH": str(self.repo_root),
        }
        raw_events: list[dict[str, Any]] = []

        def observe(line: bytes) -> None:
            for item in extract_json_objects(line.decode("utf-8", errors="replace")):
                raw_events.append(item)
                for event in _normalize_bladeai_event(
                    campaign_id, trial_id, item, runtime_context
                ):
                    lifecycle.append(event)
                    event_observer(event)

        bladeai_python = self.base_environment.get(
            "STAGE2_BLADEAI_PYTHON", sys.executable
        )
        result = subprocess_streaming_runner(
            [bladeai_python, "-m", "stage2_service.bladeai_worker", str(request_path)],
            b"",
            env,
            self.timeout_seconds,
            observe,
        )
        (artifact_dir / "stdout.txt").write_bytes(result.stdout)
        (artifact_dir / "stderr.txt").write_bytes(result.stderr)
        final = next(
            (item for item in reversed(raw_events) if item.get("type") == "stage2_bladeai_result"),
            None,
        )
        status = "timeout" if result.timed_out else "completed" if result.returncode == 0 and final else "failed"
        blade_status = str((final or {}).get("status") or "failed")
        verdict = (
            AgentVerdict.PASS
            if blade_status == "passed"
            else AgentVerdict.INCONCLUSIVE
            if blade_status == "degraded"
            else AgentVerdict.FAIL
        )
        shutil.rmtree(root, ignore_errors=True)
        return HarnessReport(
            status=status,
            agent_verdict=verdict,
            lifecycle_events=tuple(lifecycle),
            artifact_refs=(
                f"{campaign_id}/{trial_id}/stdout.txt",
                f"{campaign_id}/{trial_id}/stderr.txt",
            ),
            final_output={
                **dict(final or {}),
                "process_succeeded": not result.timed_out and final is not None,
                "returncode": result.returncode,
            },
        )

    @staticmethod
    def _planned_capabilities(
        harness: HarnessKind, capability: CapabilityProfile
    ) -> list[str]:
        if harness is HarnessKind.BLADEAI and capability.direct_kubeconfig:
            return ["metrics.k8s.io", "mcp.chaos.create", "mcp.telemetry.read"]
        return ["mcp.k8s.read", "mcp.chaos.create", "mcp.telemetry.read"]

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


def _bladeai_fault_parts(fault_type: str) -> tuple[str, str, str]:
    mapping = {
        "network-delay": ("pod", "network", "delay"),
        "network-loss": ("pod", "network", "loss"),
        "cpu-load": ("pod", "cpu", "fullload"),
        "memory-load": ("pod", "mem", "load"),
        "pod-delete": ("pod", "pod", "delete"),
        "pod-fail": ("pod", "pod", "fail"),
    }
    value = mapping.get(fault_type)
    if value is None:
        raise HarnessRuntimeError(f"BladeAI fault type is unsupported: {fault_type}")
    return value


def _normalize_bladeai_event(
    campaign_id: str,
    trial_id: str,
    item: Mapping[str, Any],
    runtime_context,
) -> list[LifecycleEvent]:
    if item.get("type") != "stage2_bladeai_event":
        return []
    kind = str(item.get("kind") or "")
    payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
    name = str(payload.get("name") or payload.get("tool") or "")
    phase = None
    normalized = None
    result_payload: dict[str, Any] = {"bladeai_event": kind, **dict(payload)}
    if kind == "step_start":
        phase_map = {
            "planning": LifecyclePhase.C1_PLAN,
            "baseline_capture": LifecyclePhase.C2_TARGET,
            "fault_injection": LifecyclePhase.C3_INJECT,
            "verification": LifecyclePhase.C4_EFFECT,
            "safety_check": LifecyclePhase.C5_SAFETY,
            "auto_recover": LifecyclePhase.C6_RECOVERY,
        }
        phase = phase_map.get(name)
        normalized = (
            "recovery_requested"
            if name == "auto_recover"
            else "phase_started"
        )
    elif kind == "tool_start":
        if name in {"blade_create", "kubectl"}:
            phase = LifecyclePhase.C3_INJECT
            normalized = "main_fault_requested"
            result_payload["target_uid"] = runtime_context.target.uid
        elif name in {"blade_status", "kubectl_verify"}:
            phase = LifecyclePhase.C4_EFFECT
            normalized = "effect_check_started"
        elif name == "blade_destroy":
            phase = LifecyclePhase.C6_RECOVERY
            normalized = "recovery_requested"
    elif kind == "finish":
        phase = LifecyclePhase.C6_RECOVERY
        normalized = "business_recovery_verified"
    if phase is None or normalized is None:
        return []
    digest = hashlib.sha256(
        f"{trial_id}\x1f{kind}\x1f{name}\x1f{len(json.dumps(payload, sort_keys=True))}".encode()
    ).hexdigest()[:16]
    return [
        LifecycleEvent(
            event_id=f"{trial_id}-bladeai-{digest}",
            campaign_id=campaign_id,
            trial_id=trial_id,
            harness=HarnessKind.BLADEAI,
            phase=phase,
            kind=normalized,
            payload=result_payload,
        )
    ]
