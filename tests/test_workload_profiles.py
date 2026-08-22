from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from scripts import validate_workload_profiles


PROFILE = Path("environment/workloads/deterministic-profiles.yaml")


def write_profile(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "profiles.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_repository_deterministic_profiles_are_valid():
    data = validate_workload_profiles.validate_profile(PROFILE)

    applications = {item["id"]: item for item in data["spec"]["applications"]}
    assert set(applications) == {"train-ticket", "sock-shop", "otel-demo"}
    assert all(sum(flow["weightPercent"] for flow in app["trafficMix"]) == 100 for app in applications.values())
    assert all(app["entrySlo"]["minimumSuccessRate"] == 0.95 for app in applications.values())
    assert applications["otel-demo"]["entrySlo"]["minimumThroughputRps"] == pytest.approx(
        applications["otel-demo"]["entrySlo"]["calibratedHealthyThroughputRps"] * 0.95
    )


def test_profile_rejects_non_deterministic_or_incomplete_mix(tmp_path):
    data = yaml.safe_load(PROFILE.read_text(encoding="utf-8"))
    broken = deepcopy(data)
    broken["spec"]["applications"][0]["trafficMix"][0]["weightPercent"] = 59

    with pytest.raises(validate_workload_profiles.ProfileError, match="sum to 100"):
        validate_workload_profiles.validate_profile(write_profile(tmp_path, broken))


def test_profile_rejects_success_objective_below_95_percent(tmp_path):
    data = yaml.safe_load(PROFILE.read_text(encoding="utf-8"))
    broken = deepcopy(data)
    broken["spec"]["applications"][1]["entrySlo"]["minimumSuccessRate"] = 0.94
    broken["spec"]["applications"][1]["entrySlo"]["maximumErrorRate"] = 0.06

    with pytest.raises(validate_workload_profiles.ProfileError, match="minimumSuccessRate"):
        validate_workload_profiles.validate_profile(write_profile(tmp_path, broken))


def test_profile_rejects_duplicate_seeds(tmp_path):
    data = yaml.safe_load(PROFILE.read_text(encoding="utf-8"))
    broken = deepcopy(data)
    broken["spec"]["applications"][1]["determinism"]["randomSeed"] = broken["spec"]["applications"][0]["determinism"]["randomSeed"]

    with pytest.raises(validate_workload_profiles.ProfileError, match="seeds must be unique"):
        validate_workload_profiles.validate_profile(write_profile(tmp_path, broken))
