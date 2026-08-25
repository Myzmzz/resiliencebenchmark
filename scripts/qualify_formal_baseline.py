#!/usr/bin/env python3
"""Run one formal 600-second OTel Demo baseline with the scoped Controller identity."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from controller.trial_preparation import FormalOtelBaselineMeasurer
from progression.controller import TrialTicket

IMAGE_RE = re.compile(r"^.+:[^/@]+@sha256:[0-9a-f]{64}$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--run-id", default="formal-baseline-preflight")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    kubeconfig = args.kubeconfig.expanduser().resolve()
    if not kubeconfig.is_file() or not IMAGE_RE.fullmatch(args.image):
        print(
            json.dumps(
                {
                    "schema_version": "formal-baseline-qualification.v1",
                    "status": "blocked",
                    "message": "explicit kubeconfig and digest-pinned image are required",
                }
            )
        )
        return 2
    if not re.fullmatch(r"^[a-z0-9][a-z0-9-]{2,62}$", args.run_id):
        print("formal baseline run id is invalid", file=sys.stderr)
        return 2
    if not args.execute:
        report = {
            "schema_version": "formal-baseline-qualification.v1",
            "status": "planned",
            "duration_seconds": 600,
            "measurement_window_seconds": 300,
            "kubeconfig": {"configured": True},
            "image": {"digest_pinned": True},
        }
    else:
        try:
            measured = FormalOtelBaselineMeasurer(
                kubeconfig=kubeconfig,
                workload_image=args.image,
            )(
                TrialTicket(
                    trial_id=args.run_id,
                    run_id=args.run_id,
                    episode_id="EPI-BASELINE-PREFLIGHT",
                    level_id="L1",
                    attempt=1,
                ),
                {"level_id": "L1"},
                {
                    "namespace": "otel-demo",
                    "kind": "Pod",
                    "name": "frontend-preflight",
                    "uid": "preflight-only",
                    "component": "frontend",
                },
            )
            report = {
                "schema_version": "formal-baseline-qualification.v1",
                "status": "qualified" if measured.get("qualified") is True else "failed",
                **dict(measured),
            }
        except (OSError, RuntimeError, ValueError) as exc:
            report = {
                "schema_version": "formal-baseline-qualification.v1",
                "status": "failed",
                "error_type": type(exc).__name__,
                "message": str(exc)[:500],
            }
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if report["status"] in {"planned", "qualified"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
