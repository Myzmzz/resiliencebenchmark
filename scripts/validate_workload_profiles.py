#!/usr/bin/env python3
"""Validate deterministic workload profiles before baseline or fault execution."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any

import yaml


DEFAULT_PROFILE = Path("environment/workloads/deterministic-profiles.yaml")
EXPECTED_APPLICATIONS = {"train-ticket", "sock-shop", "otel-demo"}


class ProfileError(ValueError):
    """A deterministic workload contract is incomplete or unsafe."""


def require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProfileError(f"{field} must be a mapping")
    return value


def positive_int(value: Any, field: str, minimum: int = 1) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ProfileError(f"{field} must be an integer >= {minimum}")
    return value


def positive_number(value: Any, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or float(value) <= 0:
        raise ProfileError(f"{field} must be positive")
    return float(value)


def probability(value: Any, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= float(value) <= 1:
        raise ProfileError(f"{field} must be between 0 and 1")
    return float(value)


def validate_entry_slo(value: Any, field: str) -> None:
    slo = require_mapping(value, field)
    success = probability(slo.get("minimumSuccessRate"), f"{field}.minimumSuccessRate")
    error = probability(slo.get("maximumErrorRate"), f"{field}.maximumErrorRate")
    if success < 0.95:
        raise ProfileError(f"{field}.minimumSuccessRate must be >= 0.95")
    if error > 0.05:
        raise ProfileError(f"{field}.maximumErrorRate must be <= 0.05")
    if abs((1 - success) - error) > 1e-9:
        raise ProfileError(f"{field} success and error objectives must be complementary")
    probability(slo.get("minimumThroughputRatio"), f"{field}.minimumThroughputRatio")
    if float(slo["minimumThroughputRatio"]) < 0.95:
        raise ProfileError(f"{field}.minimumThroughputRatio must be >= 0.95")
    if "p95LatencyMs" in slo:
        positive_int(slo["p95LatencyMs"], f"{field}.p95LatencyMs")
    if "latencyStatistic" in slo and slo["latencyStatistic"] != "p95":
        raise ProfileError(f"{field}.latencyStatistic must be p95")
    if "maxConsecutiveFailures" in slo:
        positive_int(slo["maxConsecutiveFailures"], f"{field}.maxConsecutiveFailures")
    calibrated = slo.get("calibratedHealthyThroughputRps")
    minimum = slo.get("minimumThroughputRps")
    evidence = slo.get("calibrationEvidenceRef")
    if any(value is not None for value in (calibrated, minimum, evidence)):
        baseline = positive_number(calibrated, f"{field}.calibratedHealthyThroughputRps")
        floor = positive_number(minimum, f"{field}.minimumThroughputRps")
        expected = baseline * float(slo["minimumThroughputRatio"])
        if abs(floor - expected) > 1e-9:
            raise ProfileError(f"{field}.minimumThroughputRps must equal calibrated baseline times ratio")
        if not isinstance(evidence, str) or not evidence:
            raise ProfileError(f"{field}.calibrationEvidenceRef is required")


def validate_profile(path: Path = DEFAULT_PROFILE) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    root = require_mapping(data, "profile")
    spec = require_mapping(root.get("spec"), "spec")
    defaults = require_mapping(spec.get("defaults"), "spec.defaults")
    warmup = positive_int(defaults.get("warmupSeconds"), "spec.defaults.warmupSeconds")
    duration = positive_int(defaults.get("durationSeconds"), "spec.defaults.durationSeconds", minimum=60)
    window = positive_int(defaults.get("evaluationWindowSeconds"), "spec.defaults.evaluationWindowSeconds", minimum=60)
    positive_int(defaults.get("minimumSamples"), "spec.defaults.minimumSamples", minimum=20)
    if warmup >= duration:
        raise ProfileError("warmupSeconds must be smaller than durationSeconds")
    if window > duration - warmup:
        raise ProfileError("evaluationWindowSeconds must fit after warmup")
    validate_entry_slo(defaults.get("entrySlo"), "spec.defaults.entrySlo")

    applications = spec.get("applications")
    if not isinstance(applications, list) or not applications:
        raise ProfileError("spec.applications must be a non-empty list")
    ids = [item.get("id") for item in applications if isinstance(item, dict)]
    if set(ids) != EXPECTED_APPLICATIONS or len(ids) != len(EXPECTED_APPLICATIONS):
        raise ProfileError("spec.applications must define train-ticket, sock-shop, and otel-demo exactly once")

    seeds: set[int] = set()
    for index, raw in enumerate(applications):
        app = require_mapping(raw, f"spec.applications[{index}]")
        app_id = str(app.get("id"))
        if app.get("namespace") != app_id:
            raise ProfileError(f"{app_id}.namespace must equal the application id")
        if not isinstance(app.get("entryService"), str) or not app["entryService"]:
            raise ProfileError(f"{app_id}.entryService is required")
        executor = require_mapping(app.get("executor"), f"{app_id}.executor")
        for key in ("kind", "profileRef", "resultArtifact"):
            if not isinstance(executor.get(key), str) or not executor[key]:
                raise ProfileError(f"{app_id}.executor.{key} is required")
        determinism = require_mapping(app.get("determinism"), f"{app_id}.determinism")
        seed = positive_int(determinism.get("randomSeed"), f"{app_id}.determinism.randomSeed")
        if seed in seeds:
            raise ProfileError("application random seeds must be unique")
        seeds.add(seed)
        if determinism.get("seedDerivation") not in {"sha256-seed-run-slot", "sha256-seed-user-iteration"}:
            raise ProfileError(f"{app_id}.determinism.seedDerivation is unsupported")
        load = require_mapping(app.get("load"), f"{app_id}.load")
        model = load.get("model")
        if model == "open":
            positive_number(load.get("targetFlowQps"), f"{app_id}.load.targetFlowQps")
            positive_int(load.get("concurrency"), f"{app_id}.load.concurrency")
        elif model == "closed":
            positive_int(load.get("users"), f"{app_id}.load.users")
            positive_number(load.get("spawnRatePerSecond"), f"{app_id}.load.spawnRatePerSecond")
        else:
            raise ProfileError(f"{app_id}.load.model must be open or closed")
        mix = app.get("trafficMix")
        if not isinstance(mix, list) or not mix:
            raise ProfileError(f"{app_id}.trafficMix must be a non-empty list")
        flows = [item.get("flow") for item in mix if isinstance(item, dict)]
        if len(flows) != len(set(flows)) or any(not isinstance(flow, str) or not flow for flow in flows):
            raise ProfileError(f"{app_id}.trafficMix flow ids must be unique non-empty strings")
        weights = [positive_int(item.get("weightPercent"), f"{app_id}.trafficMix.weightPercent") for item in mix]
        if sum(weights) != 100:
            raise ProfileError(f"{app_id}.trafficMix weights must sum to 100")
        validate_entry_slo(app.get("entrySlo"), f"{app_id}.entrySlo")
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    args = parser.parse_args(argv)
    try:
        data = validate_profile(args.profile)
    except (OSError, yaml.YAMLError, ProfileError) as exc:
        print(f"workload profile validation failed: {exc}", file=sys.stderr)
        return 2
    print(f"deterministic workload profiles valid: {len(data['spec']['applications'])} applications")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
