"""LangGraph workflow coordinating twelve isolated LangChain subagents."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from resilience_agent.model_client import load_model_config

from .agents import LangChainSemanticAgents, create_langchain_model
from .codegraph_driver import CodeGraphDriver
from .config import SemanticScanConfig
from .context import SemanticContextManager
from .contracts import (
    CoordinatorPlan,
    DClass,
    SemanticScanReport,
    TemplateAgentOutput,
    TemplateCoverage,
    TemplateFinding,
    TemplateMatch,
    TemplatePlan,
    VerificationDecision,
)
from .evidence import EvidenceLedger
from .kubernetes_scanner import KubernetesConfigScanner, LiveKubernetesScanner
from .prompts import PromptRepository
from .registry import SemanticTemplate, SemanticTemplateRegistry, load_template_registry


class SemanticScanError(RuntimeError):
    pass


_RETRYABLE_AGENT_ERRORS = {
    "APITimeoutError",
    "OpenAITimeoutError",
    "StructuredOutputValidationError",
}


class AgentBackend(Protocol):
    def plan(self, context: dict[str, Any]) -> tuple[CoordinatorPlan, dict[str, int]]: ...

    def analyze(
        self, template: SemanticTemplate, context: dict[str, Any]
    ) -> tuple[TemplateAgentOutput, dict[str, int]]: ...

    def verify(
        self, template: SemanticTemplate, context: dict[str, Any]
    ) -> tuple[VerificationDecision, dict[str, int]]: ...


class ScanState(TypedDict, total=False):
    graph_manifest: dict[str, Any]
    kubernetes_manifest: dict[str, Any]
    plan: dict[str, Any]
    drafts: dict[str, dict[str, Any]]
    verifications: dict[str, dict[str, Any]]
    errors: dict[str, str]
    usage: dict[str, dict[str, int]]
    report: dict[str, Any]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _run_id() -> str:
    now = datetime.now(timezone.utc)
    material = now.isoformat().encode()
    return f"semantic-{now:%Y%m%dt%H%M%sz}-{hashlib.sha256(material).hexdigest()[:8]}"


class SemanticScanWorkflow:
    def __init__(
        self,
        config: SemanticScanConfig,
        *,
        codegraph: CodeGraphDriver | None = None,
        kubernetes: KubernetesConfigScanner | None = None,
        ledger: EvidenceLedger | None = None,
        registry: SemanticTemplateRegistry | None = None,
        agents: AgentBackend | None = None,
        run_id: str | None = None,
        resume: bool = False,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.config = config
        self.registry = registry or load_template_registry(config.templates_path)
        known_ids = {item.template_id for item in self.registry.templates}
        unknown_ids = set(config.active_template_ids) - known_ids
        if unknown_ids:
            raise SemanticScanError(
                "unknown active template IDs: " + ", ".join(sorted(unknown_ids))
            )
        active = set(config.active_template_ids)
        self.templates = [
            item for item in self.registry.templates if item.template_id in active
        ]
        self.codegraph = codegraph or CodeGraphDriver(config.codebase, config.codegraph)
        if kubernetes is not None:
            self.kubernetes = kubernetes
        elif config.kubernetes.mode == "live":
            self.kubernetes = LiveKubernetesScanner(
                config.kubernetes.namespace,
                kubeconfig=(
                    str(config.kubernetes.kubeconfig_path)
                    if config.kubernetes.kubeconfig_path is not None
                    else None
                ),
                max_resources_per_agent=config.kubernetes.max_resources_per_agent,
                authoritative_for_namespace=config.kubernetes.authoritative_for_namespace,
            )
        else:
            self.kubernetes = KubernetesConfigScanner(config.kubernetes)
        self.ledger = ledger or EvidenceLedger()
        self.prompts = PromptRepository(config.prompts_root)
        self.contexts = SemanticContextManager(
            self.codegraph,
            self.kubernetes,
            self.ledger,
            config.agents.context_budget,
        )
        if agents is None:
            model_config = load_model_config(config.agents.model_config_path)
            model = create_langchain_model(model_config)
            agents = LangChainSemanticAgents(
                model=model,
                config=config.agents,
                prompts=self.prompts,
                codegraph=self.codegraph,
                kubernetes=self.kubernetes,
                ledger=self.ledger,
            )
            self.model_public = model_config.public_dict()
        else:
            self.model_public = {"provider": "injected-agent-backend"}
        self.agents = agents
        self.run_id = run_id or _run_id()
        self.run_root = config.output_dir / self.run_id
        self.resume = resume
        self.event_sink = event_sink
        self._event_lock = threading.Lock()
        self.graph = self._build_graph()

    def run(self) -> SemanticScanReport:
        initial = self._load_checkpoint() if self.resume else {}
        self._emit("run_started", resumed=bool(initial))
        result = self.graph.invoke(
            initial, config={"recursion_limit": self.config.agents.recursion_limit}
        )
        report = SemanticScanReport.model_validate(result["report"])
        self._persist(result, report)
        self._emit(
            "run_completed",
            matches=len(report.matches),
            question_eligible_count=report.question_eligible_count,
        )
        return report

    def _build_graph(self):
        builder = StateGraph(ScanState)
        builder.add_node("prepare", self._prepare)
        builder.add_node("plan", self._plan)
        builder.add_node("analyze", self._analyze)
        builder.add_node("verify", self._verify)
        builder.add_node("synthesize", self._synthesize)
        builder.add_edge(START, "prepare")
        builder.add_edge("prepare", "plan")
        builder.add_edge("plan", "analyze")
        builder.add_edge("analyze", "verify")
        builder.add_edge("verify", "synthesize")
        builder.add_edge("synthesize", END)
        return builder.compile()

    def _prepare(self, state: ScanState) -> ScanState:
        if state.get("graph_manifest") and state.get("kubernetes_manifest"):
            graph_manifest = self.codegraph.ensure_index()
            kubernetes_manifest = self.kubernetes.scan()
            if (
                graph_manifest.index_sha256
                != state["graph_manifest"]["index_sha256"]
                or kubernetes_manifest.manifest_sha256
                != state["kubernetes_manifest"]["manifest_sha256"]
            ):
                raise SemanticScanError(
                    "resume inputs differ from the checkpointed graph or Kubernetes manifests"
                )
            self._emit("prepare_reused")
            return {}
        self._emit("prepare_started")
        graph_manifest = self.codegraph.ensure_index()
        kubernetes_manifest = self.kubernetes.scan()
        update: ScanState = {
            "graph_manifest": graph_manifest.model_dump(mode="json"),
            "kubernetes_manifest": kubernetes_manifest.model_dump(mode="json"),
            "drafts": {},
            "verifications": {},
            "errors": {},
            "usage": {},
        }
        self._checkpoint(update)
        self._emit(
            "prepare_completed",
            graph_nodes=graph_manifest.node_count,
            kubernetes_resources=kubernetes_manifest.resource_count,
        )
        return update

    def _plan(self, state: ScanState) -> ScanState:
        if state.get("plan"):
            self._emit("plan_reused")
            return {}
        self._emit("plan_started")
        context = self.contexts.coordinator_context(
            graph_manifest=state["graph_manifest"],
            template_index=[
                item
                for item in self.registry.index_for_prompt()
                if item["template_id"] in set(self.config.active_template_ids)
            ],
        )
        if self.config.planning_mode == "deterministic":
            plan = CoordinatorPlan(
                plans=[
                    TemplatePlan(
                        template_id=template.template_id,
                        codegraph_focus=template.graph_focus,
                        kubernetes_focus=template.kubernetes_kinds,
                        priority="high",
                        rationale=(
                            "Controller uses the frozen template evidence contract for a "
                            "positive or negative control."
                        ),
                    )
                    for template in self.templates
                ]
            )
            usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        else:
            plan, usage = self.agents.plan(context)
        if {item.template_id for item in plan.plans} != {
            item.template_id for item in self.templates
        }:
            raise SemanticScanError("coordinator plan does not cover the active templates")
        update: ScanState = {
            "plan": plan.model_dump(mode="json"),
            "usage": {**state.get("usage", {}), "coordinator": usage},
        }
        self._checkpoint({**state, **update})
        self._emit("plan_completed", templates=len(plan.plans))
        return update

    def _analyze(self, state: ScanState) -> ScanState:
        plan = CoordinatorPlan.model_validate(state["plan"])
        plans = {item.template_id: item for item in plan.plans}
        drafts: dict[str, dict[str, Any]] = dict(state.get("drafts", {}))
        errors = dict(state.get("errors", {}))
        usage = dict(state.get("usage", {}))
        pending: list[SemanticTemplate] = []
        for template in self.templates:
            if template.template_id in drafts:
                self._emit("template_reused", template_id=template.template_id)
                continue
            pending.append(template)
        if pending:
            max_workers = min(self.config.agents.max_concurrency, len(pending))
            with ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="semantic-template",
            ) as executor:
                futures = {
                    executor.submit(
                        self._analyze_one,
                        template,
                        plans[template.template_id],
                    ): template
                    for template in pending
                }
                for future in as_completed(futures):
                    template = futures[future]
                    try:
                        draft, item_usage = future.result()
                    except Exception as exc:  # noqa: BLE001 - preserve coverage and abstain.
                        errors[template.template_id] = (
                            f"{type(exc).__name__}: {str(exc)[:1200]}"
                        )
                    else:
                        drafts[template.template_id] = draft.model_dump(mode="json")
                        usage[f"agent:{template.template_id}"] = item_usage
                        errors.pop(template.template_id, None)
                    self._checkpoint(
                        {
                            **state,
                            "drafts": self._ordered_drafts(drafts),
                            "errors": self._ordered_mapping(errors),
                            "usage": self._ordered_mapping(usage),
                        }
                    )
        return {
            "drafts": self._ordered_drafts(drafts),
            "errors": self._ordered_mapping(errors),
            "usage": self._ordered_mapping(usage),
        }

    def _analyze_one(
        self,
        template: SemanticTemplate,
        plan: TemplatePlan,
    ) -> tuple[TemplateAgentOutput, dict[str, int]]:
        self._emit("template_started", template_id=template.template_id)
        try:
            context = self.contexts.subagent_context(template, plan)
            draft, item_usage = self._call_agent_with_retry(
                phase="template",
                template_id=template.template_id,
                operation=lambda template=template, context=context: self.agents.analyze(
                    template, context
                ),
            )
            _write_json(
                self.run_root / "agent-attempts" / f"{template.template_id}.json",
                draft.model_dump(mode="json"),
            )
            self._validate_draft(template, draft)
        except Exception as exc:
            self._emit(
                "template_failed",
                template_id=template.template_id,
                error_type=type(exc).__name__,
            )
            raise
        self._emit(
            "template_completed",
            template_id=template.template_id,
            scan_status=draft.scan_status,
            finding_count=len(draft.findings),
        )
        return draft, item_usage

    def _verify(self, state: ScanState) -> ScanState:
        verifications: dict[str, dict[str, Any]] = dict(
            state.get("verifications", {})
        )
        errors = dict(state.get("errors", {}))
        usage = dict(state.get("usage", {}))
        tasks: list[tuple[SemanticTemplate, TemplateFinding]] = []
        for template_id, raw in state.get("drafts", {}).items():
            draft = TemplateAgentOutput.model_validate(raw)
            if draft.scan_status != "candidate":
                continue
            template = self.registry.by_id(template_id)
            for finding in draft.findings:
                verification_key = finding.finding_id
                if verification_key in verifications:
                    self._emit(
                        "verification_reused",
                        template_id=template_id,
                        finding_id=finding.finding_id,
                    )
                    continue
                tasks.append((template, finding))
        if tasks:
            max_workers = min(self.config.agents.max_concurrency, len(tasks))
            with ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="semantic-verifier",
            ) as executor:
                futures = {
                    executor.submit(self._verify_one, template, finding): (
                        template,
                        finding,
                    )
                    for template, finding in tasks
                }
                for future in as_completed(futures):
                    template, finding = futures[future]
                    verification_key = finding.finding_id
                    try:
                        decision, item_usage = future.result()
                    except Exception as exc:  # noqa: BLE001 - failed review cannot pass.
                        errors[f"verify:{finding.finding_id}"] = (
                            f"{type(exc).__name__}: {str(exc)[:1200]}"
                        )
                    else:
                        verifications[verification_key] = decision.model_dump(
                            mode="json"
                        )
                        usage[f"verifier:{finding.finding_id}"] = item_usage
                        errors.pop(f"verify:{finding.finding_id}", None)
                    self._checkpoint(
                        {
                            **state,
                            "verifications": self._ordered_mapping(verifications),
                            "errors": self._ordered_mapping(errors),
                            "usage": self._ordered_mapping(usage),
                        }
                    )
        return {
            "verifications": self._ordered_mapping(verifications),
            "errors": self._ordered_mapping(errors),
            "usage": self._ordered_mapping(usage),
        }

    def _verify_one(
        self,
        template: SemanticTemplate,
        finding: TemplateFinding,
    ) -> tuple[VerificationDecision, dict[str, int]]:
        self._emit(
            "verification_started",
            template_id=template.template_id,
            finding_id=finding.finding_id,
        )
        try:
            context = self.contexts.verifier_context(
                template, finding.model_dump(mode="json")
            )
            decision, item_usage = self._call_agent_with_retry(
                phase="verification",
                template_id=template.template_id,
                operation=lambda template=template, context=context: self.agents.verify(
                    template, context
                ),
            )
            _write_json(
                self.run_root
                / "verification-attempts"
                / f"{finding.finding_id}.json",
                decision.model_dump(mode="json"),
            )
            self._validate_verification(finding, decision)
        except Exception as exc:
            self._emit(
                "verification_failed",
                template_id=template.template_id,
                finding_id=finding.finding_id,
                error_type=type(exc).__name__,
            )
            raise
        self._emit(
            "verification_completed",
            template_id=template.template_id,
            finding_id=finding.finding_id,
            verdict=decision.verdict,
        )
        return decision, item_usage

    def _call_agent_with_retry(
        self,
        *,
        phase: str,
        template_id: str,
        operation: Callable[[], tuple[Any, dict[str, int]]],
    ) -> tuple[Any, dict[str, int]]:
        max_attempts = self.config.agents.max_attempts_per_agent
        for attempt in range(1, max_attempts + 1):
            try:
                return operation()
            except Exception as exc:
                retryable = type(exc).__name__ in _RETRYABLE_AGENT_ERRORS
                if not retryable or attempt == max_attempts:
                    raise
                self._emit(
                    f"{phase}_retrying",
                    template_id=template_id,
                    attempt=attempt + 1,
                    previous_error_type=type(exc).__name__,
                )
        raise AssertionError("unreachable agent retry loop")

    def _synthesize(self, state: ScanState) -> ScanState:
        matches: list[TemplateMatch] = []
        coverage: list[TemplateCoverage] = []
        errors = state.get("errors", {})
        for template in self.templates:
            template_id = template.template_id
            if template_id in errors:
                coverage.append(
                    TemplateCoverage(
                        template_id=template_id,
                        status="scan_failed",
                        explanation=errors[template_id],
                    )
                )
                continue
            raw = state.get("drafts", {}).get(template_id)
            if raw is None:
                coverage.append(
                    TemplateCoverage(
                        template_id=template_id,
                        status="scan_failed",
                        explanation="template agent returned no result",
                    )
                )
                continue
            draft = TemplateAgentOutput.model_validate(raw)
            if draft.scan_status != "candidate":
                coverage.append(
                    TemplateCoverage(
                        template_id=template_id,
                        status=draft.scan_status,
                        explanation=draft.explanation,
                    )
                )
                continue
            template_matched = False
            template_rejected = True
            explanations = []
            for finding in draft.findings:
                decision_raw = state.get("verifications", {}).get(finding.finding_id)
                if decision_raw is None:
                    explanations.append(
                        errors.get(
                            f"verify:{finding.finding_id}",
                            f"{finding.finding_id}: independent verifier returned no result",
                        )
                    )
                    template_rejected = False
                    continue
                decision = VerificationDecision.model_validate(decision_raw)
                match = self._final_match(template, finding, decision, state)
                matches.append(match)
                if match.candidate_status == "unactionable_candidate":
                    explanations.append(f"{finding.finding_id}: {decision.explanation}")
                    template_rejected = template_rejected and decision.verdict == "rejected"
                    continue
                template_matched = True
                template_rejected = False
                explanations.append(f"{finding.finding_id}: {decision.explanation}")
            if template_matched:
                status = "matched"
            elif template_rejected:
                status = "rejected"
            else:
                status = "insufficient_evidence"
            coverage.append(
                TemplateCoverage(
                    template_id=template_id,
                    status=status,
                    explanation="; ".join(explanations)[:1500] or draft.explanation,
                )
            )
        tool_traces = list(getattr(self.agents, "tool_traces", []))
        report = SemanticScanReport(
            run_id=self.run_id,
            generated_at=datetime.now(timezone.utc).isoformat(),
            codegraph=state["graph_manifest"],
            kubernetes=state["kubernetes_manifest"],
            template_registry_version=self.registry.registry_version,
            model={**self.model_public, "usage_by_agent": state.get("usage", {})},
            matches=matches,
            coverage=coverage,
            question_eligible_count=sum(item.question_eligible for item in matches),
            scan_coverage=self._scan_coverage(tool_traces),
            tool_traces=tool_traces,
            limitations=[
                "Static semantic matches remain candidate defects until a qualified fault experiment confirms them.",
                *[f"{key}: {value}" for key, value in sorted(errors.items())],
            ],
        )
        return {"report": report.model_dump(mode="json")}

    def _checkpoint(self, state: Mapping[str, Any]) -> None:
        serializable = {
            key: value
            for key, value in state.items()
            if key
            in {
                "graph_manifest",
                "kubernetes_manifest",
                "plan",
                "drafts",
                "verifications",
                "errors",
                "usage",
                "report",
            }
        }
        _write_json(self.run_root / "checkpoint-state.json", serializable)
        _write_json(self.run_root / "evidence-ledger.json", self.ledger.catalog())

    def _ordered_drafts(
        self, drafts: Mapping[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        return {
            template.template_id: drafts[template.template_id]
            for template in self.templates
            if template.template_id in drafts
        }

    @staticmethod
    def _ordered_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
        return {key: values[key] for key in sorted(values)}

    def _load_checkpoint(self) -> ScanState:
        path = self.run_root / "checkpoint-state.json"
        if not path.is_file():
            raise SemanticScanError(f"resume checkpoint does not exist: {path}")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise SemanticScanError("semantic scan checkpoint is not a JSON object")
        evidence_path = self.run_root / "evidence-ledger.json"
        if evidence_path.is_file():
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            if not isinstance(evidence, list):
                raise SemanticScanError("semantic scan evidence checkpoint is invalid")
            self.ledger.load(evidence)
        return value

    def _scan_coverage(self, tool_traces: list[dict[str, Any]]) -> dict[str, Any]:
        evidence = self.ledger.catalog()
        code_paths = sorted(
            {
                item["path"]
                for item in evidence
                if item.get("path")
                and str(item.get("kind", "")).startswith("codegraph")
            }
        )
        k8s_symbols = sorted(
            {
                item["symbol"]
                for item in evidence
                if item.get("symbol")
                and str(item.get("kind", "")).startswith("kubernetes")
            }
        )
        trace_counts: dict[str, int] = {}
        for trace in tool_traces:
            tool_name = str(trace.get("tool") or "unknown")
            trace_counts[tool_name] = trace_counts.get(tool_name, 0) + 1
        return {
            "codegraph": {
                "visited_file_count": len(code_paths),
                "visited_files": code_paths[:500],
                "parse_failures": self.codegraph.manifest.parse_failures,
                "manifest_coverage": self.codegraph.manifest.coverage,
            },
            "kubernetes": {
                "visited_resource_count": len(k8s_symbols),
                "visited_resources": k8s_symbols[:500],
                "manifest_kinds": self.kubernetes.manifest.kinds,
            },
            "tool_trace_counts": dict(sorted(trace_counts.items())),
            "evidence_count": len(evidence),
        }

    def _emit(self, event: str, **detail: Any) -> None:
        if self.event_sink is not None:
            record = {
                "event": event,
                "run_id": self.run_id,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                **detail,
            }
            with self._event_lock:
                self.event_sink(record)

    def _validate_draft(
        self, template: SemanticTemplate, draft: TemplateAgentOutput
    ) -> None:
        if draft.defect_name != template.defect_name:
            raise SemanticScanError("agent changed the registered defect name")
        seen: set[str] = set()
        for finding in draft.findings:
            if finding.finding_id in seen:
                raise SemanticScanError("agent returned duplicate finding IDs")
            seen.add(finding.finding_id)
            if finding.proposed_d_class not in template.d_class.allowed:
                raise SemanticScanError("agent selected a D class outside the template policy")
            self.ledger.require(finding.evidence_ids)
            known = set(finding.evidence_ids)
            for step in finding.mechanism_chain:
                if not set(step.evidence_ids) <= known:
                    raise SemanticScanError("mechanism chain references uncited evidence")
            for alternative in finding.alternatives_checked:
                if not set(alternative.evidence_ids) <= known:
                    raise SemanticScanError("alternative references uncited evidence")
            for residual in finding.residual_hypotheses:
                if not set(residual.evidence_ids) <= known:
                    raise SemanticScanError("residual hypothesis references uncited evidence")
            faults = {item.fault_type for item in finding.available_fault_types}
            if faults and not faults <= set(template.fault_types):
                raise SemanticScanError("agent proposed an unregistered fault type")
            if (
                finding.fault_injection_target is not None
                and finding.fault_injection_target.resource_kind
                not in template.fault_target_kinds
            ):
                raise SemanticScanError("agent proposed an unsupported fault target kind")

    def _validate_verification(
        self,
        finding: TemplateFinding,
        decision: VerificationDecision,
    ) -> None:
        if decision.finding_id != finding.finding_id:
            raise SemanticScanError("verifier returned a different finding ID")
        cited = set(finding.evidence_ids)
        self.ledger.require(decision.verified_evidence_ids)
        if not set(decision.invalid_evidence_ids) <= cited:
            raise SemanticScanError("verifier rejected unknown evidence")

    def _final_match(
        self,
        template: SemanticTemplate,
        finding: TemplateFinding,
        decision: VerificationDecision,
        state: ScanState,
    ) -> TemplateMatch:
        all_evidence_ids = list(
            dict.fromkeys([*finding.evidence_ids, *decision.verified_evidence_ids])
        )
        evidence = self.ledger.require(all_evidence_ids)
        actionable_gates = (
            finding.locatable,
            finding.injectable,
            finding.oracle_observable,
            finding.cleanup_available,
            decision.evidence_reproducible,
            decision.target_supported,
            decision.fault_is_discriminating,
            decision.cleanup_supported,
            finding.fault_injection_target is not None,
            bool(finding.available_fault_types),
        )
        question_eligible = (
            all(actionable_gates)
            and decision.verdict != "rejected"
            and finding.proposed_d_class != DClass.UNCLASSIFIED
        )
        if not question_eligible:
            candidate_status = "unactionable_candidate"
        elif decision.verdict == "confirmed" and decision.mechanism_static_support == "strong":
            candidate_status = "confirmed_candidate"
        else:
            candidate_status = "plausible_candidate"
        score = (
            0.20 * bool(finding.mechanism_chain)
            + 0.20 * decision.evidence_reproducible
            + 0.20 * (decision.mechanism_static_support == "strong")
            + 0.10 * (decision.mechanism_static_support == "partial")
            + 0.15 * decision.target_supported
            + 0.15 * decision.fault_is_discriminating
            + 0.10 * decision.cleanup_supported
            + 0.10 * (decision.verdict == "confirmed")
        )
        if not decision.safeguards_excluded:
            score -= 0.10
        score = max(0.0, min(1.0, float(score)))
        residual_hypotheses = [
            *finding.residual_hypotheses,
            *decision.residual_hypotheses,
        ]
        return TemplateMatch(
            template_id=template.template_id,
            finding_id=finding.finding_id,
            defect_name=template.defect_name,
            d_class=finding.proposed_d_class,
            evidence_explanation=finding.evidence_explanation,
            evidence=evidence,
            mechanism_chain=finding.mechanism_chain,
            available_fault_types=finding.available_fault_types,
            fault_injection_target=finding.fault_injection_target,
            confidence=round(score, 4),
            confidence_level="high" if score >= 0.8 else ("medium" if score >= 0.5 else "low"),
            candidate_status=candidate_status,
            verifier_status=decision.verdict,
            question_eligible=question_eligible,
            alternatives_checked=finding.alternatives_checked,
            residual_hypotheses=residual_hypotheses,
            provenance={
                "codegraph_index_sha256": state["graph_manifest"]["index_sha256"],
                "kubernetes_manifest_sha256": state["kubernetes_manifest"][
                    "manifest_sha256"
                ],
                "template_registry_version": self.registry.registry_version,
                "verified_evidence_ids": decision.verified_evidence_ids,
                "confidence_claim": finding.confidence_claim,
                "eligibility_gates": {
                    "locatable": finding.locatable,
                    "injectable": finding.injectable,
                    "oracle_observable": finding.oracle_observable,
                    "cleanup_available": finding.cleanup_available,
                    "evidence_reproducible": decision.evidence_reproducible,
                    "target_supported": decision.target_supported,
                    "fault_is_discriminating": decision.fault_is_discriminating,
                    "cleanup_supported": decision.cleanup_supported,
                },
            },
        )

    def _persist(self, state: Mapping[str, Any], report: SemanticScanReport) -> None:
        prompts = [
            "common_system.md",
            "coordinator.md",
            "verifier.md",
            *(item.prompt_file for item in self.templates),
        ]
        _write_json(self.run_root / "codegraph-manifest.json", state["graph_manifest"])
        _write_json(
            self.run_root / "kubernetes-manifest.json", state["kubernetes_manifest"]
        )
        _write_json(self.run_root / "coordinator-plan.json", state["plan"])
        _write_json(self.run_root / "agent-drafts.json", state.get("drafts", {}))
        _write_json(
            self.run_root / "verification-decisions.json",
            state.get("verifications", {}),
        )
        _write_json(self.run_root / "evidence-ledger.json", self.ledger.catalog())
        _write_json(self.run_root / "tool-traces.json", getattr(self.agents, "tool_traces", []))
        _write_json(self.run_root / "prompt-manifest.json", self.prompts.manifest(prompts))
        _write_json(
            self.run_root / "schemas" / "template-match.schema.json",
            TemplateMatch.model_json_schema(),
        )
        _write_json(
            self.run_root / "schemas" / "semantic-scan-report.schema.json",
            SemanticScanReport.model_json_schema(),
        )
        _write_json(self.run_root / "semantic-scan-report.json", report.model_dump(mode="json"))
