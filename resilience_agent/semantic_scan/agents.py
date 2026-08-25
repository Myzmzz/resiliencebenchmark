"""LangChain subagents for semantic template analysis and independent review."""

from __future__ import annotations

import json
import signal
import threading
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, TypeVar

from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
)
from langchain.agents.structured_output import (
    ProviderStrategy,
    StructuredOutputValidationError,
    ToolStrategy,
)
from langchain.tools import tool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from resilience_agent.model_client import ModelConfig

from .codegraph_driver import CodeGraphDriver, CodeGraphError
from .config import AgentConfig
from .contracts import CoordinatorPlan, TemplateAgentOutput, VerificationDecision
from .evidence import EvidenceLedger
from .kubernetes_scanner import KubernetesConfigError, KubernetesConfigScanner
from .prompts import PromptRepository
from .registry import SemanticTemplate


class SemanticAgentError(RuntimeError):
    pass


class SemanticAgentTimeout(SemanticAgentError):
    pass


T = TypeVar("T", bound=BaseModel)


@contextmanager
def _agent_deadline(seconds: int):
    if threading.current_thread() is not threading.main_thread() or not hasattr(
        signal, "SIGALRM"
    ):
        yield
        return
    previous_handler = signal.getsignal(signal.SIGALRM)

    def expire(_signum, _frame):
        raise SemanticAgentTimeout(f"agent exceeded {seconds} seconds")

    signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def create_langchain_model(config: ModelConfig) -> ChatOpenAI:
    return ChatOpenAI(
        model=config.model,
        api_key=config.credential,
        base_url=config.base_url,
        timeout=config.timeout_seconds,
        max_retries=config.max_transport_retries,
        use_responses_api=True,
        reasoning={"effort": config.reasoning_effort},
        verbosity=config.text_verbosity,
        store=config.store,
        max_completion_tokens=config.max_output_tokens,
    )


