#!/usr/bin/env python3
"""Verify positive and negative match-to-Episode generation controls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from resilience_agent.semantic_scan.contracts import SemanticScanReport
from resilience_agent.semantic_scan.episode_contracts import (
    EpisodeGenerationReport,
    InternalEpisode,
    PublicEpisodeTask,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--positive-scan", type=Path, required=True)
    parser.add_argument("--positive-episodes", type=Path, required=True)
    parser.add_argument("--negative-scan", type=Path, required=True)
    parser.add_argument("--negative-episodes", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT
        / "artifacts/episode-generation/local-episode-experiment-summary.json",
    )
    return parser


def _load_scan(path: Path) -> SemanticScanReport:
    return SemanticScanReport.model_validate_json(
        path.expanduser().resolve().read_text(encoding="utf-8")
    )


def _load_generation(path: Path) -> EpisodeGenerationReport:
    return EpisodeGenerationReport.model_validate_json(
        (path.expanduser().resolve() / "episode-generation-report.json").read_text(
            encoding="utf-8"
        )
    )


def verify(args: argparse.Namespace) -> dict:
    positive_scan = _load_scan(args.positive_scan)
    negative_scan = _load_scan(args.negative_scan)
    positive_root = args.positive_episodes.expanduser().resolve()
    negative_root = args.negative_episodes.expanduser().resolve()
    positive = _load_generation(positive_root)
    negative = _load_generation(negative_root)
    if positive.source_match_count != len(positive_scan.matches):
        raise ValueError("positive source match count differs from its scan")
    if positive.generated_count != 1 or not positive.one_to_one_verified:
        raise ValueError("positive control did not generate exactly one Episode")
    if negative.source_match_count != len(negative_scan.matches):
        raise ValueError("negative source match count differs from its scan")
    if negative.generated_count != 0 or not negative.one_to_one_verified:
        raise ValueError("negative control generated an Episode")
    episode_id = positive.items[0].episode_id
    episode_root = positive_root / episode_id
    internal = InternalEpisode.model_validate_json(
        (episode_root / "episode-internal.json").read_text(encoding="utf-8")
    )
    public = PublicEpisodeTask.model_validate_json(
        (episode_root / "episode-public.json").read_text(encoding="utf-8")
    )
    private = json.dumps(internal.model_dump(mode="json"), ensure_ascii=False)
    visible = json.dumps(public.model_dump(mode="json"), ensure_ascii=False)
    leaked = [
        value
        for value in (
            internal.defect_basis.defect_name,
            internal.runtime_binding.pod_uid,
            internal.main_fault.command_template,
            internal.main_fault.cleanup_command,
        )
        if value in visible
    ]
    if leaked:
        raise ValueError("public Episode leaked private material")
    if internal.main_fault.duration_seconds < 600:
        raise ValueError("main fault window is below 600 seconds")
    if "blade create" not in internal.main_fault.command_template:
        raise ValueError("main fault command is not executable through ChaosBlade")
    if "blade destroy" not in internal.main_fault.cleanup_command:
        raise ValueError("Episode has no matching ChaosBlade cleanup command")
    if len(internal.oracle.gates) != 6:
        raise ValueError("Episode does not contain all six Oracle gates")
    return {
        "schema_version": "episode-generation-local-experiment-summary.v1",
        "status": "passed",
        "positive": {
            "source_matches": positive.source_match_count,
            "generated_episodes": positive.generated_count,
            "episode_id": episode_id,
            "fault_type": internal.main_fault.fault_type,
            "fault_semantics": internal.main_fault.fault_semantics,
            "duration_seconds": internal.main_fault.duration_seconds,
            "oracle_gates": [item.gate_id for item in internal.oracle.gates],
            "private_bytes": len(private.encode()),
            "public_bytes": len(visible.encode()),
            "private_leak_count": 0,
        },
        "negative": {
            "source_matches": negative.source_match_count,
            "generated_episodes": negative.generated_count,
        },
        "one_to_one_verified": True,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = verify(args)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 2
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
