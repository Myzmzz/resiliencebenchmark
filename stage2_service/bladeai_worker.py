"""Isolated BladeAI L4 subprocess entrypoint used by the Stage-2 service."""

from __future__ import annotations

import json
import os
import sys
import asyncio
import ast
from contextlib import contextmanager
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .bladeai_mcp_guard import (
    BladeAIMcpGuardError,
    BladeAIMcpGuardPatch,
    build_allowed_mcp_guard_tool_names,
)
from .bladeai_task import (
    NativeProposalCapture,
    BladeTaskError,
    BladeTaskRequest,
    HarnessConfirmationClient,
    McpHarnessConfirmationClient,
    McpTargetUIDResolver,
    TargetUIDResolver,
    partial_plan_from_native_proposal,
    confirmation_granted,
)


def emit(kind: str, payload: dict) -> None:
    if kind in {"runtime_tool_start", "tool_start", "runtime_tool_end", "tool_end"}:
        _record_wp8_discovery(payload, listing_namespaces=_WP8_LABEL_LISTING_NAMESPACES)
    print(
        json.dumps(
            {"type": "stage2_bladeai_event", "kind": kind, "payload": payload},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )


CHANNEL_ONLY_MAX_TURNS = 12
_CHANNEL_ONLY_MUTATION_OPERATIONS = frozenset(
    {
        "chaos_create_experiment",
        "chaos_destroy_experiment",
        "chaos_mesh_create_experiment",
        "chaos_mesh_destroy_experiment",
    }
)


def _channel_only_enabled() -> bool:
    return os.environ.get("RESBENCH_BLADEAI_CHANNEL_ONLY", "").strip().lower() == "true"


def _wp8_enabled() -> bool:
    return os.environ.get("RESBENCH_BLADEAI_WP8", "").strip().lower() == "true"


WP8_SKILL_CASE_PATH = (
    "references/catalogue/Pod_网络延迟/"
    "Pod_网络延迟_WP8_受控网络延迟.md"
)
WP8_SKILL_CASE_CONTENT = """# WP8 受控网络延迟资格用例

**用例名称**

WP8 固定 1ms 网络延迟资格验证

**故障现象**

对单个、已由 Agent 绑定 UID 的 Pod 施加受控 network-delay。

**注入验证**

先通过 SDK confirmation gate 获得 Controller 批准，再由 BladeAI 内部
执行图调用受控 shim 完成 validate-then-create。不得使用 selector、shell、
kubectl、Chaos Mesh 或其他未列出的执行器。

**恢复验证**

使用同一 cleanup handle 销毁并确认实验资源不存在，随后提交完整终态 JSON。

blade create k8s pod-network delay --time 1 --timeout 30
"""

_WP8_DISCOVERED_TARGETS: dict[tuple[str, str], dict[str, str]] = {}
_WP8_LABEL_LISTING_NAMESPACES: set[str] = set()


def _input_mapping(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = payload.get("input") or payload.get("arguments") or payload.get("args")
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            try:
                parsed = ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _targets_from_wp8_discovery(
    payload: Mapping[str, Any],
    *,
    listing_namespaces: set[str] | None = None,
) -> dict[tuple[str, str], dict[str, str]]:
    """Extract only eligible WP8 targets from an MCP call or result."""
    discovered: dict[tuple[str, str], dict[str, str]] = {}
    if not _wp8_enabled():
        return discovered
    tool = str(payload.get("tool") or "")
    if not tool.endswith(("k8s_get_resource", "k8s_list_resources")):
        return discovered
    arguments = _input_mapping(payload)
    namespace_arg = str(arguments.get("namespace") or "").strip()
    resource_arg = str(arguments.get("resource") or "").strip().lower()
    if resource_arg == "pods" and "resiliencebenchmark.io/qualification=bladeai-wp8" in str(
        arguments.get("label_selector") or ""
    ):
        if namespace_arg:
            (listing_namespaces if listing_namespaces is not None else _WP8_LABEL_LISTING_NAMESPACES).add(namespace_arg)
    # The full get-resource response can be truncated by the SDK event bridge.
    # A name from a get call is admissible only after the Agent itself listed
    # the WP8 qualification label in the same namespace.
    if tool.endswith("k8s_get_resource") and resource_arg == "pods":
        name_arg = str(arguments.get("name") or "").strip()
        known_namespaces = listing_namespaces if listing_namespaces is not None else _WP8_LABEL_LISTING_NAMESPACES
        if name_arg and namespace_arg in known_namespaces:
            discovered[(namespace_arg, name_arg)] = {
                "namespace": namespace_arg,
                "name": name_arg,
                "uid": "",
            }
    raw = payload.get("result")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return discovered
    if not isinstance(raw, Mapping):
        return discovered
    objects: list[Any] = []
    obj = raw.get("object")
    if isinstance(obj, Mapping):
        objects.append(obj)
    items = raw.get("items")
    if isinstance(items, list):
        objects.extend(items)
    for item in objects:
        if not isinstance(item, Mapping):
            continue
        metadata = item.get("metadata")
        if not isinstance(metadata, Mapping):
            continue
        labels = metadata.get("labels")
        if not isinstance(labels, Mapping) or labels.get(
            "resiliencebenchmark.io/qualification"
        ) != "bladeai-wp8":
            continue
        namespace = str(metadata.get("namespace") or raw.get("namespace") or "").strip()
        name = str(metadata.get("name") or "").strip()
        uid = str(metadata.get("uid") or "").strip()
        if namespace and name:
            discovered[(namespace, name)] = {
                "namespace": namespace,
                "name": name,
                "uid": uid,
            }
    return discovered


def _record_wp8_discovery(
    payload: Mapping[str, Any],
    *,
    target_store=None,
    listing_namespaces: set[str] | None = None,
) -> None:
    """Retain target names/UIDs returned by Agent read-only MCP calls."""
    discovered = _targets_from_wp8_discovery(payload, listing_namespaces=listing_namespaces)
    _WP8_DISCOVERED_TARGETS.update(discovered)
    if target_store is not None:
        target_store.update(discovered)


def _augment_wp8_proposal_target(
    proposal: Mapping[str, Any],
    *,
    discovered_targets: Mapping[tuple[str, str], Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """Bind one uniquely observed target when SDK proposal names are empty."""
    value = dict(proposal)
    target = value.get("target")
    if not isinstance(target, Mapping):
        return value
    names = target.get("names")
    if isinstance(names, list) and len(names) == 1 and str(names[0]).strip():
        return value
    namespace = str(target.get("namespace") or "").strip()
    source = discovered_targets if discovered_targets is not None else _WP8_DISCOVERED_TARGETS
    candidates = [item for (ns, _name), item in source.items()
                  if not namespace or ns == namespace]
    if len(candidates) != 1:
        return value
    bound = dict(target)
    bound["namespace"] = candidates[0]["namespace"]
    bound["names"] = [candidates[0]["name"]]
    value["target"] = bound
    return value


def _apply_wp8_skill_guard(factory_module: Any, registry: Any) -> None:
    """Keep the WP8 planning surface focused on the connected MCP tools.

    The upstream BladeAI skill tool advertises a mandatory full-catalogue
    activation.  That is useful for ordinary user tasks, but it adds a large
    response and an avoidable model turn to the fixed WP8 qualification.  The
    marker is set only by the WP8 launch adapter; normal L4 tasks retain the
    upstream tool and behaviour unchanged.
    """
    if not _wp8_enabled():
        return
    activate = getattr(registry, "activate", None)
    if callable(activate):
        def disabled_activate(_skill_name: str) -> str:
            return (
                "WP8 qualification: built-in skill activation is disabled. "
                "Use the connected read-only MCP tools and the fixed qualification contract."
            )

        # The registry is process-local to this isolated worker.  Replacing
        # this bound method prevents a fallback skill call from returning the
        # full catalogue even if a model ignores the tool description.
        registry.activate = disabled_activate

    original_builder = getattr(factory_module, "_build_skill_tools", None)
    if not callable(original_builder) or getattr(original_builder, "_resbench_wp8_guard", False):
        return

    def build_tools(value: Any):
        tools = original_builder(value)
        for tool in tools:
            if getattr(tool, "name", None) == "activate_skill":
                tool.description = (
                    "WP8 qualification only: do not call this built-in skill tool. "
                    "Use the connected MCP tools directly."
                )
        return tools

    build_tools._resbench_wp8_guard = True
    factory_module._build_skill_tools = build_tools


@contextmanager
def _wp8_confirmation_state(l4_module: Any, task: Any):
    """Force the fixed WP8 task through BladeAI's real confirmation gate.

    BladeAI 0.6.2's L4 adapter hard-codes ``needs_confirmation=False`` when
    converting every task.  WP8 must verify the SDK ``require_approval`` hook,
    so only the explicitly tagged qualification task overrides that single
    state field.  The isolated worker restores the upstream function on exit.
    """
    payload = getattr(task, "payload", None)
    if not isinstance(payload, dict) or payload.get("qualification_type") != (
        "BLADEAI_WP8_FULL_CHAIN_QUALIFICATION"
    ):
        yield
        return
    original = l4_module.test_task_to_initial_state

    def build_state(value: Any) -> dict[str, Any]:
        state = original(value)
        state["needs_confirmation"] = True
        # The WP8 contract fixes one audited qualification case. Preloading
        # this small case satisfies the upstream catalogue gate without
        # exposing the full skill catalogue or asking the model to activate a
        # large built-in skill response.
        if _wp8_enabled():
            state["skill_name"] = "k8s-chaos-skills"
            state["matched_use_case_path"] = WP8_SKILL_CASE_PATH
            state["skill_case_content"] = WP8_SKILL_CASE_CONTENT
        return state

    l4_module.test_task_to_initial_state = build_state
    try:
        yield
    finally:
        l4_module.test_task_to_initial_state = original


def _mcp_operation_name(tool_name: str) -> str:
    """Convert the BladeAI MCP adapter name to ``server.operation`` form."""
    value = str(tool_name or "").strip()
    if "__" in value and "." not in value:
        server, operation = value.split("__", 1)
        return f"{server}.{operation}"
    return value


def _channel_only_tool_map(manager: Any) -> dict[str, Any]:
    """Return only configured MCP tools; built-in BladeAI tools are excluded."""
    tools: dict[str, Any] = {}
    for phase in ("clarification", "phase1", "verifier"):
        for tool in manager.tools_for_phase(phase):
            name = str(getattr(tool, "name", "") or "")
            if name:
                tools[name] = tool
    return tools


def _tool_call_fields(tool_call: Any, index: int) -> tuple[str, str, dict[str, Any]]:
    if isinstance(tool_call, dict):
        name = str(tool_call.get("name") or "")
        call_id = str(tool_call.get("id") or f"channel-tool-{index}")
        arguments = tool_call.get("args", {})
    else:
        name = str(getattr(tool_call, "name", "") or "")
        call_id = str(getattr(tool_call, "id", "") or f"channel-tool-{index}")
        arguments = getattr(tool_call, "args", {})
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            parsed = {}
        arguments = parsed
    return name, call_id, dict(arguments) if isinstance(arguments, dict) else {}


def _text_content(value: Any) -> str:
    content = getattr(value, "content", value)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text") or "") if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content)


async def _run_channel_only(
    pool: Any,
    runtime: Runtime,
    task: Any,
    *,
    llm_factory: Any = None,
):
    """Run a bounded BladeAI MCP-channel probe without the L4 injection graph.

    This path is intentionally selected only by BASE channel qualification.
    It binds the actual connected MCP tools to the configured BladeAI model,
    executes at most ``CHANNEL_ONLY_MAX_TURNS`` model turns, and treats any
    native mutation tool request as an immediate qualification violation.
    The full task/WP8 path continues to use the normal inject/recover graphs.
    """
    from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
    from chaos_agent.l4.schemas import L4AgentError, L4TaskResult

    tool_map = _channel_only_tool_map(pool._mcp_manager)
    if not tool_map:
        raise BladeTaskError("BladeAI channel qualification has no connected MCP tools")
    if llm_factory is None:
        from chaos_agent.agent.factory import make_llm

        llm_factory = make_llm
    llm = llm_factory()
    bound_llm = llm.bind_tools(list(tool_map.values()))
    messages = [
        SystemMessage(
            content=(
                "You are running a no-fault MCP channel qualification. "
                "Use only the MCP tools provided in this conversation. "
                "Do not attempt fault injection, native BladeAI tools, shell, "
                "kubectl, file changes, or any other tool. Follow the user "
                "sequence and submit the requested qualification result."
            )
        ),
        HumanMessage(content=task.intent),
    ]
    calls = 0
    for turn in range(1, CHANNEL_ONLY_MAX_TURNS + 1):
        response = await bound_llm.ainvoke(messages)
        messages.append(response)
        content = _text_content(response)
        if content:
            emit("llm_thought", {"message": content[:500], "content": content[:3000], "turn": turn})
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            summary = content[:2000] or "BladeAI ended the channel probe without submitting a result"
            emit("conclusion", {"status": "failed", "level": "error", "message": summary})
            return L4TaskResult(
                task_id=task.task_id,
                status="failed",
                summary=summary,
                error=L4AgentError(
                    code="CHANNEL_QUALIFICATION_RESULT_MISSING",
                    message="BladeAI did not submit the required MCP qualification result",
                ),
                extras={"channel_only": True, "turns": turn, "tool_calls": calls},
            )
        for index, raw_call in enumerate(tool_calls, start=1):
            name, call_id, arguments = _tool_call_fields(raw_call, f"{turn}-{index}")
            operation = _mcp_operation_name(name)
            if operation.rsplit(".", 1)[-1] in _CHANNEL_ONLY_MUTATION_OPERATIONS:
                reason = "mutation tool is forbidden in BASE channel qualification"
                emit("fatal", {"error": reason, "tool": name, "integration_status": "qualification_violation"})
                emit("conclusion", {"status": "failed", "level": "error", "message": reason, "tool": name})
                return L4TaskResult(
                    task_id=task.task_id,
                    status="failed",
                    summary=reason,
                    error=L4AgentError(
                        code="CHANNEL_QUALIFICATION_MUTATION_ATTEMPT",
                        message=reason,
                        details={"tool": name},
                    ),
                    extras={"channel_only": True, "turns": turn, "tool_calls": calls},
                )
            tool = tool_map.get(name)
            if tool is None:
                reason = f"unbound MCP tool requested during channel qualification: {name or '<empty>'}"
                emit("fatal", {"error": reason, "integration_status": "qualification_violation"})
                emit("conclusion", {"status": "failed", "level": "error", "message": reason})
                return L4TaskResult(
                    task_id=task.task_id,
                    status="failed",
                    summary=reason,
                    error=L4AgentError(code="CHANNEL_QUALIFICATION_TOOL_NOT_BOUND", message=reason),
                    extras={"channel_only": True, "turns": turn, "tool_calls": calls},
                )
            calls += 1
            emit("runtime_tool_start", {"call_id": call_id, "tool": name, "params": arguments, "input": arguments})
            try:
                output = await tool.ainvoke(arguments)
                output_text = _text_content(output)
                status = "completed"
            except Exception as exc:  # noqa: BLE001 - return a bounded tool error to the model.
                output_text = f"[tool error] {type(exc).__name__}: {exc}"
                status = "failed"
            emit(
                "runtime_tool_end" if status == "completed" else "runtime_tool_error",
                {"call_id": call_id, "tool": name, "result": output_text[:10000], "status": status},
            )
            messages.append(ToolMessage(content=output_text, tool_call_id=call_id, name=name))
            submit_accepted = False
            if operation == "harness_channel.harness_submit_result" and status == "completed":
                try:
                    submit_payload = json.loads(output_text)
                except (TypeError, json.JSONDecodeError):
                    submit_payload = {}
                submit_accepted = (
                    isinstance(submit_payload, dict)
                    and submit_payload.get("ok") is True
                    and submit_payload.get("valid") is True
                )
            if submit_accepted:
                summary = "BladeAI submitted the BASE MCP channel qualification result"
                emit("conclusion", {"status": "passed", "level": "ok", "message": summary})
                return L4TaskResult(
                    task_id=task.task_id,
                    status="passed",
                    summary=summary,
                    extras={"channel_only": True, "turns": turn, "tool_calls": calls},
                )
    reason = f"BladeAI channel qualification exceeded the bounded {CHANNEL_ONLY_MAX_TURNS}-turn probe budget"
    emit("conclusion", {"status": "failed", "level": "error", "message": reason})
    return L4TaskResult(
        task_id=task.task_id,
        status="failed",
        summary=reason,
        error=L4AgentError(code="CHANNEL_QUALIFICATION_TURN_LIMIT", message=reason),
        extras={"channel_only": True, "turns": CHANNEL_ONLY_MAX_TURNS, "tool_calls": calls},
    )


class Step:
    def __init__(self, name: str, attrs: dict):
        self.name = name
        self.attrs = dict(attrs)


class _RuntimeTool:
    """Forward BladeAI's SDK-side tool audit calls into the event stream."""

    def execute(self, name: str, params: dict | None = None, **kwargs: Any) -> dict[str, Any]:
        payload = {"tool": name, "params": dict(params or {}), "kwargs": kwargs}
        emit("runtime_tool_execute", payload)
        return {"status": "recorded", "payload": payload}


class Runtime:
    def __init__(
        self,
        confirmation_client: HarnessConfirmationClient | None = None,
        *,
        proposal_capture: NativeProposalCapture | None = None,
        target_uid_resolver: TargetUIDResolver | None = None,
    ):
        self.trajectory = SimpleNamespace(
            thought_trace=[], state_transitions=[], agent_specific={}
        )
        self.tool = _RuntimeTool()
        self.confirmation_client = confirmation_client
        self.proposal_capture = proposal_capture
        self.target_uid_resolver = target_uid_resolver
        self._approval_sequence = 0
        self._wp8_discovered_targets: dict[tuple[str, str], dict[str, str]] = {}
        self._wp8_listing_namespaces: set[str] = set()

    @contextmanager
    def step(self, name: str, attrs: dict | None = None):
        step = Step(name, attrs or {})
        emit("step_start", {"name": name, "attrs": step.attrs})
        try:
            yield step
        finally:
            emit("step_end", {"name": name, "attrs": step.attrs})

    def emit_event(self, kind: str, payload: dict):
        # Do not reduce native events to a local lifecycle vocabulary here.
        # ``BladeAIHarnessAdapter`` is the single normalizer for all harnesses.
        if kind in {"runtime_tool_start", "tool_start", "runtime_tool_end", "tool_end"}:
            _record_wp8_discovery(
                payload,
                target_store=self._wp8_discovered_targets,
                listing_namespaces=self._wp8_listing_namespaces,
            )
        emit(kind, dict(payload))

    def require_approval(self, risk_level: str) -> bool:
        if self.confirmation_client is None:
            emit("approval", {"risk_level": risk_level, "decision": "rejected", "reason": "harness_channel_unavailable"})
            return False
        sdk_confirmation_id = self._next_sdk_confirmation_id()
        try:
            if self.proposal_capture is None or self.target_uid_resolver is None:
                raise BladeTaskError("BladeAI proposal capture or controlled target discovery is unavailable")
            proposal = self.proposal_capture.take()
            if _wp8_enabled():
                proposal = _augment_wp8_proposal_target(
                    proposal, discovered_targets=self._wp8_discovered_targets
                )
            emit(
                "sdk_confirmation_proposed",
                {
                    "sdk_confirmation_id": sdk_confirmation_id,
                    "risk_level": risk_level,
                    "proposal_fields": sorted(str(key) for key in proposal),
                    "wp8_discovered_target_count": (
                        len(self._wp8_discovered_targets) if _wp8_enabled() else None
                    ),
                    "wp8_bound_target_name": (
                        ((proposal.get("target") or {}).get("names") or [None])[0]
                        if _wp8_enabled() and isinstance(proposal.get("target"), Mapping)
                        else None
                    ),
                },
            )
            plan = partial_plan_from_native_proposal(
                proposal,
                target_uid_resolver=self.target_uid_resolver,
            )
            response = self.confirmation_client.confirm(plan)
            granted = confirmation_granted(response)
            confirm_call_id = response.get("controller_call_id")
            emit(
                "approval",
                {
                    "sdk_confirmation_id": sdk_confirmation_id,
                    "risk_level": risk_level,
                    "decision": "approved" if granted else "rejected",
                    "harness_response": dict(response),
                    "confirm_call_id": confirm_call_id if isinstance(confirm_call_id, str) and confirm_call_id else None,
                    "plan_fields": sorted(plan),
                    "assisted": response.get("assisted"),
                    "affected_nodes": response.get("affected_nodes"),
                },
            )
            return granted
        except BladeTaskError as exc:
            emit("approval", {"sdk_confirmation_id": sdk_confirmation_id, "risk_level": risk_level, "decision": "rejected", "reason": str(exc)})
            return False
        except Exception as exc:
            emit(
                "approval",
                {
                    "sdk_confirmation_id": sdk_confirmation_id,
                    "risk_level": risk_level,
                    "decision": "rejected",
                    "reason": f"harness_confirmation_error:{type(exc).__name__}",
                },
            )
            return False

    def _next_sdk_confirmation_id(self) -> str:
        self._approval_sequence += 1
        return f"bladeai-sdk-confirm-{self._approval_sequence}"

    def finish(self, status: str):
        emit("finish", {"status": status})

    def heal(self, *_args, **_kwargs):
        return None


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) != 1:
        print("usage: python -m stage2_service.bladeai_worker <request.json>", file=sys.stderr)
        return 2
    try:
        raw_request = json.loads(Path(values[0]).read_text(encoding="utf-8"))
        if not isinstance(raw_request, dict):
            raise BladeTaskError("request must be a JSON object")
        request = BladeTaskRequest.from_mapping(raw_request)
    except (OSError, json.JSONDecodeError, BladeTaskError) as exc:
        emit("fatal", {"error": f"invalid BladeAI request: {exc}", "integration_status": "incomplete"})
        return 2
    try:
        from chaos_agent.l4.agent import L4ResilienceAgent
        from chaos_agent.l4.schemas import L4TestTask
    except ImportError as exc:
        emit("fatal", {"error": f"BladeAI import failed: {type(exc).__name__}"})
        return 2
    try:
        _install_worker_sdk_runtime(L4ResilienceAgent)
        _assert_controlled_blade_shim()
        confirmation_client = (
            McpHarnessConfirmationClient.from_env()
            if request.mode == "task"
            else None
        )
        target_uid_resolver = McpTargetUIDResolver.from_env() if request.mode == "task" else None
    except BladeTaskError as exc:
        emit("fatal", {"error": str(exc), "integration_status": "incomplete"})
        return 2
    task = L4TestTask(
        task_id=request.trial_id,
        intent=request.intent,
        target=request.l4_target(),
        test_type="resilience-fault-injection",
        payload=request.l4_payload(),
    )
    emit("task_started", {"mode": request.mode, "target": task.target, "namespace": request.namespace})
    if _wp8_enabled():
        _WP8_DISCOVERED_TARGETS.clear()
        _WP8_LABEL_LISTING_NAMESPACES.clear()
    proposal_capture = NativeProposalCapture() if request.mode == "task" else None
    runtime = Runtime(
        confirmation_client,
        proposal_capture=proposal_capture,
        target_uid_resolver=target_uid_resolver,
    )
    agent = L4ResilienceAgent()
    try:
        result = _run_agent_lifecycle(agent, runtime, task)
    except BladeTaskError as exc:
        emit("fatal", {"error": str(exc), "integration_status": "incomplete"})
        return 2
    except KeyboardInterrupt:
        emit("fatal", {"error": "BladeAI SDK interrupted: KeyboardInterrupt", "integration_status": "incomplete"})
        return 130
    except Exception as exc:
        emit("fatal", {"error": f"BladeAI SDK failed: {type(exc).__name__}", "integration_status": "incomplete"})
        return 2
    print(
        json.dumps(
            {
                "type": "stage2_bladeai_result",
                "status": result.status,
                "task_id": result.task_id,
                "trajectory_id": result.trajectory_id,
                "summary": result.summary,
                "error": (
                    None
                    if result.error is None
                    else {
                        "code": result.error.code,
                        "message": result.error.message,
                        "recoverable": result.error.recoverable,
                    }
                ),
                "extras": result.extras,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )
    # A failed/refused SDK task is a measured Agent outcome, not a failure to
    # run the benchmark adapter. Fatal setup/import/uncaught errors still exit
    # nonzero above; preserve the SDK status/error in the emitted result.
    return 0


def _run_agent_lifecycle(agent, runtime: Runtime, task):
    """Run the SDK lifecycle and always invoke cleanup after startup.

    The fixed upstream L4 adapter treats most graph errors as ``L4TaskResult``
    values, but process-level integration defects can still escape from
    prepare/execute.  Cleanup is part of the worker's safety boundary, so it is
    attempted exactly once on every prepared task path before the worker emits
    its terminal result or fatal status.
    """

    result = None
    primary_error: BaseException | None = None
    cleanup_error: BaseException | None = None
    try:
        with _capture_native_confirmation_proposal(runtime):
            agent.prepare(runtime, task)
            result = agent.execute(runtime, task)
    except BaseException as exc:
        primary_error = exc
    try:
        agent.cleanup(runtime, task)
    except BaseException as exc:
        cleanup_error = exc

    if primary_error is not None:
        if cleanup_error is not None:
            emit(
                "cleanup_error",
                {
                    "error": f"BladeAI SDK cleanup failed: {type(cleanup_error).__name__}",
                    "after_error": type(primary_error).__name__,
                },
            )
        raise primary_error
    if cleanup_error is not None:
        raise BladeTaskError(f"BladeAI SDK cleanup failed: {type(cleanup_error).__name__}") from cleanup_error
    if result is None:
        raise BladeTaskError("BladeAI SDK returned no result")
    return result


def _assert_controlled_blade_shim() -> None:
    """Fail closed if BladeAI resolved its bundled native binary instead.

    The upstream resolver searches bundled paths before the environment
    override.  A correct agent-runtime image removes those paths; this check
    prevents an accidental image regression from silently bypassing the shim.
    """

    configured = os.environ.get("BLADE_AI_BLADE_PATH", "")
    if not configured:
        raise BladeTaskError("BLADE_AI_BLADE_PATH must name the controlled blade shim")
    expected = Path(configured).resolve()
    if not expected.is_file() or not os.access(expected, os.X_OK):
        raise BladeTaskError("controlled blade shim is not executable")
    try:
        from chaos_agent.utils.blade_paths import get_bundled_blade_path

        resolved = Path(get_bundled_blade_path()).resolve()
    except Exception as exc:
        raise BladeTaskError(f"unable to verify controlled blade shim: {type(exc).__name__}") from exc
    if resolved != expected:
        raise BladeTaskError("BladeAI resolved a native blade binary instead of the controlled shim")


def _install_worker_sdk_runtime(agent_cls: type) -> None:
    """Patch BladeAI's L4 pool inside this isolated worker process.

    BladeAI 0.6.2's L4 adapter initializes Agent Core with
    ``asyncio.run(create_agent(...))`` and does not pass ``mcp_manager``.  A
    connected MCP client owns transports, sessions, and locks tied to the loop
    that opened them, so this worker initializes MCP and compiles the graphs in
    the same event loop that executes them, then closes MCP before that loop is
    torn down.
    """

    if agent_cls.__dict__.get("_resbench_worker_mcp_runtime") is True:
        return

    try:
        import chaos_agent.l4.agent as l4_module
        from .bladeai_events import BladeAIStage2EventGraph
    except ImportError as exc:  # pragma: no cover - guarded by caller import.
        raise BladeTaskError("BladeAI L4 runtime or its event dependencies are unavailable") from exc

    class _WorkerMcpChaosAgentPool:
        inject_graph = None
        recover_graph = None
        skill_registry = None

        def __init__(self) -> None:
            self._initialized = False
            self._mcp_manager = None
            self._mcp_guard = None

        async def ensure_initialized_async(self) -> None:
            if self._initialized:
                return
            from langgraph.checkpoint.memory import MemorySaver

            from chaos_agent.agent.factory import create_agent
            import chaos_agent.agent.factory as factory_module
            from chaos_agent.config.settings import settings
            from chaos_agent.skills.loader import get_skills_dir
            from chaos_agent.skills.registry import SkillRegistry

            registry = SkillRegistry()
            skills_dir = get_skills_dir()
            if skills_dir.exists():
                registry.load_from_directory(skills_dir)

            checkpointer = MemorySaver()
            mcp_manager = None
            mcp_guard = None
            configured_servers: list[str] = []
            connected_servers: list[str] = []
            try:
                if not settings.mcp_enabled:
                    raise BladeTaskError("BladeAI MCP must be enabled for Stage-2 task mode")
                from chaos_agent.mcp.config import load_mcp_config
                from chaos_agent.mcp.manager import McpManager

                config_path = Path(settings.mcp_config_path).expanduser()
                configs = load_mcp_config(config_path)
                if not configs:
                    raise BladeTaskError("BladeAI MCP config is empty")
                configured_servers = [cfg.name for cfg in configs]
                mcp_manager = McpManager(configs=configs)
                await mcp_manager.connect_all(
                    connect_timeout_seconds=settings.mcp_connect_timeout_seconds,
                )
                connected_servers = [client.name for client in mcp_manager._clients]
                missing = sorted(set(configured_servers) - set(connected_servers))
                if missing:
                    raise BladeTaskError(
                        "BladeAI MCP failed to connect configured servers: "
                        + ", ".join(missing)
                    )
                policy_path = (
                    Path(__file__).resolve().parents[1] / "harness/mcp-tools.yaml"
                )
                try:
                    allowed_guard_tools = build_allowed_mcp_guard_tool_names(
                        mcp_manager,
                        policy_path,
                    )
                    mcp_guard = BladeAIMcpGuardPatch(allowed_guard_tools)
                    mcp_guard.install()
                except BladeAIMcpGuardError as exc:
                    raise BladeTaskError(str(exc)) from exc
                if _channel_only_enabled():
                    # BASE channel qualification must not construct the
                    # resilience-injection graph.  That graph includes the
                    # native blade_create path and can interpret a channel
                    # probe prompt as an experiment request.  The dedicated
                    # bounded probe below uses only connected MCP tools.
                    inject_graph = None
                    recover_graph = None
                else:
                    _apply_wp8_skill_guard(factory_module, registry)
                    agents = await create_agent(
                        registry,
                        checkpointer=checkpointer,
                        mcp_manager=mcp_manager,
                    )
                    inject_graph = BladeAIStage2EventGraph(
                        agents["inject"], emit=emit, operation="inject"
                    )
                    recover_graph = BladeAIStage2EventGraph(
                        agents["recover"], emit=emit, operation="recover"
                    )
                phase_tool_counts = {
                    phase: len(mcp_manager.tools_for_phase(phase))
                    for phase in ("clarification", "phase1", "phase2", "verifier")
                }
            except BaseException:
                if mcp_guard is not None:
                    mcp_guard.restore()
                if mcp_manager is not None:
                    await mcp_manager.disconnect_all()
                raise
            self.inject_graph = inject_graph
            self.recover_graph = recover_graph
            self.skill_registry = registry
            self._mcp_manager = mcp_manager
            self._mcp_guard = mcp_guard
            self._initialized = True
            emit(
                "mcp_lifecycle",
                {
                    "configured_servers": configured_servers,
                    "connected_servers": connected_servers,
                    "phase_tool_counts": phase_tool_counts,
                },
            )

        async def close(self) -> None:
            if self._mcp_guard is not None:
                self._mcp_guard.restore()
                self._mcp_guard = None
            if self._mcp_manager is not None:
                await self._mcp_manager.disconnect_all()
                self._mcp_manager = None
            self.inject_graph = None
            self.recover_graph = None
            self.skill_registry = None
            self._initialized = False

    def _patched_ensure_pool(self):
        if self._pool is None:
            l4_module._setup_logging()
            self._pool = _WorkerMcpChaosAgentPool()
        return self._pool

    def _patched_prepare(self, runtime, task) -> None:
        self._ensure_pool()

    def _patched_execute(self, runtime, task):
        if task.task_id in self._completed:
            return self._completed[task.task_id]
        self._state_transitions_buffer = []
        pool = self._ensure_pool()

        async def _run_once():
            await pool.ensure_initialized_async()
            try:
                if _channel_only_enabled():
                    result = await _run_channel_only(pool, runtime, task)
                    if runtime is not None and hasattr(runtime, "finish"):
                        runtime.finish(status=result.status)
                    return result
                with _wp8_confirmation_state(l4_module, task):
                    return await self._async_execute(pool, runtime, task)
            finally:
                await pool.close()

        from .bladeai_duration import preserve_explicit_fault_duration

        with preserve_explicit_fault_duration():
            result = asyncio.run(_run_once())
        if result.status in ("passed", "failed", "cancelled", "degraded"):
            self._completed[task.task_id] = result
            if len(self._completed) > 100:
                oldest_inserted = next(iter(self._completed))
                del self._completed[oldest_inserted]
        return result

    agent_cls._ensure_pool = _patched_ensure_pool
    agent_cls.prepare = _patched_prepare
    agent_cls.execute = _patched_execute
    agent_cls._resbench_worker_mcp_runtime = True


@contextmanager
def _capture_native_confirmation_proposal(runtime: Runtime):
    """Capture the actual SDK interrupt payload without modifying BladeAI.

    BladeAI 0.6.2 calls ``interrupt(confirmation_info)`` before its L4 adapter
    invokes ``Runtime.require_approval``.  The public Runtime callback only
    receives ``risk_level``; wrapping this imported function is the narrowest
    way to retain the Agent-authored confirmation payload in this isolated
    worker process.  Runtime copies only the fields it actually contains and
    delegates absent conditions to the common Harness policy.
    """

    if runtime.proposal_capture is None:
        yield
        return
    try:
        import importlib

        module = importlib.import_module("chaos_agent.agent.nodes.confirmation_gate")
        graph_module = importlib.import_module("chaos_agent.agent.graph")
    except ImportError as exc:
        raise BladeTaskError("BladeAI confirmation gate is unavailable for proposal capture") from exc
    original = module.interrupt
    original_gate = module.confirmation_gate
    original_graph_gate = graph_module.confirmation_gate

    def capture(value):
        if isinstance(value, dict):
            runtime.proposal_capture.record(value)
        return original(value)

    async def capture_gate(state):
        if isinstance(state, dict):
            runtime.proposal_capture.record_state(state)
        return await original_gate(state)

    module.interrupt = capture
    module.confirmation_gate = capture_gate
    graph_module.confirmation_gate = capture_gate
    try:
        yield
    finally:
        module.interrupt = original
        module.confirmation_gate = original_gate
        graph_module.confirmation_gate = original_graph_gate


if __name__ == "__main__":
    exit_code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # BladeAI may leave exporter/checkpoint threads alive after agent.cleanup.
    # This is an isolated worker process and all evidence is flushed above.
    os._exit(exit_code)
