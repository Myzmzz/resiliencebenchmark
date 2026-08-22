import json

import pytest

from scripts import calibrate_workload_results


def write_summary(tmp_path, run_id, throughput, *, seed=2026082203, eligible=True):
    path = tmp_path / f"{run_id}.json"
    path.write_text(
        json.dumps(
            {
                "application": "otel-demo",
                "runId": run_id,
                "randomSeed": seed,
                "trafficMix": [{"flow": "browse", "weightPercent": 100}],
                "qualified": True,
                "checks": {"success": True, "error": True, "latency": True, "samples": True},
                "measurementWindow": {
                    "calibrationWindowEligible": eligible,
                    "measurementWindowSeconds": 300,
                },
                "throughputRps": throughput,
                "p95LatencyMs": 120,
                "successRate": 1.0,
                "errorRate": 0.0,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_calibration_freezes_median_and_95_percent_floor(tmp_path):
    first = write_summary(tmp_path, "run-1", 10.0)
    second = write_summary(tmp_path, "run-2", 10.4)

    result = calibrate_workload_results.calibrate([first, second], application="otel-demo")

    assert result["status"] == "qualified"
    assert result["baselineThroughputRps"] == pytest.approx(10.2)
    assert result["minimumThroughputRps"] == pytest.approx(9.69)
    assert result["throughputSpreadRatio"] == pytest.approx(0.4 / 10.2)


def test_calibration_rejects_seed_drift_or_short_smoke(tmp_path):
    first = write_summary(tmp_path, "run-1", 10.0)
    drifted = write_summary(tmp_path, "run-2", 10.1, seed=99)
    smoke = write_summary(tmp_path, "run-3", 10.1, eligible=False)

    with pytest.raises(calibrate_workload_results.CalibrationError, match="one resolved random seed"):
        calibrate_workload_results.calibrate([first, drifted], application="otel-demo")
    with pytest.raises(calibrate_workload_results.CalibrationError, match="not a full calibration window"):
        calibrate_workload_results.calibrate([first, smoke], application="otel-demo")


def test_calibration_rejects_unstable_throughput(tmp_path):
    first = write_summary(tmp_path, "run-1", 10.0)
    second = write_summary(tmp_path, "run-2", 12.0)

    with pytest.raises(calibrate_workload_results.CalibrationError, match="throughput spread"):
        calibrate_workload_results.calibrate([first, second], application="otel-demo")
