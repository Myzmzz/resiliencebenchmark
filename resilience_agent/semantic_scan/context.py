"""Context packing with explicit per-agent budgets and isolation."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from .codegraph_driver import CodeGraphDriver
from .config import ContextBudgetConfig
from .contracts import TemplatePlan
from .evidence import EvidenceLedger
from .kubernetes_scanner import KubernetesConfigScanner
from .registry import SemanticTemplate


class ContextBudgetError(RuntimeError):
    pass


def _bounded(value: Any, max_chars: int) -> Any:
    result = deepcopy(value)

    def rendered_size() -> int:
        return len(json.dumps(result, ensure_ascii=False, sort_keys=True))

    def lists(item: Any, path: tuple[Any, ...] = ()):
        output = []
        if isinstance(item, dict):
            for key, child in item.items():
                output.extend(lists(child, (*path, key)))
        elif isinstance(item, list) and len(item) > 1:
            output.append((path, item))
            for index, child in enumerate(item):
                output.extend(lists(child, (*path, index)))
        return output

    def replace(path: tuple[Any, ...], new_value: Any) -> None:
        current = result
        for part in path[:-1]:
            current = current[part]
        current[path[-1]] = new_value

    while rendered_size() > max_chars:
        candidates = lists(result)
        if not candidates:
            break
        path, items = max(
            candidates,
            key=lambda pair: len(
                json.dumps(pair[1], ensure_ascii=False, sort_keys=True)
            ),
        )
        replace(path, items[: max(1, len(items) // 2)])
        if isinstance(result, dict):
            result["context_truncated"] = True
    if rendered_size() <= max_chars:
        return result
    raise ContextBudgetError(f"context cannot be reduced below {max_chars} characters")


class SemanticContextManager:
    def __init__(
        self,
        codegraph: CodeGraphDriver,
        kubernetes: KubernetesConfigScanner,
        ledger: EvidenceLedger,
        budget: ContextBudgetConfig,
    ):
        self.codegraph = codegraph
        self.kubernetes = kubernetes
        self.ledger = ledger
        self.budget = budget

    def coordinator_context(
        self,
        *,
        graph_manifest: dict[str, Any],
        template_index: list[dict[str, Any]],
    ) -> dict[str, Any]:
        resource_index = [
            {
                "source_alias": item["source_alias"],
                "kind": item["resource"].get("kind"),
                "name": item["resource"].get("name"),
                "namespace": item["resource"].get("namespace"),
            }
            for item in self.kubernetes.resources
        ]
        value = {
            "codegraph_manifest": graph_manifest,
            "kubernetes_inventory": {
                "manifest": self.kubernetes.manifest.model_dump(mode="json"),
                "resource_index": resource_index,
            },
            "active_templates": template_index,
            "context_policy": (
                "Planning only. Every template must be covered; no defect verdict is allowed."
            ),
        }
        return _bounded(value, self.budget.coordinator_chars)

    def subagent_context(
        self,
        template: SemanticTemplate,
        plan: TemplatePlan,
    ) -> dict[str, Any]:
        value = {
            "template": template.model_dump(mode="json"),
            "coordinator_plan": plan.model_dump(mode="json"),
            "codegraph_manifest": self.codegraph.manifest.model_dump(mode="json"),
            "kubernetes_scope": self.kubernetes.manifest.model_dump(mode="json"),
            "evidence_catalog": [],
            "agent_driven_tool_policy": {
                "codegraph": (
                    "You must call CodeGraph tools yourself to enumerate entrypoints, "
                    "wrappers, interceptors, DI registrations, callers, callees, and "
                    "counter-evidence. The Controller has not prefetched semantic context."
                ),
                "kubernetes": (
                    "Use Kubernetes tools to fetch the exact resource fields needed for "
                    "the candidate, including command, args, env, probes, resources, "
                    "ConfigMap data, owner refs, and topology."
                ),
            },
            "required_output_rule": (
                "Use only evidence_id values returned by tools during this agent run."
            ),
        }
        return _bounded(value, self.budget.subagent_chars)

    def verifier_context(
        self,
        template: SemanticTemplate,
        draft: dict[str, Any],
    ) -> dict[str, Any]:
        evidence_ids = draft.get("evidence_ids", [])
        value = {
            "template": template.model_dump(mode="json"),
            "candidate": draft,
            "codegraph_manifest": self.codegraph.manifest.model_dump(mode="json"),
            "kubernetes_scope": self.kubernetes.manifest.model_dump(mode="json"),
            "cited_evidence": self.ledger.catalog(evidence_ids),
            "verification_rule": (
                "Attempt to falsify the candidate using fresh bounded CodeGraph and Kubernetes tools."
            ),
        }
        return _bounded(value, self.budget.verifier_chars)
