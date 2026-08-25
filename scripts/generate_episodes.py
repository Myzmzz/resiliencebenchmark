#!/usr/bin/env python3
"""Compile one internal and one public Episode for every verified match."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from resilience_agent.semantic_scan.contracts import SemanticScanReport
from resilience_agent.semantic_scan.episode_generator import (
    EpisodeCompiler,
    load_episode_generation_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "resilience_agent/config/episode-generation.rd14-positive.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--scan-report", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_episode_generation_config(args.config)
        scan = SemanticScanReport.model_validate_json(
            args.scan_report.expanduser().resolve().read_text(encoding="utf-8")
        )
        report = EpisodeCompiler(config).compile(scan)
        result = {
            "status": "completed",
            "source_scan_run_id": report.source_scan_run_id,
            "source_match_count": report.source_match_count,
            "episode_eligible_count": report.episode_eligible_count,
            "generated_count": report.generated_count,
            "materialized_count": report.materialized_count,
            "one_to_one_verified": report.one_to_one_verified,
            "output_dir": str(config.output_dir),
            "episodes": [item.model_dump(mode="json") for item in report.items],
        }
    except Exception as exc:  # noqa: BLE001 - bounded CLI failure contract.
        result = {
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc)[:2000],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
