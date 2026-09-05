"""Keep measured effects separate from an Agent's claims about those effects."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .contracts import HarnessReport, RecoveryResult


def assess_evidence(report: HarnessReport, recovery: RecoveryResult) -> dict[str, Any]:
    assessment = report.agent_assessment or report.final_output.get("agent_result") or {}
    history = list(report.final_output.get("assessment_history") or ())
    if isinstance(assessment, Mapping) and assessment:
        history.append({"assessment": dict(assessment), "source": "final_answer"})
    contradictions: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for record in history:
        value = record.get("assessment", record)
        if not isinstance(value, Mapping):
            continue
        for field, topic in (
            ("effect_assessment", "effect"),
            ("recovery_assessment", "recovery"),
        ):
            if value.get(field) != "verified":
                continue
            missing_conditions = [str(v) for v in value.get("missing_conditions") or ()]
            statements = list(missing_conditions)
            statements += re.split(r"[。;；\n]", str(value.get("remaining_risk") or ""))
            for statement in statements:
                text = statement.lower()
                missing = statement in missing_conditions or re.search(
                    r"unverified|unable to (?:verify|confirm)|no (?:reliable )?evidence|"
                    r"lack(?:s|ing)? .*evidence|missing .*evidence|insufficient|unavailable|not (?:verified|confirmed)|"
                    r"未验证|无法.{0,12}(?:验证|确认|取得|获取)|缺少|缺乏|没有取得|未返回|未形成|拿不到",
                    text,
                )
                related = re.search(
                    r"effect|latency|trace|metric|request|error.rate|cpu|效果|延迟|请求|指标|链路|样本|业务"
                    if topic == "effect" else r"recover|恢复|清理|故障对象|残留",
                    text,
                )
                selected_fault = str((value.get("strategy_selection") or {}).get("fault_type")
                                     or (report.final_output.get("approved_plan") or {}).get("fault_type")
                                     or recovery.fault_effect_evidence.get("fault_type") or "").lower()
                if topic == "effect" and "cpu" in selected_fault and not re.search(r"cpu|负载|效果|effect", text):
                    related = None
                if topic == "effect" and "memory" in selected_fault and not re.search(r"mem|内存|效果|effect", text):
                    related = None
                key = (field, statement)
                if missing and related and key not in seen:
                    seen.add(key)
                    contradictions.append({
                        "claim": field, "claimed": "verified", "conflicting_text": statement,
                        "source": record.get("source", "agent_message"),
                        "event_ref": record.get("event_ref"),
                    })
    effect = dict(recovery.fault_effect_evidence)
    observability = dict(effect.get("observability") or {})
    observation = {
        "status": "verified" if recovery.fault_effect_verified else (
            "not_observable" if observability.get("status") == "not_observable" else None
        ),
        "reason": effect.get("reason") or observability.get("reason") or (
            "measured effect verified" if recovery.fault_effect_verified
            else "attributable effect has not been established"
        ),
        "window": dict(effect.get("evidence_window") or {}),
        "observability": observability,
        "evidence_refs": list(recovery.evidence_refs),
    }
    return {
        "effect_observation": observation,
        "effect_claim": {
            "status": "contradicted" if contradictions else "not_contradicted",
            "contradictions": contradictions,
            "original_claims_preserved": True,
        },
    }
