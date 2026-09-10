#!/usr/bin/env python3
"""Run the real BladeAI WP8 task-mode full-chain qualification.

The command is inert unless ``--execute`` is supplied.  It never deploys or
deletes the canary Pod and never publishes capabilities automatically.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from stage2_service.bladeai_qualification_runner import BladeAIQualificationRunner
from stage2_service.contracts import STAGE2_BLADEAI_DEFAULT_MODEL
from stage2_service.runtime_factory import Stage2RuntimeConfig, Stage2System


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually launch the BladeAI WP8 qualification. Without this flag no action is taken.",
    )
    parser.add_argument(
        "--model",
        default=STAGE2_BLADEAI_DEFAULT_MODEL,
        help="Gateway model alias for BladeAI (default: gpt-5.6-sol).",
    )
    parser.add_argument(
        "--canary-pod",
        required=True,
        help=(
            "Existing Ready Pod in otel-demo labelled "
            "resiliencebenchmark.io/qualification=bladeai-wp8."
        ),
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="New or empty directory for the WP8 qualification record.",
    )
    parser.add_argument(
        "--protected-root",
        type=Path,
        default=None,
        help="Expected Stage-2 private root. If supplied, it must match STAGE2_PRIVATE_ROOT.",
    )
    parser.add_argument(
        "--namespace",
        default="otel-demo",
        choices=["otel-demo"],
        help="Application namespace. WP8 qualification is currently scoped to otel-demo.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.execute:
        print(json.dumps({
            "status": "rejected",
            "reason": "--execute is required; no BladeAI qualification action was taken",
        }, ensure_ascii=False, sort_keys=True))
        return 2
    try:
        config = Stage2RuntimeConfig.from_env()
        _validate_protected_root(config.private_root, args.protected_root)
        result = BladeAIQualificationRunner(
            Stage2System(config),
            namespace=args.namespace,
        ).run(
            model=args.model,
            canary_pod=args.canary_pod,
            output_dir=args.output_dir,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({
            "status": "failed",
            "error_type": type(exc).__name__,
        }, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps({
        "status": result.record.get("status"),
        "passed": result.record.get("passed") is True,
        "trial_id": result.trial_id,
        "campaign_id": result.campaign_id,
        "output": str(result.output),
        "artifact_refs": list(result.artifact_refs),
        "failure_reasons": result.record.get("failure_reasons") or [],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if result.record.get("passed") is True else 1


def _validate_protected_root(config_private_root: Path, supplied: Path | None) -> None:
    if supplied is None:
        return
    root = supplied.resolve()
    if root != config_private_root.resolve():
        raise ValueError(
            f"--protected-root {root} does not match STAGE2_PRIVATE_ROOT {config_private_root.resolve()}"
        )
    if root == Path("/") or root == Path.home().resolve():
        raise ValueError("protected root must not be filesystem root or the user home")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
