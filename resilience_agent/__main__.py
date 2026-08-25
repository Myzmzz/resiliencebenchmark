"""CLI for our internal resilience analysis and Episode-design Agent."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .common import load_document, validate_document, write_json
from .agent import RUN_SCHEMA, ResilienceAnalysisAgent, unique_limitations
from .defect_identification import identify_defects
from .episode_design import design_episodes
from .model_client import ModelClientError, ResponsesModelClient, load_model_config
from .pipeline import (
    CANDIDATE_SCHEMA,
    EPISODE_SCHEMA,
    REPO_ROOT,
    TEMPLATE_ROOT,
    load_context,
    run_pipeline,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Identify resilience candidates and design complete validation Episodes"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    identify = sub.add_parser("identify", help="run our defect-identification capability")
    identify.add_argument("--project", type=Path, required=True)
    identify.add_argument("--context", type=Path)
    identify.add_argument("--catalog", type=Path, default=REPO_ROOT / "tasks/catalog/resilience-defect-classes.v0.1.yaml")
    identify.add_argument("--rules", type=Path, default=TEMPLATE_ROOT / "defect-matchers.v0.1.yaml")
    identify.add_argument("--output", type=Path)

    episode = sub.add_parser("design-episodes", help="design complete Episodes from candidates")
    episode.add_argument("--candidates", type=Path, required=True)
    episode.add_argument(
        "--templates",
        type=Path,
        default=TEMPLATE_ROOT / "episode-design-templates.v0.1.yaml",
    )
    episode.add_argument("--min-confidence", type=float, default=0.65)
    episode.add_argument("--output", type=Path)

    run = sub.add_parser("run", help="run both internal capabilities with a validated handoff")
    run.add_argument("--project", type=Path, required=True)
    run.add_argument("--context", type=Path)
    run.add_argument("--output-dir", type=Path)
    run.add_argument("--min-confidence", type=float, default=0.65)
    run.add_argument(
        "--evidence-root",
        action="append",
        default=[],
        metavar="ALIAS=PATH",
        help="add an explicitly authorized read-only evidence root",
    )
    run.add_argument(
        "--reasoning-mode",
        choices=["model", "deterministic"],
        default="model",
        help="model is the production path; deterministic is an explicit offline fallback",
    )
    run.add_argument(
        "--model-config",
        type=Path,
        default=REPO_ROOT / "resilience_agent/config/model.yaml",
    )
    return parser


def _emit(value: object, output: Path | None) -> None:
    if output:
        write_json(output, value)
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "identify":
        result = identify_defects(args.project, args.catalog, args.rules, load_context(args.context))
        validate_document(result, CANDIDATE_SCHEMA)
        _emit(result, args.output)
        return 0
    if args.command == "design-episodes":
        candidates = load_document(args.candidates)
        validate_document(candidates, CANDIDATE_SCHEMA)
        result = design_episodes(
            candidates, args.templates, min_confidence=args.min_confidence
        )
        validate_document(result, EPISODE_SCHEMA)
        _emit(result, args.output)
        return 0
    context = load_context(args.context)
    evidence_roots: dict[str, Path] = {}
    for specification in args.evidence_root:
        if "=" not in specification:
            print("ERROR: --evidence-root must use ALIAS=PATH", file=sys.stderr)
            return 2
        alias, raw_path = specification.split("=", 1)
        evidence_roots[alias] = Path(raw_path).expanduser().resolve()
    if args.reasoning_mode == "model":
        try:
            config = load_model_config(args.model_config)
            result = ResilienceAnalysisAgent(ResponsesModelClient(config)).run(
                args.project,
                system_context=context,
                evidence_roots=evidence_roots,
                output_dir=args.output_dir,
                min_confidence=args.min_confidence,
            )
        except (ModelClientError, ValueError, RuntimeError) as exc:
            print(f"ERROR: model-driven Agent run failed: {exc}", file=sys.stderr)
            return 2
        candidates = result.candidates
        episodes = result.episode_designs
        manifest = result.run_manifest
    else:
        started = datetime.now(timezone.utc)
        candidates, episodes = run_pipeline(
            args.project,
            system_context=context,
            output_dir=args.output_dir,
            min_confidence=args.min_confidence,
        )
        completed = datetime.now(timezone.utc)
        from .common import stable_id

        manifest = {
            "schema_version": "resilience-agent-run.v0.1",
            "run_id": stable_id("RARUN", [args.project.resolve().as_posix(), started.isoformat()]),
            "status": "completed",
            "started_at": started.isoformat(),
            "completed_at": completed.isoformat(),
            "reasoning_mode": "deterministic",
            "model": None,
            "project_root": args.project.resolve().as_posix(),
            "evidence_roots": {
                alias: path.as_posix() for alias, path in evidence_roots.items()
            },
            "stages": [
                {"stage": "deterministic_evidence_collection", "status": "completed"},
                {"stage": "deterministic_episode_design", "status": "completed"},
            ],
            "artifacts": {
                "candidate_defects": "candidate-defects.json",
                "episode_designs": "episode-designs.json",
                "agent_run": "agent-run.json",
                "model_defect_assessment": None,
                "model_episode_review": None,
            },
            "limitations": unique_limitations(candidates, episodes),
        }
        validate_document(manifest, RUN_SCHEMA)
        if args.output_dir:
            write_json(args.output_dir / "agent-run.json", manifest)
    if args.output_dir:
        print(
            json.dumps(
                {
                    "analysis_id": candidates["analysis_id"],
                    "candidate_count": len(candidates["candidates"]),
                    "design_set_id": episodes["design_set_id"],
                    "episode_count": len(episodes["episodes"]),
                    "reasoning_mode": manifest["reasoning_mode"],
                    "model": manifest["model"]["model"] if manifest["model"] else None,
                    "output_dir": args.output_dir.resolve().as_posix(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(
            json.dumps(
                {"candidates": candidates, "episode_designs": episodes},
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
