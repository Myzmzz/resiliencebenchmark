#!/usr/bin/env python3
"""Run the backend-only 4-Harness x 2-model x 7-case Stage-2 matrix."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from stage2_service.matrix import (
    DEFAULT_MATRIX_PROMPT,
    build_matrix_requests,
    load_completed_matrix_results,
    load_qualification_matrix,
    run_matrix,
)
from stage2_service.runtime_factory import Stage2RuntimeConfig, Stage2System


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--execute", action="store_true")
    value.add_argument("--qualification-file", type=Path, required=True)
    value.add_argument(
        "--matrix-id",
        default="matrix-otel-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S"),
    )
    value.add_argument("--prompt", default=DEFAULT_MATRIX_PROMPT)
    value.add_argument("--resume-from")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.execute:
        raise SystemExit("--execute is required; this command has no simulated mode")
    config = Stage2RuntimeConfig.from_env()
    system = Stage2System(config)
    qualifications = load_qualification_matrix(
        args.qualification_file.expanduser().resolve()
    )
    requests = build_matrix_requests(
        matrix_id=args.matrix_id,
        repo_root=config.repo_root,
        prompt=args.prompt,
        qualification_matrix=qualifications,
    )
    prior_results = (
        load_completed_matrix_results(config.artifact_root, args.resume_from)
        if args.resume_from
        else ()
    )
    report = run_matrix(
        matrix_id=args.matrix_id,
        artifact_root=config.artifact_root,
        requests=requests,
        run_campaign=lambda request, observer: system.run(
            request, event_observer=observer
        ),
        preflight=system.preflight(),
        prior_results=prior_results,
    )
    print(
        json.dumps(
            {
                "matrix_id": report["matrix_id"],
                "completed_trial_count": report["completed_trial_count"],
                "expected_trial_count": report["expected_trial_count"],
                "report": str(config.artifact_root / args.matrix_id / "report.md"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["completed_trial_count"] == report["expected_trial_count"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
