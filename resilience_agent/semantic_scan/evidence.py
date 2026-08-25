"""Deterministic evidence ledger shared by analysis and verification agents."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterable
from typing import Any

from .contracts import EvidenceKind, EvidenceRef


def _sha(value: Any) -> str:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode()).hexdigest()


class EvidenceLedger:
    def __init__(self):
        self._items: dict[str, EvidenceRef] = {}
        self._lock = threading.RLock()

    def add_codegraph(self, payload: Any, *, query: str) -> list[EvidenceRef]:
        candidates: list[EvidenceRef] = []
        if isinstance(payload, list):
            for item in payload:
                node = item.get("node") if isinstance(item, dict) else None
                if isinstance(node, dict):
                    candidates.append(self._node(node, query))
        elif isinstance(payload, dict):
            for node in payload.get("nodes", []):
                if isinstance(node, dict):
                    candidates.append(self._node(node, query))
            for key in ("callers", "callees"):
                for node in payload.get(key, []):
                    if isinstance(node, dict):
                        candidates.append(self._node(node, query))
            for edge in payload.get("edges", []):
                if isinstance(edge, dict):
                    candidates.append(self._edge(edge, query))
            for block in payload.get("codeBlocks", []):
                if isinstance(block, dict):
                    candidates.append(self._code_block(block, query))
        return self._store(candidates)

    def add_kubernetes(self, payload: dict[str, Any]) -> list[EvidenceRef]:
        candidates = []
        for item in payload.get("resources", []):
            if not isinstance(item, dict):
                continue
            resource = item.get("resource") if isinstance(item.get("resource"), dict) else {}
            statement = (
                f"Kubernetes {resource.get('kind')} {resource.get('namespace') or '<default>'}/"
                f"{resource.get('name')} from {item.get('source_alias')}"
            )
            material = {"path": item.get("path"), "resource": resource}
            evidence_kind = (
                EvidenceKind.KUBERNETES_LIVE
                if item.get("source_alias") == "live-apiserver"
                or str(item.get("path") or "").startswith("kubernetes://")
                else EvidenceKind.KUBERNETES_MANIFEST
            )
            candidates.append(
                self._make(
                    kind=evidence_kind,
                    statement=statement,
                    path=str(item.get("path") or ""),
                    source_hash=_sha(material),
                    symbol=f"{resource.get('kind')}/{resource.get('name')}",
                )
            )
        return self._store(candidates)

    def get(self, evidence_id: str) -> EvidenceRef | None:
        with self._lock:
            return self._items.get(evidence_id)

    def resolve_id(self, evidence_id: str) -> str:
        with self._lock:
            if evidence_id in self._items:
                return evidence_id
            if evidence_id.startswith("EV-") and len(evidence_id) >= 12:
                matches = [item for item in self._items if item.startswith(evidence_id)]
                if len(matches) == 1:
                    return matches[0]
        raise ValueError(f"unknown evidence ID: {evidence_id}")

    def require(self, evidence_ids: Iterable[str]) -> list[EvidenceRef]:
        with self._lock:
            output = []
            for evidence_id in evidence_ids:
                resolved_id = self.resolve_id(evidence_id)
                item = self._items.get(resolved_id)
                if item is None:
                    raise ValueError(f"unknown evidence ID: {evidence_id}")
                output.append(item)
            return output

    def catalog(self, evidence_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = (
                self.require(evidence_ids)
                if evidence_ids is not None
                else [self._items[key] for key in sorted(self._items)]
            )
        return [item.model_dump(mode="json") for item in items]

    def load(self, values: Iterable[dict[str, Any]]) -> None:
        self._store([EvidenceRef.model_validate(value) for value in values])

    def _store(self, candidates: list[EvidenceRef]) -> list[EvidenceRef]:
        with self._lock:
            for item in candidates:
                self._items[item.evidence_id] = item
        return candidates

    def _node(self, node: dict[str, Any], query: str) -> EvidenceRef:
        path = node.get("filePath")
        start = node.get("startLine")
        end = node.get("endLine")
        return self._make(
            kind=EvidenceKind.CODEGRAPH_NODE,
            statement=(
                f"CodeGraph {node.get('kind')} {node.get('qualifiedName') or node.get('name')} "
                f"selected by query: {query[:160]}"
            ),
            path=str(path) if path else None,
            start_line=int(start) if start else None,
            end_line=int(end) if end else None,
            symbol=str(node.get("qualifiedName") or node.get("name") or ""),
            source_hash=_sha(node),
        )

    def _edge(self, edge: dict[str, Any], query: str) -> EvidenceRef:
        return self._make(
            kind=EvidenceKind.CODEGRAPH_EDGE,
            statement=(
                f"CodeGraph edge {edge.get('source')} -[{edge.get('kind')}]-> "
                f"{edge.get('target')} selected by query: {query[:120]}"
            ),
            start_line=int(edge["line"]) if edge.get("line") else None,
            end_line=int(edge["line"]) if edge.get("line") else None,
            relation=str(edge.get("kind") or "relation"),
            source_hash=_sha(edge),
        )

    def _code_block(self, block: dict[str, Any], query: str) -> EvidenceRef:
        return self._make(
            kind=EvidenceKind.CODEGRAPH_CONTEXT,
            statement=(
                f"CodeGraph context for {block.get('nodeKind')} {block.get('nodeName')} "
                f"selected by query: {query[:140]}"
            ),
            path=str(block.get("filePath") or ""),
            start_line=int(block["startLine"]) if block.get("startLine") else None,
            end_line=int(block["endLine"]) if block.get("endLine") else None,
            symbol=str(block.get("nodeName") or ""),
            source_hash=_sha(block),
        )

    @staticmethod
    def _make(
        *,
        kind: EvidenceKind,
        statement: str,
        source_hash: str,
        path: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        symbol: str | None = None,
        relation: str | None = None,
    ) -> EvidenceRef:
        identity = {
            "kind": kind.value,
            "statement": statement,
            "path": path,
            "start_line": start_line,
            "end_line": end_line,
            "symbol": symbol,
            "relation": relation,
            "source_hash": source_hash,
        }
        evidence_id = "EV-" + _sha(identity)[:12].upper()
        return EvidenceRef(
            evidence_id=evidence_id,
            kind=kind,
            statement=statement,
            path=path,
            start_line=start_line,
            end_line=end_line,
            symbol=symbol,
            relation=relation,
            source_hash=source_hash,
        )
