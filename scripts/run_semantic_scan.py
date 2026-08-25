#!/usr/bin/env python3
"""Run the CodeGraph and Kubernetes semantic resilience scan locally."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from resilience_agent.semantic_scan import (
    SemanticScanWorkflow,
    load_semantic_scan_config,
)
from resilience_agent.semantic_scan.registry import load_template_registry

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "resilience_agent/config/semantic-scan.otel-demo.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate paths, prompts and the twelve-template registry without calling a model.",
    )
    parser.add_argument("--run-id")
    parser.add_argument("--resume", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.run_id and not re.fullmatch(r"^semantic-[a-z0-9-]{8,80}$", args.run_id):
            raise ValueError("--run-id must be a safe semantic-* identifier")
        if args.resume and not args.run_id:
            raise ValueError("--resume requires --run-id")
        config = load_semantic_scan_config(args.config)
        registry = load_template_registry(config.templates_path)
        if args.validate_only:
            report = {
                "status": "validated",
                "codebase": str(config.codebase.path),
                "kubernetes_sources": [
                    str(item.path) for item in config.kubernetes.sources
                ],
                "template_registry_version": registry.registry_version,
                "template_ids": config.active_template_ids,
                "prompts_root": str(config.prompts_root),
            }
        else:
            result = SemanticScanWorkflow(
                config,
                run_id=args.run_id,
                resume=args.resume,
                event_sink=lambda event: print(
                    json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True
                ),
            ).run()
            report = {
                "status": "completed",
                "run_id": result.run_id,
                "output": str(config.output_dir / result.run_id),
                "matches": [item.template_id for item in result.matches],
                "question_eligible_count": result.question_eligible_count,
                "coverage": {
                    item.template_id: item.status for item in result.coverage
                },
            }
    except Exception as exc:  # noqa: BLE001 - CLI emits bounded structured errors.
        report = {
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc)[:2000],
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
