from __future__ import annotations

from pathlib import Path

from scripts.run_execution_worker import execution_timing_policy


def test_standard_profile_keeps_formal_windows() -> None:
    policy = execution_timing_policy("standard-l3")

    assert policy.mode == "formal"
    assert policy.experiment_seconds == 900
    assert policy.formal_run_eligible is True
    assert policy.formal_baseline_report is None


def test_engineering_profile_uses_short_window_and_retained_baseline(
    monkeypatch, tmp_path: Path
) -> None:
    report = tmp_path / "baseline.json"
    monkeypatch.setenv("RESBENCH_FORMAL_BASELINE_REPORT", str(report))

    policy = execution_timing_policy("engineering-l3")

    assert policy.mode == "engineering"
    assert policy.experiment_seconds == 360
    assert policy.formal_run_eligible is False
    assert policy.formal_baseline_report == report.resolve()
