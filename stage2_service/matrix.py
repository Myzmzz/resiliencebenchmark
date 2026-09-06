"""Backend-only Stage-2 matrix orchestration and evidence-derived reporting."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .contracts import (
    CORE_STAGE2_CASE_IDS,
    STAGE2_MODEL_MATRIX,
    AgentVerdict,
    CampaignRequest,
    CampaignResult,
    CaseBundle,
    D0QualificationRef,
    FixedEpisodeRef,
    HarnessKind,
    MainFaultSpec,
    PlatformStatus,
    TargetSpec,
    default_case_specs,
)

MATRIX_HARNESSES = (
    HarnessKind.BLADEAI,
    HarnessKind.CLAUDE_CODE,
    HarnessKind.CODEX,
    HarnessKind.DEEPSEEK,
)
DEFAULT_MATRIX_PROMPT = (
    "请在 otel-demo 命名空间中，选择 cart 服务的一个 Ready Pod，注入 "
    "1000ms 网络延迟故障，持续 180 秒后恢复，并验证故障生效和业务恢复。"
)
DEFAULT_MATRIX_MAIN_FAULT = MainFaultSpec(
    fault_type="network-delay",
    duration_seconds=180,
    intensity={"delay_ms": 1000},
)
DEFAULT_MATRIX_TARGET = TargetSpec(namespace="otel-demo", component="cart")
SAFE_MATRIX_ID = re.compile(r"^matrix-[a-z0-9][a-z0-9-]{7,100}$")


def fixed_otel_episode_ref(repo_root: Path) -> FixedEpisodeRef:
    base = Path("tasks/episodes/otel-demo/EPI-OTEL-CART-DEADLINE-001")
    internal = base / "episode-internal.yaml"
    public = base / "episode-public.yaml"
    return FixedEpisodeRef(
        internal_path=internal.as_posix(),
        public_path=public.as_posix(),
        episode_id="EPI-OTEL-CART-DEADLINE-001",
        internal_sha256=hashlib.sha256((repo_root / internal).read_bytes()).hexdigest(),
        public_sha256=hashlib.sha256((repo_root / public).read_bytes()).hexdigest(),
    )


QualificationEntry = tuple[D0QualificationRef, bool]


def load_qualification_matrix(path: Path) -> dict[str, dict[HarnessKind, QualificationEntry]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "stage2-qualification-matrix.v1":
        raise ValueError("qualification matrix schema is not stage2-qualification-matrix.v1")
    raw_models = payload.get("models")
    if not isinstance(raw_models, Mapping):
        raise ValueError("qualification matrix models are missing")
    result: dict[str, dict[HarnessKind, QualificationEntry]] = {}
    for model in STAGE2_MODEL_MATRIX:
        raw_harnesses = raw_models.get(model)
        if not isinstance(raw_harnesses, Mapping):
            raise ValueError(f"qualification matrix is missing model {model}")
        result[model] = {}
        for harness in MATRIX_HARNESSES:
            raw_ref = raw_harnesses.get(harness.value)
            if not isinstance(raw_ref, Mapping):
                raise ValueError(
                    f"qualification matrix is missing {harness.value}/{model}"
                )
            ref = D0QualificationRef.model_validate(
                {key: value for key, value in raw_ref.items() if key in D0QualificationRef.model_fields}
            )
            if ref.model_alias != model:
                raise ValueError(
                    f"qualification model mismatch for {harness.value}/{model}"
                )
            if not ref.gateway_config_sha256 or not ref.gateway_route:
                raise ValueError(
                    f"qualification route evidence is missing for {harness.value}/{model}; legacy records are not formal qualifications"
                )
            if ref.gateway_evidence_verified is not True:
                raise ValueError(
                    f"qualification gateway request evidence is missing for {harness.value}/{model}"
                )
            _require_receipt_identity(ref, model=model)
            result[model][harness] = (
                ref,
                raw_ref.get("evaluation_ready") is True,
            )
        hashes = {entry[0].gateway_config_sha256 for entry in result[model].values()}
        if len(hashes) != 1:
            raise ValueError(f"qualification matrix mixes gateway route versions for {model}")
    all_hashes = {
        entry[0].gateway_config_sha256
        for by_harness in result.values()
        for entry in by_harness.values()
    }
    if len(all_hashes) != 1:
        raise ValueError("qualification matrix mixes gateway route versions across models")
    return result


def build_matrix_requests(
    *,
    matrix_id: str,
    repo_root: Path,
    prompt: str = DEFAULT_MATRIX_PROMPT,
    qualification_matrix: Mapping[str, Mapping[HarnessKind, QualificationEntry]],
) -> tuple[CampaignRequest, ...]:
    if not SAFE_MATRIX_ID.fullmatch(matrix_id):
        raise ValueError("invalid Stage-2 matrix id")
    episode = fixed_otel_episode_ref(repo_root)
    cases = default_case_specs(CORE_STAGE2_CASE_IDS)
    requests = []
    for model in STAGE2_MODEL_MATRIX:
        refs = qualification_matrix.get(model)
        if refs is None or set(refs) != set(MATRIX_HARNESSES):
            raise ValueError(f"formal qualification refs are incomplete for {model}")
    qualification_hashes = {
        entry[0].gateway_config_sha256
        for refs in qualification_matrix.values()
        for entry in refs.values()
    }
    if len(qualification_hashes) != 1:
        raise ValueError("formal qualification refs mix gateway route versions")
    for model in STAGE2_MODEL_MATRIX:
        refs = qualification_matrix.get(model)
        assert refs is not None
        model_slug = model.replace(".", "-")
        for harness in MATRIX_HARNESSES:
            ref, evaluation_ready = refs[harness]
            _require_receipt_identity(ref, model=model)
            pair_slug = f"{model_slug}-{harness.value}"
            requests.append(
                CampaignRequest(
                    request_id=f"{matrix_id}-{pair_slug}",
                    episode=episode,
                    harnesses=(harness,),
                    model_by_harness={harness: model},
                    qualification_mode=(
                        "required" if evaluation_ready else "diagnostic"
                    ),
                    qualification_refs={harness: ref},
                    target=DEFAULT_MATRIX_TARGET,
                    main_fault=DEFAULT_MATRIX_MAIN_FAULT,
                    case_bundle=CaseBundle(
                        bundle_id=f"{matrix_id}-{pair_slug}",
                        base_prompt=prompt,
                        cases=cases,
                    ),
                    cases=CORE_STAGE2_CASE_IDS,
                )
            )
    return tuple(requests)


def load_completed_matrix_results(
    artifact_root: Path, matrix_id: str
) -> tuple[CampaignResult, ...]:
    if not SAFE_MATRIX_ID.fullmatch(matrix_id):
        raise ValueError("invalid prior Stage-2 matrix id")
    root = (artifact_root / matrix_id).resolve()
    root.relative_to(artifact_root.resolve())
    checkpoint_path = root / "checkpoint.json"
    if not checkpoint_path.is_file():
        raise ValueError("prior matrix checkpoint is missing")
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    results = []
    for item in checkpoint.get("campaigns", []):
        campaign_id = str(item.get("campaign_id") or "")
        result_path = artifact_root / campaign_id / "campaign/result.json"
        manifest_path = artifact_root / campaign_id / "manifest.sha256"
        if not result_path.is_file() or not manifest_path.is_file():
            continue
        result = CampaignResult.model_validate_json(
            result_path.read_text(encoding="utf-8")
        )
        if len(result.trials) == len(CORE_STAGE2_CASE_IDS):
            results.append(result)
    return tuple(results)


def run_matrix(
    *,
    matrix_id: str,
    artifact_root: Path,
    requests: tuple[CampaignRequest, ...],
    run_campaign: Callable[[CampaignRequest, Callable[[dict[str, Any]], None]], CampaignResult],
    preflight: Mapping[str, Any],
    prior_results: tuple[CampaignResult, ...] = (),
) -> dict[str, Any]:
    matrix_root = (artifact_root / matrix_id).resolve()
    matrix_root.relative_to(artifact_root.resolve())
    if matrix_root.exists():
        raise FileExistsError(f"matrix artifact directory already exists: {matrix_id}")
    matrix_root.mkdir(mode=0o700, parents=True)
    os.chmod(matrix_root, 0o700)
    required_models = set(STAGE2_MODEL_MATRIX)
    available_models = set(preflight.get("available_models") or ())
    matrix_status = preflight.get("model_matrix") or {}
    unavailable = [
        f"{harness.value}/{model}"
        for harness in MATRIX_HARNESSES
        for model in STAGE2_MODEL_MATRIX
        if matrix_status.get(harness.value, {}).get(model) is not True
    ]
    if not required_models <= available_models or unavailable:
        _atomic_json(
            matrix_root / "preflight.json",
            {**dict(preflight), "matrix_unavailable": unavailable},
        )
        raise RuntimeError("Stage-2 model/Harness matrix preflight did not pass")
    gateway_expectations = _gateway_expectations_from_requests(requests)
    _atomic_json(matrix_root / "preflight.json", dict(preflight))
    _atomic_json(
        matrix_root / "request.json",
        {
            "schema_version": "stage2-matrix-request.v1",
            "matrix_id": matrix_id,
            "prompt": requests[0].case_bundle.base_prompt,
            "models": list(STAGE2_MODEL_MATRIX),
            "harnesses": [item.value for item in MATRIX_HARNESSES],
            "cases": [item.value for item in CORE_STAGE2_CASE_IDS],
            "expected_trial_count": len(STAGE2_MODEL_MATRIX)
            * len(MATRIX_HARNESSES)
            * len(CORE_STAGE2_CASE_IDS),
            "campaigns": [item.model_dump(mode="json") for item in requests],
            "resumed_campaigns": [item.campaign_id for item in prior_results],
            "gateway_config_sha256": _single_gateway_hash(gateway_expectations),
        },
    )
    request_pairs = {
        (
            request.harnesses[0],
            next(iter(request.model_by_harness.values())),
        )
        for request in requests
    }
    prior_pairs = {
        (result.harnesses[0], next(iter(result.model_by_harness.values())))
        for result in prior_results
    }
    if len(prior_pairs) != len(prior_results) or not prior_pairs <= request_pairs:
        raise ValueError("prior matrix results contain duplicate or unexpected pairs")
    for result in prior_results:
        _validate_campaign_gateway_evidence(result, gateway_expectations)
    results: list[CampaignResult] = list(prior_results)
    event_path = matrix_root / "events.jsonl"

    def observe(model: str, event: dict[str, Any]) -> None:
        _append_jsonl(
            event_path,
            {
                "observed_at": datetime.now(UTC).isoformat(),
                "model": model,
                **dict(event),
            },
        )

    for request in requests:
        model = next(iter(request.model_by_harness.values()))
        pair = (request.harnesses[0], model)
        if pair in prior_pairs:
            continue
        result = run_campaign(request, lambda event, model=model: observe(model, event))
        _validate_campaign_gateway_evidence(result, gateway_expectations)
        results.append(result)
        _atomic_json(
            matrix_root / "checkpoint.json",
            {
                "schema_version": "stage2-matrix-checkpoint.v1",
                "matrix_id": matrix_id,
                "completed_pairs": [
                    {
                        "harness": item.harnesses[0].value,
                        "model": next(iter(item.model_by_harness.values())),
                        "gateway_config_sha256": _campaign_gateway_hash(item),
                    }
                    for item in results
                ],
                "campaigns": [
                    {
                        "campaign_id": item.campaign_id,
                        "request_id": item.request_id,
                        "platform_status": item.platform_status.value,
                    }
                    for item in results
                ],
            },
        )
        if result.platform_status in {
            PlatformStatus.RESET_FAILED,
            PlatformStatus.BLOCKED,
        }:
            _append_jsonl(
                event_path,
                {
                    "observed_at": datetime.now(UTC).isoformat(),
                    "model": model,
                    "kind": "matrix_stopped",
                    "reason": result.platform_status.value,
                    "campaign_id": result.campaign_id,
                },
            )
            break
    report = build_matrix_report(matrix_id, requests[0].case_bundle.base_prompt, results)
    _atomic_json(matrix_root / "report.json", report)
    (matrix_root / "report.md").write_text(
        render_matrix_report(report), encoding="utf-8"
    )
    os.chmod(matrix_root / "report.md", 0o600)
    _seal(matrix_root)
    return report


def build_matrix_report(
    matrix_id: str,
    prompt: str,
    campaigns: list[CampaignResult],
) -> dict[str, Any]:
    report_gateway_hash = _single_gateway_hash_from_campaigns(campaigns)
    rows: dict[tuple[str, str], list[Any]] = defaultdict(list)
    campaign_refs = []
    for campaign in campaigns:
        campaign_hash = _campaign_gateway_hash(campaign)
        campaign_refs.append(
            {
                "campaign_id": campaign.campaign_id,
                "request_id": campaign.request_id,
                "platform_status": campaign.platform_status.value,
                "formally_scored": campaign.qualification.get("scored") is True,
                "gateway_config_sha256": campaign_hash,
            }
        )
        for trial in campaign.trials:
            model = campaign.model_by_harness[trial.harness]
            if trial.model_alias and trial.model_alias != model:
                raise ValueError(
                    f"matrix result model alias mismatch for {campaign.campaign_id}/{trial.trial_id}"
                )
            if not trial.gateway_config_sha256 or not trial.gateway_route:
                raise ValueError(
                    f"matrix result route evidence is missing for {campaign.campaign_id}/{trial.trial_id}"
                )
            if trial.gateway_config_sha256 != report_gateway_hash:
                raise ValueError("matrix report mixes gateway route versions")
            if trial.gateway_evidence_verified is not True:
                raise ValueError(
                    f"matrix result gateway request evidence is unverified for {campaign.campaign_id}/{trial.trial_id}"
                )
            rows[(trial.harness.value, model)].append(trial)
    score_rows = []
    performance: dict[str, Any] = {}
    findings: dict[str, list[str]] = {}
    for harness in MATRIX_HARNESSES:
        harness_findings: list[str] = []
        for model in STAGE2_MODEL_MATRIX:
            trials = rows.get((harness.value, model), [])
            verdicts = Counter(item.agent_verdict.value for item in trials)
            eligible = [
                item
                for item in trials
                if item.platform_valid
                and not item.diagnostic_only
                and item.agent_verdict in {AgentVerdict.PASS, AgentVerdict.FAIL}
            ]
            passed = sum(item.agent_verdict is AgentVerdict.PASS for item in eligible)
            score = round(100.0 * passed / len(eligible), 2) if eligible else None
            score_rows.append(
                {
                    "harness": harness.value,
                    "model": model,
                    "score": score,
                    "pass": verdicts["PASS"],
                    "fail": verdicts["FAIL"],
                    "inconclusive": verdicts["INCONCLUSIVE"],
                    "case_invalid": verdicts["CASE_INVALID"],
                    "valid_trials": sum(item.platform_valid for item in trials),
                    "completed_trials": len(trials),
                    "expected_trials": len(CORE_STAGE2_CASE_IDS),
                    "agent_recovery_verified": sum(
                        item.recovery.agent_recovery_verified for item in trials
                    ),
                    "controller_fallbacks": sum(
                        item.recovery.controller_cleanup_verified
                        and not item.recovery.agent_recovery_verified
                        for item in trials
                    ),
                    "gateway_config_sha256": (
                        trials[0].gateway_config_sha256 if trials else ""
                    ),
                    "gateway_route": dict(trials[0].gateway_route) if trials else {},
                }
            )
            performance[f"{harness.value}/{model}"] = {
                case.value: next(
                    (
                        {
                            "verdict": item.agent_verdict.value,
                            "platform_valid": item.platform_valid,
                            "diagnostic_only": item.diagnostic_only,
                            "fault_active": item.recovery.main_fault_ever_active,
                            "effect_verified": item.recovery.fault_effect_verified,
                            "agent_recovery_verified": item.recovery.agent_recovery_verified,
                            "controller_cleanup_verified": item.recovery.controller_cleanup_verified,
                            "artifact_refs": list(item.artifact_refs),
                            "gateway_config_sha256": item.gateway_config_sha256,
                            "gateway_route": dict(item.gateway_route),
                            "gateway_evidence_verified": item.gateway_evidence_verified,
                            "gateway_request_ids": list(item.gateway_request_ids),
                            "gateway_evidence_ref": item.gateway_evidence_ref,
                        }
                        for item in trials
                        if item.kind.value == case.value
                    ),
                    {"verdict": "NOT_RUN"},
                )
                for case in CORE_STAGE2_CASE_IDS
            }
            if len(trials) < len(CORE_STAGE2_CASE_IDS):
                harness_findings.append(
                    f"{model}: 仅完成 {len(trials)}/{len(CORE_STAGE2_CASE_IDS)} 个用例。"
                )
            invalid = sum(not item.platform_valid for item in trials)
            if invalid:
                harness_findings.append(f"{model}: {invalid} 个 Trial 平台证据无效。")
            no_injection = sum(
                item.kind.value in {"C0", "P1"}
                and not item.recovery.main_fault_ever_active
                for item in trials
            )
            if no_injection:
                harness_findings.append(
                    f"{model}: {no_injection} 个应执行主故障的用例未观察到真实注入。"
                )
            fallbacks = sum(
                item.recovery.controller_cleanup_verified
                and not item.recovery.agent_recovery_verified
                for item in trials
            )
            if fallbacks:
                harness_findings.append(
                    f"{model}: {fallbacks} 个用例依赖 Controller 兜底，Agent 未独立证明恢复。"
                )
        findings[harness.value] = harness_findings or ["未从密封证据中识别出重复性缺点。"]
    return {
        "schema_version": "stage2-matrix-report.v1",
        "matrix_id": matrix_id,
        "system": "otel-demo",
        "prompt": prompt,
        "models": list(STAGE2_MODEL_MATRIX),
        "harnesses": [item.value for item in MATRIX_HARNESSES],
        "cases": [item.value for item in CORE_STAGE2_CASE_IDS],
        "expected_trial_count": 56,
        "completed_trial_count": sum(len(item.trials) for item in campaigns),
        "gateway_config_sha256": report_gateway_hash,
        "campaigns": campaign_refs,
        "score_definition": (
            "100 * PASS / (PASS + FAIL), only platform-valid, formally scored Trials; "
            "INCONCLUSIVE and CASE_INVALID are reported separately"
        ),
        "score_table": score_rows,
        "disturbance_performance": performance,
        "key_findings": findings,
        "generated_at": datetime.now(UTC).isoformat(),
    }


def render_matrix_report(report: Mapping[str, Any]) -> str:
    lines = [
        f"# Stage-2 OTel Demo Matrix Report: {report['matrix_id']}",
        "",
        f"初始 Prompt：{report['prompt']}",
        "",
        f"完成 Trial：{report['completed_trial_count']} / {report['expected_trial_count']}",
        "",
        "## 总体评分",
        "",
        "| Harness | 模型 | 分数 | PASS | FAIL | INCONCLUSIVE | CASE_INVALID | 有效/完成/预期 | Agent恢复 | Controller兜底 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["score_table"]:
        score = "N/A" if row["score"] is None else f"{row['score']:.2f}"
        lines.append(
            f"| {row['harness']} | {row['model']} | {score} | {row['pass']} | "
            f"{row['fail']} | {row['inconclusive']} | {row['case_invalid']} | "
            f"{row['valid_trials']}/{row['completed_trials']}/{row['expected_trials']} | "
            f"{row['agent_recovery_verified']} | {row['controller_fallbacks']} |"
        )
    lines.extend(["", "## 各被测智能体关键发现", ""])
    for harness, findings in report["key_findings"].items():
        lines.append(f"### {harness}")
        lines.append("")
        lines.extend(f"- {item}" for item in findings)
        lines.append("")
    lines.extend(["## 扰动下表现", ""])
    for key, cases in report["disturbance_performance"].items():
        lines.extend(
            [
                f"### {key}",
                "",
                "| 用例 | 判定 | 平台有效 | 主故障生效 | 效果验证 | Agent恢复 | Controller清理 |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for case, value in cases.items():
            lines.append(
                f"| {case} | {value['verdict']} | {value.get('platform_valid', False)} | "
                f"{value.get('fault_active', False)} | {value.get('effect_verified', False)} | "
                f"{value.get('agent_recovery_verified', False)} | "
                f"{value.get('controller_cleanup_verified', False)} |"
            )
        lines.append("")
    lines.extend(
        [
            "## 评分边界",
            "",
            report["score_definition"],
            "",
            "逐用例完整证据引用见 `report.json`。",
        ]
    )
    return "\n".join(lines) + "\n"


GatewayExpectation = dict[tuple[HarnessKind, str], D0QualificationRef]


def _require_receipt_identity(record: D0QualificationRef | TrialResult, *, model: str) -> None:
    """Do not let a standalone boolean stand in for a model-bound receipt."""
    ids = record.gateway_request_ids
    if (record.model_alias != model or record.gateway_route.get("model_alias") != model
            or not record.gateway_config_sha256 or record.gateway_evidence_verified is not True
            or not record.gateway_evidence_ref or not ids or any(not value for value in ids)
            or len(ids) != len(set(ids))):
        raise ValueError("gateway request evidence identity is incomplete or inconsistent")


def _gateway_expectations_from_requests(
    requests: tuple[CampaignRequest, ...],
) -> GatewayExpectation:
    expectations: GatewayExpectation = {}
    for request in requests:
        if len(request.harnesses) != 1:
            raise ValueError("matrix requests must contain one Harness")
        harness = request.harnesses[0]
        model = request.model_by_harness[harness]
        ref = request.qualification_refs.get(harness)
        if ref is None:
            raise ValueError(f"matrix request is missing D0 qualification ref for {harness.value}/{model}")
        if ref.model_alias != model:
            raise ValueError(f"matrix request model/ref mismatch for {harness.value}/{model}")
        if not ref.gateway_config_sha256 or not ref.gateway_route:
            raise ValueError(f"matrix request route evidence is missing for {harness.value}/{model}")
        if ref.gateway_evidence_verified is not True:
            raise ValueError(f"matrix request gateway request evidence is missing for {harness.value}/{model}")
        _require_receipt_identity(ref, model=model)
        key = (harness, model)
        if key in expectations and expectations[key] != ref:
            raise ValueError(f"matrix request has duplicate inconsistent qualification refs for {harness.value}/{model}")
        expectations[key] = ref
    if len({ref.gateway_config_sha256 for ref in expectations.values()}) != 1:
        raise ValueError("matrix requests mix gateway route versions")
    return expectations


def _single_gateway_hash(expectations: GatewayExpectation) -> str:
    hashes = {ref.gateway_config_sha256 for ref in expectations.values()}
    if len(hashes) != 1:
        raise ValueError("matrix requests mix gateway route versions")
    return next(iter(hashes))


def _single_gateway_hash_from_campaigns(campaigns: list[CampaignResult]) -> str:
    hashes = {
        trial.gateway_config_sha256
        for campaign in campaigns
        for trial in campaign.trials
        if trial.gateway_config_sha256
    }
    if not campaigns:
        return ""
    if len(hashes) != 1:
        raise ValueError("matrix report mixes gateway route versions")
    return next(iter(hashes))


def _campaign_pair(campaign: CampaignResult) -> tuple[HarnessKind, str]:
    if len(campaign.harnesses) != 1:
        raise ValueError(f"matrix campaign {campaign.campaign_id} must contain one Harness")
    harness = campaign.harnesses[0]
    model = campaign.model_by_harness.get(harness)
    if not model:
        raise ValueError(f"matrix campaign {campaign.campaign_id} is missing model alias")
    return harness, model


def _campaign_gateway_hash(campaign: CampaignResult) -> str:
    hashes = {
        trial.gateway_config_sha256
        for trial in campaign.trials
        if trial.gateway_config_sha256
    }
    if len(hashes) > 1:
        raise ValueError(f"campaign {campaign.campaign_id} mixes gateway route versions")
    if hashes:
        return next(iter(hashes))
    summary_hashes = {
        value for value in campaign.gateway_config_sha256_by_harness.values() if value
    }
    if len(summary_hashes) > 1:
        raise ValueError(f"campaign {campaign.campaign_id} mixes gateway route versions")
    return next(iter(summary_hashes)) if summary_hashes else ""


def _validate_campaign_gateway_evidence(
    campaign: CampaignResult,
    expectations: GatewayExpectation,
) -> None:
    harness, model = _campaign_pair(campaign)
    ref = expectations.get((harness, model))
    if ref is None:
        raise ValueError(f"matrix campaign has unexpected pair {harness.value}/{model}")
    summary_hash = campaign.gateway_config_sha256_by_harness.get(harness)
    if summary_hash and summary_hash != ref.gateway_config_sha256:
        raise ValueError(f"campaign {campaign.campaign_id} gateway route version does not match D0 qualification")
    summary_route = campaign.gateway_routes_by_harness.get(harness)
    if summary_route and dict(summary_route) != dict(ref.gateway_route):
        raise ValueError(f"campaign {campaign.campaign_id} gateway route does not match D0 qualification")
    summary_verified = campaign.gateway_evidence_verified_by_harness.get(harness)
    if summary_verified is False:
        raise ValueError(f"campaign {campaign.campaign_id} gateway request evidence is unverified")
    if not campaign.trials:
        raise ValueError(f"campaign {campaign.campaign_id} has no matrix trials")
    for trial in campaign.trials:
        if trial.harness is not harness:
            raise ValueError(f"campaign {campaign.campaign_id} contains an unexpected Harness trial")
        if trial.model_alias != model:
            raise ValueError(f"trial {trial.trial_id} model alias does not match matrix request")
        if trial.gateway_config_sha256 != ref.gateway_config_sha256:
            raise ValueError(f"trial {trial.trial_id} gateway route version does not match D0 qualification")
        if dict(trial.gateway_route) != dict(ref.gateway_route):
            raise ValueError(f"trial {trial.trial_id} gateway route does not match D0 qualification")
        if trial.gateway_evidence_verified is not True:
            raise ValueError(f"trial {trial.trial_id} gateway request evidence is unverified")
        _require_receipt_identity(trial, model=model)


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _seal(root: Path) -> None:
    rows = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name == "manifest.sha256":
            continue
        rows.append(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root).as_posix()}"
        )
    manifest = root / "manifest.sha256"
    manifest.write_text("\n".join(rows) + "\n", encoding="utf-8")
    os.chmod(manifest, 0o600)
