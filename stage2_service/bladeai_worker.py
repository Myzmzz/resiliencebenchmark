"""Isolated BladeAI L4 subprocess entrypoint used by the Stage-2 service."""

from __future__ import annotations

import json
import os
import sys
import asyncio
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .bladeai_events import BladeAIStage2EventGraph
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
    print(
        json.dumps(
            {"type": "stage2_bladeai_event", "kind": kind, "payload": payload},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
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
            emit(
                "sdk_confirmation_proposed",
                {
                    "sdk_confirmation_id": sdk_confirmation_id,
                    "risk_level": risk_level,
                    "proposal_fields": sorted(str(key) for key in proposal),
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
    except ImportError as exc:  # pragma: no cover - guarded by caller import.
        raise BladeTaskError("BladeAI L4 module is unavailable") from exc

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
