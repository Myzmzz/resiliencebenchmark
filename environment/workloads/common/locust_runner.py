#!/usr/bin/env python3
"""Run a fixed Locust profile and evaluate its entry SLO from CSV evidence."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def int_env(name: str, minimum: int = 1) -> int:
    try:
        value = int(require_env(name))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def float_env(name: str, minimum: float = 0.0) -> float:
    try:
        value = float(require_env(name))
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def locust_argv() -> list[str]:
    return [
        "locust",
        "--locustfile",
        require_env("RESBENCH_LOCUSTFILE"),
        "--headless",
        "--users",
        str(int_env("RESBENCH_USERS")),
        "--spawn-rate",
        str(float_env("RESBENCH_SPAWN_RATE", 0.1)),
        "--run-time",
        f"{int_env('RESBENCH_DURATION_SECONDS', 60)}s",
        "--host",
        require_env("RESBENCH_TARGET_URL"),
        "--csv",
        require_env("RESBENCH_CSV_PREFIX"),
        "--csv-full-history",
        "--only-summary",
        "--exit-code-on-error",
        "0",
    ]


def aggregate_row(path: Path) -> dict[str, str]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        if row.get("Name") == "Aggregated":
            return row
    raise ValueError("Locust stats CSV is missing the Aggregated row")


def number(row: dict[str, str], key: str) -> float:
    try:
        return float(row.get(key, "0") or 0)
    except ValueError as exc:
        raise ValueError(f"Locust aggregate field {key} is not numeric") from exc


def evaluate(stats_path: Path) -> tuple[dict[str, Any], bool]:
    row = aggregate_row(stats_path)
    requests = int(number(row, "Request Count"))
    failures = int(number(row, "Failure Count"))
    throughput = number(row, "Requests/s")
    p95 = int(number(row, "95%"))
    success_rate = (requests - failures) / requests if requests else 0.0
    error_rate = failures / requests if requests else 1.0
    minimum_success = float_env("RESBENCH_MINIMUM_SUCCESS_RATE")
    maximum_error = float_env("RESBENCH_MAXIMUM_ERROR_RATE")
    maximum_p95 = int_env("RESBENCH_MAXIMUM_P95_LATENCY_MS")
    minimum_samples = int_env("RESBENCH_MINIMUM_SAMPLES", 20)
    baseline_raw = os.environ.get("RESBENCH_BASELINE_THROUGHPUT_RPS", "").strip()
    throughput_ratio = float_env("RESBENCH_MINIMUM_THROUGHPUT_RATIO")
    baseline_throughput = float(baseline_raw) if baseline_raw else None
    minimum_throughput = baseline_throughput * throughput_ratio if baseline_throughput is not None else None
    checks = {
        "minimumSamples": requests >= minimum_samples,
        "minimumSuccessRate": success_rate >= minimum_success,
        "maximumErrorRate": error_rate <= maximum_error,
        "maximumP95LatencyMs": p95 <= maximum_p95,
        "minimumThroughput": minimum_throughput is None or throughput >= minimum_throughput,
    }
    summary = {
        "schemaVersion": "resiliencebenchmark.locust_entry_slo/v1",
        "application": require_env("RESBENCH_APPLICATION"),
        "runId": require_env("RESBENCH_RUN_ID"),
        "randomSeed": int_env("RESBENCH_RANDOM_SEED"),
        "trafficMix": json.loads(require_env("RESBENCH_TRAFFIC_MIX_JSON")),
        "requests": requests,
        "failures": failures,
        "successRate": success_rate,
        "errorRate": error_rate,
        "p95LatencyMs": p95,
        "throughputRps": throughput,
        "baselineThroughputRps": baseline_throughput,
        "minimumThroughputRps": minimum_throughput,
        "checks": checks,
        "qualified": all(checks.values()),
        "calibrationRequired": baseline_throughput is None,
    }
    return summary, bool(summary["qualified"])


def main() -> int:
    try:
        prefix = Path(require_env("RESBENCH_CSV_PREFIX"))
        prefix.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(locust_argv(), check=False)
        summary, qualified = evaluate(Path(str(prefix) + "_stats.csv"))
        summary["locustExitCode"] = int(completed.returncode)
        summary_path = Path(require_env("RESBENCH_SUMMARY_PATH"))
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"qualified": qualified, "summaryPath": str(summary_path)}, sort_keys=True))
        return 0 if qualified else 3
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"locust_runner: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
