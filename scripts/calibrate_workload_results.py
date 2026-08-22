#!/usr/bin/env python3
"""Freeze a deterministic workload throughput baseline from repeated healthy runs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
from typing import Any


class CalibrationError(ValueError):
    """Raised when repeated workload evidence is not calibration-eligible."""


def load_summary(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise CalibrationError(f"{path.name} must contain a JSON object")
    return data, hashlib.sha256(raw).hexdigest()


def normalized_mix(summary: dict[str, Any]) -> str:
    return json.dumps(summary.get("trafficMix"), sort_keys=True, separators=(",", ":"))


def calibrate(
    paths: list[Path],
    *,
    application: str,
    maximum_throughput_spread_ratio: float = 0.10,
    minimum_throughput_ratio: float = 0.95,
) -> dict[str, Any]:
    if len(paths) < 2:
        raise CalibrationError("at least two independent summaries are required")
    if not 0 <= maximum_throughput_spread_ratio <= 1:
        raise CalibrationError("maximum throughput spread ratio must be between 0 and 1")
    if not 0 < minimum_throughput_ratio <= 1:
        raise CalibrationError("minimum throughput ratio must be between 0 and 1")

    loaded = [load_summary(path) for path in paths]
    summaries = [item[0] for item in loaded]
    seeds = {item.get("randomSeed") for item in summaries}
    mixes = {normalized_mix(item) for item in summaries}
    run_ids = [str(item.get("runId") or "") for item in summaries]
    if any(item.get("application") != application for item in summaries):
        raise CalibrationError("all summaries must belong to the selected application")
    if len(seeds) != 1 or None in seeds:
        raise CalibrationError("all summaries must use one resolved random seed")
    if len(mixes) != 1 or "null" in mixes:
        raise CalibrationError("all summaries must use one traffic mix")
    if len(set(run_ids)) != len(run_ids) or any(not value for value in run_ids):
        raise CalibrationError("run ids must be present and unique")

    throughputs: list[float] = []
    p95_values: list[int] = []
    sources: list[dict[str, Any]] = []
    for path, (summary, digest) in zip(paths, loaded):
        window = summary.get("measurementWindow")
        checks = summary.get("checks")
        if not isinstance(window, dict) or window.get("calibrationWindowEligible") is not True:
            raise CalibrationError(f"{path.name} is not a full calibration window")
        if summary.get("qualified") is not True or not isinstance(checks, dict) or not all(checks.values()):
            raise CalibrationError(f"{path.name} did not satisfy its entry SLO")
        throughput = float(summary.get("throughputRps") or 0)
        if throughput <= 0:
            raise CalibrationError(f"{path.name} has no positive throughput")
        p95 = int(summary.get("p95LatencyMs") or 0)
        throughputs.append(throughput)
        p95_values.append(p95)
        sources.append(
            {
                "runId": summary["runId"],
                "summarySha256": digest,
                "throughputRps": throughput,
                "p95LatencyMs": p95,
                "successRate": float(summary.get("successRate") or 0),
                "errorRate": float(summary.get("errorRate") or 0),
                "measurementWindowSeconds": int(window["measurementWindowSeconds"]),
            }
        )

    baseline = float(statistics.median(throughputs))
    spread = (max(throughputs) - min(throughputs)) / baseline
    if spread > maximum_throughput_spread_ratio:
        raise CalibrationError(
            f"throughput spread {spread:.6f} exceeds maximum {maximum_throughput_spread_ratio:.6f}"
        )
    return {
        "schemaVersion": "resiliencebenchmark.workload_calibration/v1",
        "application": application,
        "status": "qualified",
        "randomSeed": next(iter(seeds)),
        "trafficMix": summaries[0]["trafficMix"],
        "runCount": len(summaries),
        "baselineMethod": "median-of-independent-healthy-runs",
        "baselineThroughputRps": baseline,
        "minimumThroughputRatio": minimum_throughput_ratio,
        "minimumThroughputRps": baseline * minimum_throughput_ratio,
        "throughputSpreadRatio": spread,
        "maximumThroughputSpreadRatio": maximum_throughput_spread_ratio,
        "medianP95LatencyMs": float(statistics.median(p95_values)),
        "sourceRuns": sources,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--application", required=True)
    parser.add_argument("--summary", type=Path, action="append", required=True)
    parser.add_argument("--maximum-throughput-spread-ratio", type=float, default=0.10)
    parser.add_argument("--minimum-throughput-ratio", type=float, default=0.95)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = calibrate(
            args.summary,
            application=args.application,
            maximum_throughput_spread_ratio=args.maximum_throughput_spread_ratio,
            minimum_throughput_ratio=args.minimum_throughput_ratio,
        )
        rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        sys.stdout.write(rendered)
        return 0
    except (OSError, json.JSONDecodeError, CalibrationError, ValueError) as exc:
        print(f"calibrate_workload_results: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
