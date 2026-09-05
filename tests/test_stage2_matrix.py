from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from stage2_service.contracts import (
    CORE_STAGE2_CASE_IDS,
    STAGE2_MODEL_MATRIX,
    AgentVerdict,
    CampaignResult,
    D0QualificationRef,
    HarnessKind,
    PlatformStatus,
    RecoveryResult,
    RuntimeTarget,
    TrialResult,
    default_case_specs,
)
from stage2_service.matrix import (
    MATRIX_HARNESSES,
    build_matrix_report,
    build_matrix_requests,
    load_completed_matrix_results,
    run_matrix,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def qualifications():
    return {
        model: {
            harness: (
                D0QualificationRef(
                    campaign_id=f"d0-otel-accounting-{model.replace('.', '-')}-{harness.value}",
                    manifest_sha256="a" * 64,
                    agent_status="PASS",
                    model_alias=model,
                ),
                True,
            )
            for harness in MATRIX_HARNESSES
        }
        for model in STAGE2_MODEL_MATRIX
    }


def campaign_result(request, campaign_id: str) -> CampaignResult:
    trials = []
    for harness in request.harnesses:
        for case in default_case_specs(request.cases):
            trials.append(
                TrialResult(
                    trial_id=f"{campaign_id}-{harness.value}-{case.case_id.value.lower()}",
                    harness=harness,
                    kind=case.trial_kind,
                    runtime_target=RuntimeTarget(
                        namespace="otel-demo",
                        component="cart",
                        name="cart-example",
                        uid="11111111-2222-4333-8444-555555555555",
                    ),
                    platform_valid=True,
                    diagnostic_only=False,
                    agent_verdict=AgentVerdict.PASS,
                    disturbances=(),
                    recovery=RecoveryResult(
                        agent_attempted=True,
                        agent_recovery_verified=True,
                        controller_cleanup_verified=True,
                        fault_absent=True,
                        business_recovery_verified=True,
                        main_fault_ever_active=True,
                        main_fault_target_verified=True,
                        fault_effect_verified=True,
                    ),
                    artifact_refs=(f"{campaign_id}/trial.json",),
                )
            )
    now = datetime.now(UTC)
    return CampaignResult(
        campaign_id=campaign_id,
        request_id=request.request_id,
        harnesses=request.harnesses,
        model_by_harness=request.model_by_harness,
        platform_status=PlatformStatus.COMPLETED,
        trials=tuple(trials),
        started_at=now,
        finished_at=now,
        qualification={"scored": True},
    )


def test_builds_exact_four_by_two_by_seven_formal_matrix():
    requests = build_matrix_requests(
        matrix_id="matrix-otel-20260901-120000",
        repo_root=REPO_ROOT,
        qualification_matrix=qualifications(),
    )

    assert len(requests) == 8
    assert {next(iter(item.model_by_harness.values())) for item in requests} == set(
        STAGE2_MODEL_MATRIX
    )
    assert all(len(item.harnesses) == 1 for item in requests)
    assert {item.harnesses[0] for item in requests} == set(MATRIX_HARNESSES)
    assert all(item.cases == CORE_STAGE2_CASE_IDS for item in requests)
    assert all(item.qualification_mode == "required" for item in requests)
    assert sum(len(item.harnesses) * len(item.cases) for item in requests) == len(requests) * len(CORE_STAGE2_CASE_IDS)


def test_matrix_runner_writes_scored_report_and_manifest(tmp_path):
    requests = build_matrix_requests(
        matrix_id="matrix-otel-20260901-120001",
        repo_root=REPO_ROOT,
        qualification_matrix=qualifications(),
    )
    sequence = iter(f"campaign-{index:016x}" for index in range(1, 9))

    report = run_matrix(
        matrix_id="matrix-otel-20260901-120001",
        artifact_root=tmp_path,
        requests=requests,
        run_campaign=lambda request, _observer: campaign_result(request, next(sequence)),
        preflight={
            "available_models": list(STAGE2_MODEL_MATRIX),
            "model_matrix": {
                harness.value: {model: True for model in STAGE2_MODEL_MATRIX}
                for harness in MATRIX_HARNESSES
            },
        },
    )

    root = tmp_path / "matrix-otel-20260901-120001"
    assert report["completed_trial_count"] == len(requests) * len(CORE_STAGE2_CASE_IDS)
    assert len(report["score_table"]) == 8
    assert all(row["score"] == 100.0 for row in report["score_table"])
    assert (root / "checkpoint.json").is_file()
    assert (root / "report.json").is_file()
    assert (root / "report.md").is_file()
    assert (root / "manifest.sha256").is_file()
    rendered = (root / "report.md").read_text(encoding="utf-8")
    assert "## 扰动下表现" in rendered
    assert f"### bladeai/{STAGE2_MODEL_MATRIX[0]}" in rendered


def test_report_separates_platform_invalid_from_agent_score():
    requests = build_matrix_requests(
        matrix_id="matrix-otel-20260901-120002",
        repo_root=REPO_ROOT,
        qualification_matrix=qualifications(),
    )
    first = campaign_result(requests[0], "campaign-" + "3" * 16)
    trial = first.trials[0].model_copy(
        update={"platform_valid": False, "agent_verdict": AgentVerdict.CASE_INVALID}
    )
    first = first.model_copy(update={"trials": (trial, *first.trials[1:])})

    report = build_matrix_report("matrix-otel-20260901-120002", "prompt", [first])

    blade = next(
        row
        for row in report["score_table"]
        if row["harness"] == HarnessKind.BLADEAI.value
        and row["model"] == STAGE2_MODEL_MATRIX[0]
    )
    assert blade["case_invalid"] == 1
    # every core case except the one marked platform-invalid above
    assert blade["valid_trials"] == len(CORE_STAGE2_CASE_IDS) - 1
    assert blade["score"] == 100.0
    assert any("平台证据无效" in item for item in report["key_findings"]["bladeai"])


def test_matrix_stops_before_second_model_after_reset_failure(tmp_path):
    requests = build_matrix_requests(
        matrix_id="matrix-otel-20260901-120003",
        repo_root=REPO_ROOT,
        qualification_matrix=qualifications(),
    )
    calls = []

    def failed_reset(request, _observer):
        calls.append(request.request_id)
        result = campaign_result(request, "campaign-" + "4" * 16)
        return result.model_copy(update={"platform_status": PlatformStatus.RESET_FAILED})

    report = run_matrix(
        matrix_id="matrix-otel-20260901-120003",
        artifact_root=tmp_path,
        requests=requests,
        run_campaign=failed_reset,
        preflight={
            "available_models": list(STAGE2_MODEL_MATRIX),
            "model_matrix": {
                harness.value: {model: True for model in STAGE2_MODEL_MATRIX}
                for harness in MATRIX_HARNESSES
            },
        },
    )

    assert len(calls) == 1
    assert report["completed_trial_count"] == len(CORE_STAGE2_CASE_IDS)
    events = (
        tmp_path / "matrix-otel-20260901-120003" / "events.jsonl"
    ).read_text(encoding="utf-8")
    assert '"kind": "matrix_stopped"' in events
    assert '"reason": "RESET_FAILED"' in events


def test_platform_invalid_pair_runs_diagnostic_without_blocking_other_pairs():
    values = qualifications()
    ref, _ = values["claude-opus-5"][HarnessKind.CODEX]
    values["claude-opus-5"][HarnessKind.CODEX] = (ref, False)

    requests = build_matrix_requests(
        matrix_id="matrix-otel-20260901-120004",
        repo_root=REPO_ROOT,
        qualification_matrix=values,
    )

    diagnostic = next(
        item
        for item in requests
        if item.harnesses == (HarnessKind.CODEX,)
        and item.model_by_harness[HarnessKind.CODEX] == "claude-opus-5"
    )
    assert diagnostic.qualification_mode == "diagnostic"
    assert all(
        item.qualification_mode == "required"
        for item in requests
        if item is not diagnostic
    )


def test_resume_skips_only_pairs_with_seven_sealed_trials(tmp_path):
    requests = build_matrix_requests(
        matrix_id="matrix-otel-20260901-120005",
        repo_root=REPO_ROOT,
        qualification_matrix=qualifications(),
    )
    prior = campaign_result(requests[0], "campaign-" + "a" * 16)
    sequence = iter(f"campaign-{index:016x}" for index in range(20, 27))
    calls = []

    def execute(request, _observer):
        calls.append(request.request_id)
        return campaign_result(request, next(sequence))

    report = run_matrix(
        matrix_id="matrix-otel-20260901-120005",
        artifact_root=tmp_path,
        requests=requests,
        run_campaign=execute,
        preflight={
            "available_models": list(STAGE2_MODEL_MATRIX),
            "model_matrix": {
                harness.value: {model: True for model in STAGE2_MODEL_MATRIX}
                for harness in MATRIX_HARNESSES
            },
        },
        prior_results=(prior,),
    )

    assert len(calls) == 7
    # one sealed prior campaign plus seven executed ones, each covering the
    # full core case list (C0, P1, P2, D1-D6)
    assert report["completed_trial_count"] == len(requests) * len(CORE_STAGE2_CASE_IDS)


def test_load_prior_results_requires_seven_trials_and_sealed_campaign(tmp_path):
    requests = build_matrix_requests(
        matrix_id="matrix-otel-20260901-120006",
        repo_root=REPO_ROOT,
        qualification_matrix=qualifications(),
    )
    complete = campaign_result(requests[0], "campaign-" + "b" * 16)
    incomplete = campaign_result(requests[1], "campaign-" + "c" * 16).model_copy(
        update={"trials": ()}
    )
    matrix_root = tmp_path / "matrix-otel-20260901-120006"
    matrix_root.mkdir()
    (matrix_root / "checkpoint.json").write_text(
        json.dumps(
            {
                "campaigns": [
                    {"campaign_id": complete.campaign_id},
                    {"campaign_id": incomplete.campaign_id},
                ]
            }
        ),
        encoding="utf-8",
    )
    for result in (complete, incomplete):
        root = tmp_path / result.campaign_id
        (root / "campaign").mkdir(parents=True)
        (root / "campaign/result.json").write_text(
            result.model_dump_json(), encoding="utf-8"
        )
        (root / "manifest.sha256").write_text("sealed\n", encoding="utf-8")

    loaded = load_completed_matrix_results(
        tmp_path, "matrix-otel-20260901-120006"
    )

    assert [item.campaign_id for item in loaded] == [complete.campaign_id]