def _bounded_tool_result(
    payload: Any,
    evidence: list[dict[str, Any]],
    max_chars: int,
) -> dict[str, Any]:
    value = {
        "result": deepcopy(payload),
        "evidence_catalog": deepcopy(evidence),
    }

    def rendered_size() -> int:
        return len(json.dumps(value, ensure_ascii=False, sort_keys=True))

    if rendered_size() <= max_chars:
        return value
    value["truncated_to_context_budget"] = True

    def candidates(item: Any, path: tuple[Any, ...] = ()) -> list[tuple[tuple[Any, ...], Any]]:
        output: list[tuple[tuple[Any, ...], Any]] = []
        if isinstance(item, dict):
            for key, child in item.items():
                output.extend(candidates(child, (*path, key)))
        elif isinstance(item, list):
            if len(item) > 1:
                output.append((path, item))
            for index, child in enumerate(item):
                output.extend(candidates(child, (*path, index)))
        elif isinstance(item, str) and len(item) > 512:
            output.append((path, item))
        return output

    def replace(path: tuple[Any, ...], replacement: Any) -> None:
        current = value
        for part in path[:-1]:
            current = current[part]
        current[path[-1]] = replacement

    while rendered_size() > max_chars:
        reducible = candidates(value)
        if not reducible:
            break
        path, item = max(
            reducible,
            key=lambda pair: len(
                json.dumps(pair[1], ensure_ascii=False, sort_keys=True)
            ),
        )
        if isinstance(item, list):
            replacement = item[: max(1, len(item) // 2)]
        else:
            retained = max(256, len(item) // 2)
            replacement = item[:retained] + "...<truncated>"
        replace(path, replacement)

    if rendered_size() <= max_chars:
        return value
    return {
        "result": {"summary": "tool result exceeded the bounded context budget"},
        "evidence_catalog": [
            {"evidence_id": item.get("evidence_id")}
            for item in evidence[:20]
            if item.get("evidence_id")
        ],
        "truncated_to_context_budget": True,
    }


def build_read_only_tools(
    codegraph: CodeGraphDriver,
    kubernetes: KubernetesConfigScanner,
    ledger: EvidenceLedger,
    *,
    max_result_chars: int,
    trace_sink: list[dict[str, Any]] | None = None,
    actor_context_provider: Callable[[], dict[str, Any]] | None = None,
):
    trace_lock = threading.Lock()
    tool_cache: dict[tuple[Any, ...], Any] = {}
    tool_cache_lock = threading.RLock()

    def cached_payload(key: tuple[Any, ...], producer: Callable[[], Any]) -> Any:
        with tool_cache_lock:
            if key in tool_cache:
                return deepcopy(tool_cache[key])
        payload = producer()
        with tool_cache_lock:
            cached = tool_cache.setdefault(key, deepcopy(payload))
            return deepcopy(cached)

    def trace(
        tool_name: str,
        arguments: dict[str, Any],
        ok: bool,
        evidence_count: int,
        error: str | None = None,
    ) -> None:
        if trace_sink is None:
            return
        actor_context = actor_context_provider() if actor_context_provider else {}
        record = {
            **actor_context,
            "tool": tool_name,
            "arguments": arguments,
            "ok": ok,
            "evidence_count": evidence_count,
            "error": error,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        }
        with trace_lock:
            trace_sink.append(record)

    def tool_error(exc: Exception) -> dict[str, Any]:
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc)[:1000],
            "instruction": (
                "The query was not resolved. Refine the symbol or query; do not infer evidence."
            ),
            "evidence_catalog": [],
        }

    @tool("codegraph_query")
    def codegraph_query(search: str, kind: str = "", limit: int = 20) -> dict[str, Any]:
        """Search CodeGraph symbols. Returns graph nodes plus stable evidence IDs."""
        try:
            resolved_limit = min(limit, 50)
            payload = cached_payload(
                ("codegraph_query", search, kind or None, resolved_limit),
                lambda: codegraph.query(search, kind=kind or None, limit=resolved_limit),
            )
        except CodeGraphError as exc:
            trace("codegraph_query", {"search": search, "kind": kind, "limit": limit}, False, 0, str(exc)[:500])
            return tool_error(exc)
        evidence = ledger.add_codegraph(payload, query=f"query:{search}")
        trace("codegraph_query", {"search": search, "kind": kind, "limit": limit}, True, len(evidence))
        return _bounded_tool_result(
            payload,
            [item.model_dump(mode="json") for item in evidence],
            max_result_chars,
        )

    @tool("codegraph_callers")
    def codegraph_callers(symbol: str, limit: int = 20) -> dict[str, Any]:
        """Return callers of one exact symbol and stable evidence IDs."""
        try:
            resolved_limit = min(limit, 50)
            payload = cached_payload(
                ("codegraph_callers", symbol, resolved_limit),
                lambda: codegraph.callers(symbol, limit=resolved_limit),
            )
        except CodeGraphError as exc:
            trace("codegraph_callers", {"symbol": symbol, "limit": limit}, False, 0, str(exc)[:500])
            return tool_error(exc)
        evidence = ledger.add_codegraph(payload, query=f"callers:{symbol}")
        trace("codegraph_callers", {"symbol": symbol, "limit": limit}, True, len(evidence))
        return _bounded_tool_result(
            payload,
            [item.model_dump(mode="json") for item in evidence],
            max_result_chars,
        )

    @tool("codegraph_callees")
    def codegraph_callees(symbol: str, limit: int = 20) -> dict[str, Any]:
        """Return callees of one exact symbol and stable evidence IDs."""
        try:
            resolved_limit = min(limit, 50)
            payload = cached_payload(
                ("codegraph_callees", symbol, resolved_limit),
                lambda: codegraph.callees(symbol, limit=resolved_limit),
            )
        except CodeGraphError as exc:
            trace("codegraph_callees", {"symbol": symbol, "limit": limit}, False, 0, str(exc)[:500])
            return tool_error(exc)
        evidence = ledger.add_codegraph(payload, query=f"callees:{symbol}")
        trace("codegraph_callees", {"symbol": symbol, "limit": limit}, True, len(evidence))
        return _bounded_tool_result(
            payload,
            [item.model_dump(mode="json") for item in evidence],
            max_result_chars,
        )

    @tool("codegraph_context")
    def codegraph_context(task: str, max_nodes: int = 40, max_code_blocks: int = 8) -> dict[str, Any]:
        """Build bounded CodeGraph task context with nodes, edges and code excerpts."""
        try:
            resolved_max_nodes = min(max_nodes, 100)
            resolved_max_code_blocks = min(max_code_blocks, 20)
            payload = cached_payload(
                (
                    "codegraph_context",
                    task,
                    resolved_max_nodes,
                    resolved_max_code_blocks,
                ),
                lambda: codegraph.context(
                    task,
                    max_nodes=resolved_max_nodes,
                    max_code_blocks=resolved_max_code_blocks,
                ),
            )
        except CodeGraphError as exc:
            trace(
                "codegraph_context",
                {"task": task, "max_nodes": max_nodes, "max_code_blocks": max_code_blocks},
                False,
                0,
                str(exc)[:500],
            )
            return tool_error(exc)
        evidence = ledger.add_codegraph(payload, query=f"context:{task}")
        trace(
            "codegraph_context",
            {"task": task, "max_nodes": max_nodes, "max_code_blocks": max_code_blocks},
            True,
            len(evidence),
        )
        return _bounded_tool_result(
            payload,
            [item.model_dump(mode="json") for item in evidence],
            max_result_chars,
        )

    @tool("kubernetes_list_resources")
    def kubernetes_list_resources(
        kinds: list[str], name_contains: str = "", limit: int = 50
    ) -> dict[str, Any]:
        """List typed, secret-safe Kubernetes manifest resources and evidence IDs."""
        try:
            resolved_limit = min(limit, 100)
            normalized_kinds = tuple(sorted(kinds))
            payload = cached_payload(
                (
                    "kubernetes_list_resources",
                    normalized_kinds,
                    name_contains or None,
                    resolved_limit,
                ),
                lambda: kubernetes.list_resources(
                    kinds=list(normalized_kinds),
                    name_contains=name_contains or None,
                    limit=resolved_limit,
                ),
            )
        except KubernetesConfigError as exc:
            trace(
                "kubernetes_list_resources",
                {"kinds": kinds, "name_contains": name_contains, "limit": limit},
                False,
                0,
                str(exc)[:500],
            )
            return tool_error(exc)
        evidence = ledger.add_kubernetes(payload)
        trace(
            "kubernetes_list_resources",
            {"kinds": kinds, "name_contains": name_contains, "limit": limit},
            True,
            len(evidence),
        )
        return _bounded_tool_result(
            payload,
            [item.model_dump(mode="json") for item in evidence],
            max_result_chars,
        )

    @tool("kubernetes_get_resource")
    def kubernetes_get_resource(kind: str, name: str) -> dict[str, Any]:
        """Get exact typed Kubernetes resources by kind and name."""
        try:
            payload = cached_payload(
                ("kubernetes_get_resource", kind, name),
                lambda: kubernetes.get_resource(kind, name),
            )
        except KubernetesConfigError as exc:
            trace("kubernetes_get_resource", {"kind": kind, "name": name}, False, 0, str(exc)[:500])
            return tool_error(exc)
        evidence = ledger.add_kubernetes({"resources": payload["matches"]})
        trace("kubernetes_get_resource", {"kind": kind, "name": name}, True, len(evidence))
        return _bounded_tool_result(
            payload,
            [item.model_dump(mode="json") for item in evidence],
            max_result_chars,
        )

    @tool("kubernetes_get_config_value")
    def kubernetes_get_config_value(name: str, key: str) -> dict[str, Any]:
        """Return one non-secret ConfigMap data value by name and key."""
        try:
            payload = cached_payload(
                ("kubernetes_get_config_value", name, key),
                lambda: kubernetes.get_configmap_value(name, key=key),
            )
        except KubernetesConfigError as exc:
            trace("kubernetes_get_config_value", {"name": name, "key": key}, False, 0, str(exc)[:500])
            return tool_error(exc)
        evidence = ledger.add_kubernetes({"resources": payload["matches"]})
        trace("kubernetes_get_config_value", {"name": name, "key": key}, True, len(evidence))
        return _bounded_tool_result(
            payload,
            [item.model_dump(mode="json") for item in evidence],
            max_result_chars,
        )

    return [
        codegraph_query,
        codegraph_callers,
        codegraph_callees,
        codegraph_context,
        kubernetes_list_resources,
        kubernetes_get_resource,
        kubernetes_get_config_value,
    ]


def _usage(messages: list[Any]) -> dict[str, int]:
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for message in messages:
        usage = getattr(message, "usage_metadata", None)
        if not isinstance(usage, dict):
            continue
        totals["input_tokens"] += int(usage.get("input_tokens") or 0)
        totals["output_tokens"] += int(usage.get("output_tokens") or 0)
        totals["total_tokens"] += int(usage.get("total_tokens") or 0)
    return totals


class LangChainSemanticAgents:
    def __init__(
        self,
        *,
        model: Any,
        config: AgentConfig,
        prompts: PromptRepository,
        codegraph: CodeGraphDriver,
        kubernetes: KubernetesConfigScanner,
        ledger: EvidenceLedger,
        agent_builder: Callable[..., Any] = create_agent,
    ):
        self.model = model
        self.config = config
        self.prompts = prompts
        self.ledger = ledger
        self.tool_traces: list[dict[str, Any]] = []
        self._actor_context: ContextVar[dict[str, Any] | None] = ContextVar(
            "semantic_agent_actor",
            default=None,
        )
        self._agent_cache_lock = threading.Lock()
        self.tools = build_read_only_tools(
            codegraph,
            kubernetes,
            ledger,
            max_result_chars=config.context_budget.max_tool_result_chars,
            trace_sink=self.tool_traces,
            actor_context_provider=self._current_actor,
        )
        self.agent_builder = agent_builder
        self._template_agents: dict[str, Any] = {}
        self._template_tool_fallback_agents: dict[str, Any] = {}
        self._coordinator = self._build(
            self.prompts.read("coordinator.md"),
            CoordinatorPlan,
            strategy_override="tool",
            tools_override=[],
        )
        self._verifier = self._build(
            self.prompts.read("verifier.md"), VerificationDecision
        )
        self._verifier_tool_fallback: Any | None = None

    def plan(self, context: dict[str, Any]) -> tuple[CoordinatorPlan, dict[str, int]]:
        return self._invoke(
            self._coordinator,
            context,
            CoordinatorPlan,
            actor={"phase": "coordinator", "template_id": None, "finding_id": None},
        )

    def analyze(
        self,
        template: SemanticTemplate,
        context: dict[str, Any],
    ) -> tuple[TemplateAgentOutput, dict[str, int]]:
        agent = self._template_agents.get(template.template_id)
        if agent is None:
            with self._agent_cache_lock:
                agent = self._template_agents.get(template.template_id)
                if agent is None:
                    agent = self._build(
                        self.prompts.template_prompt(template.prompt_file),
                        TemplateAgentOutput,
                    )
                    self._template_agents[template.template_id] = agent
        result, usage = self._invoke_with_structured_fallback(
            agent,
            context,
            TemplateAgentOutput,
            fallback_factory=lambda: self._template_tool_fallback(template),
            actor={
                "phase": "template",
                "template_id": template.template_id,
                "finding_id": None,
            },
        )
        findings = []
        for finding in result.findings:
            mechanism_chain = [
                step.model_copy(
                    update={
                        "evidence_ids": [
                            self.ledger.resolve_id(item) for item in step.evidence_ids
                        ]
                    }
                )
                for step in finding.mechanism_chain
            ]
            alternatives = [
                item.model_copy(
                    update={
                        "evidence_ids": [
                            self.ledger.resolve_id(evidence_id)
                            for evidence_id in item.evidence_ids
                        ]
                    }
                )
                for item in finding.alternatives_checked
            ]
            residual_hypotheses = [
                item.model_copy(
                    update={
                        "evidence_ids": [
                            self.ledger.resolve_id(evidence_id)
                            for evidence_id in item.evidence_ids
                        ]
                    }
                )
                for item in finding.residual_hypotheses
            ]
            referenced_ids = [
                self.ledger.resolve_id(item) for item in finding.evidence_ids
            ]
            for step in mechanism_chain:
                referenced_ids.extend(step.evidence_ids)
            for alternative in alternatives:
                referenced_ids.extend(alternative.evidence_ids)
            for residual in residual_hypotheses:
                referenced_ids.extend(residual.evidence_ids)
            proposed_d_class = finding.proposed_d_class
            if proposed_d_class not in template.d_class.allowed:
                proposed_d_class = template.d_class.fixed or template.d_class.allowed[0]
            findings.append(
                finding.model_copy(
                    update={
                        "proposed_d_class": proposed_d_class,
                        "evidence_ids": list(dict.fromkeys(referenced_ids)),
                        "mechanism_chain": mechanism_chain,
                        "alternatives_checked": alternatives,
                        "residual_hypotheses": residual_hypotheses,
                    }
                )
            )
        result = result.model_copy(
            update={"defect_name": template.defect_name, "findings": findings}
        )
        if result.template_id != template.template_id:
            raise SemanticAgentError(
                f"{template.template_id} agent returned {result.template_id}"
            )
        return result, usage

    def verify(
        self,
        template: SemanticTemplate,
        context: dict[str, Any],
    ) -> tuple[VerificationDecision, dict[str, int]]:
        result, usage = self._invoke_with_structured_fallback(
            self._verifier,
            context,
            VerificationDecision,
            fallback_factory=self._verifier_tool_agent,
            actor={
                "phase": "verifier",
                "template_id": template.template_id,
                "finding_id": str(context.get("candidate", {}).get("finding_id") or ""),
            },
        )
        result = result.model_copy(
            update={
                "verified_evidence_ids": [
                    self.ledger.resolve_id(item)
                    for item in result.verified_evidence_ids
                ],
                "invalid_evidence_ids": [
                    self.ledger.resolve_id(item)
                    for item in result.invalid_evidence_ids
                ],
                "residual_hypotheses": [
                    item.model_copy(
                        update={
                            "evidence_ids": [
                                self.ledger.resolve_id(evidence_id)
                                for evidence_id in item.evidence_ids
                            ]
                        }
                    )
                    for item in result.residual_hypotheses
                ],
            }
        )
        if result.template_id != template.template_id:
            raise SemanticAgentError(
                f"verifier returned {result.template_id} for {template.template_id}"
            )
        return result, usage

    def _template_tool_fallback(self, template: SemanticTemplate):
        agent = self._template_tool_fallback_agents.get(template.template_id)
        if agent is None:
            with self._agent_cache_lock:
                agent = self._template_tool_fallback_agents.get(template.template_id)
                if agent is None:
                    agent = self._build(
                        self.prompts.template_prompt(template.prompt_file),
                        TemplateAgentOutput,
                        strategy_override="tool",
                    )
                    self._template_tool_fallback_agents[template.template_id] = agent
        return agent

    def _verifier_tool_agent(self):
        if self._verifier_tool_fallback is None:
            with self._agent_cache_lock:
                if self._verifier_tool_fallback is None:
                    self._verifier_tool_fallback = self._build(
                        self.prompts.read("verifier.md"),
                        VerificationDecision,
                        strategy_override="tool",
                    )
        return self._verifier_tool_fallback

    def _build(
        self,
        system_prompt: str,
        schema: type[T],
        *,
        strategy_override: str | None = None,
        tools_override: list[Any] | None = None,
    ):
        strategy: Any
        selected_strategy = strategy_override or self.config.structured_output_strategy
        if selected_strategy == "tool":
            strategy = ToolStrategy(schema)
        elif selected_strategy == "provider":
            strategy = ProviderStrategy(schema, strict=True)
        else:
            strategy = schema
        return self.agent_builder(
            model=self.model,
            tools=self.tools if tools_override is None else tools_override,
            system_prompt=system_prompt,
            response_format=strategy,
            middleware=[
                ToolCallLimitMiddleware(
                    run_limit=self.config.tool_call_limit,
                    exit_behavior="continue",
                ),
                ModelCallLimitMiddleware(
                    run_limit=self.config.model_call_limit,
                    exit_behavior="error",
                ),
            ],
            name=f"semantic_{schema.__name__.lower()}",
        )

    def _invoke_with_structured_fallback(
        self,
        agent: Any,
        context: dict[str, Any],
        schema: type[T],
        *,
        fallback_factory: Callable[[], Any],
        actor: dict[str, Any] | None = None,
    ) -> tuple[T, dict[str, int]]:
        try:
            return self._invoke(agent, context, schema, actor=actor)
        except (StructuredOutputValidationError, SemanticAgentError):
            if self.config.structured_output_strategy != "provider":
                raise
            return self._invoke(fallback_factory(), context, schema, actor=actor)

    def _invoke(
        self,
        agent: Any,
        context: dict[str, Any],
        schema: type[T],
        *,
        actor: dict[str, Any] | None = None,
    ) -> tuple[T, dict[str, int]]:
        actor_token = self._actor_context.set(dict(actor or {}))
        try:
            with _agent_deadline(self.config.agent_timeout_seconds):
                result = agent.invoke(
                    {
                        "messages": [
                            {
                                "role": "user",
                                "content": json.dumps(
                                    context, ensure_ascii=False, sort_keys=True
                                ),
                            }
                        ]
                    },
                    config={"recursion_limit": self.config.recursion_limit},
                )
        finally:
            self._actor_context.reset(actor_token)
        structured = result.get("structured_response")
        try:
            parsed = structured if isinstance(structured, schema) else schema.model_validate(structured)
        except Exception as exc:
            raise SemanticAgentError(
                f"agent did not return {schema.__name__}: {exc}"
            ) from exc
        return parsed, _usage(result.get("messages", []))

    def _current_actor(self) -> dict[str, Any]:
        return dict(self._actor_context.get() or {})
