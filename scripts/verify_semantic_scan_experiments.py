#!/usr/bin/env python3
"""Verify one full OTel scan plus RD-14 positive and negative controls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from resilience_agent.semantic_scan.contracts import SemanticScanReport
from resilience_agent.semantic_scan.registry import ACTIVE_TEMPLATE_IDS

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = REPO_ROOT / "artifacts/semantic-scan"


def _load(root: Path, run_id: str) -> SemanticScanReport:
    path = root / run_id / "semantic-scan-report.json"
    if not path.is_file():
        raise ValueError(f"semantic scan report is missing: {path}")
    return SemanticScanReport.model_validate_json(path.read_text(encoding="utf-8"))


def _require_artifacts(root: Path, run_id: str) -> None:
    required = (
        "checkpoint-state.json",
        "codegraph-manifest.json",
        "kubernetes-manifest.json",
        "agent-drafts.json",
        "verification-decisions.json",
        "evidence-ledger.json",
        "prompt-manifest.json",
        "semantic-scan-report.json",
        "schemas/template-match.schema.json",
        "schemas/semantic-scan-report.schema.json",
    )
    missing = [item for item in required if not (root / run_id / item).is_file()]
    if missing:
        raise ValueError(f"{run_id} is missing artifacts: {', '.join(missing)}")


def verify(
    root: Path,
    *,
    otel_run: str,
    positive_run: str,
    negative_run: str,
) -> dict[str, Any]:
    for run_id in (otel_run, positive_run, negative_run):
        _require_artifacts(root, run_id)
    otel = _load(root, otel_run)
    positive = _load(root, positive_run)
    negative = _load(root, negative_run)
    coverage = {item.template_id: item.status for item in otel.coverage}
    if set(coverage) != set(ACTIVE_TEMPLATE_IDS):
        raise ValueError("OTel run did not cover all twelve active templates")
    if any(status == "scan_failed" for status in coverage.values()):
        raise ValueError("OTel run contains failed template agents")
    if positive.question_eligible_count != 1 or len(positive.matches) != 1:
        raise ValueError("positive control did not produce one eligible match")
    match = positive.matches[0]
    if match.template_id != "RD-14" or match.confidence < 0.85:
        raise ValueError("positive control did not confirm high-confidence RD-14")
    if negative.matches or negative.question_eligible_count != 0:
        raise ValueError("negative control produced a false-positive match")
    if negative.coverage[0].status not in {"not_found", "not_matched"}:
        raise ValueError("negative control did not finish as not_matched")
    required_output = {
        "defect_name": bool(match.defect_name),
        "evidence_explanation": bool(match.evidence_explanation),
        "mechanism_chain": bool(match.mechanism_chain),
        "available_fault_types": bool(match.available_fault_types),
        "fault_injection_target": bool(
            match.fault_injection_target and match.fault_injection_target.component
        ),
    }
    if not all(required_output.values()):
        raise ValueError("positive output is missing required fixed fields")
    return {
        "schema_version": "semantic-scan-local-experiment-summary.v1",
        "status": "passed",
        "runs": {
            "otel_demo_full": otel_run,
            "rd14_positive": positive_run,
            "rd14_negative": negative_run,
        },
        "otel_demo": {
            "templates_covered": len(coverage),
            "coverage": coverage,
            "question_eligible_count": otel.question_eligible_count,
            "codegraph": {
                "files": otel.codegraph.file_count,
                "nodes": otel.codegraph.node_count,
                "edges": otel.codegraph.edge_count,
            },
            "kubernetes_resources": otel.kubernetes.resource_count,
        },
        "positive_control": {
            "template_id": match.template_id,
            "confidence": match.confidence,
            "d_class": match.d_class.value,
            "fault_types": [item.fault_type for item in match.available_fault_types],
            "target": (
                match.fault_injection_target.model_dump(mode="json")
                if match.fault_injection_target
                else None
            ),
            "fixed_output_fields": required_output,
        },
        "negative_control": {
            "status": negative.coverage[0].status,
            "false_positive_count": 0,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--otel-run", required=True)
    parser.add_argument("--positive-run", required=True)
    parser.add_argument("--negative-run", required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = verify(
            args.root.expanduser().resolve(),
            otel_run=args.otel_run,
            positive_run=args.positive_run,
            negative_run=args.negative_run,
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 2
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
