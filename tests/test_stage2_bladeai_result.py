"""Transcription of BladeAI's own final report into the Agent result contract."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from stage2_service.bladeai_result import NOT_STATED, transcribe_bladeai_report

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "stage2_bladeai"
SCHEMA = json.loads((ROOT / "harness/schemas/agent-result.schema.json").read_text(encoding="utf-8"))


def load(name: str) -> dict:
    """Read one captured live report, without the fixture's provenance note."""

    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    data.pop("_note", None)
    return data


def test_c0_report_becomes_a_valid_result_carrying_its_own_evidence():
    result = transcribe_bladeai_report(
        load("terminal_c0.json"),
        assistance_nodes=["PLAN_VALIDATION", "RECOVERY_TRIGGER"],
        actions=["bladeai.blade_create", "bladeai.blade_destroy"],
        agent_cleanup_requested=True,
        interaction_mode="autonomous",
    )

    jsonschema.Draft202012Validator(SCHEMA).validate(result)
    assert result["status"] == "completed"
    assert result["decision"] == "continue"
    assert result["effect_assessment"] == "verified"
    assert result["recovery_assessment"] == "verified"
    summaries = [item["summary"] for item in result["evidence"]]
    # BladeAI's own numbers, copied verbatim: CPU under fault and after cleanup.
    assert any("6259m" in summary and summary.startswith("[效果验证") for summary in summaries)
    assert any("1m / 56420Ki" in summary and summary.startswith("[恢复验证") for summary in summaries)
    # Its caveats are kept as evidence notes, not as a remaining-risk statement.
    assert any(summary.startswith("[效果验证·提示]") for summary in summaries)
    assert result["remaining_risk"] == NOT_STATED
    assert result["suspected_defect"] == NOT_STATED
    assert result["recovery_trigger"] == {
        "condition": NOT_STATED,
        "observed": False,
        "triggered_by_agent": True,
    }
    assert result["assisted"] is True
    assert result["actions_taken"] == ["bladeai.blade_create", "bladeai.blade_destroy"]
    assert "scope_decision" not in result


def test_p2_report_records_what_it_refused_and_kept_in_its_own_words():
    proposal = load("proposal_p2.json")

    result = transcribe_bladeai_report(load("terminal_p2.json"), proposals=[proposal])

    jsonschema.Draft202012Validator(SCHEMA).validate(result)
    assert result["status"] == "unsafe_to_continue"
    assert result["decision"] == "safe_stop"
    assert result["effect_assessment"] == "not_attempted"
    assert result["recovery_assessment"] == "not_applicable"
    assert result["scope_decision"] == {
        "excluded_targets": [proposal["unsafe_additional_target"]],
        "kept_targets": ["otel-demo/cart-7c58f6bb56-zdp5w"],
        "reason": proposal["decision"],
    }
    assert any(item["summary"].startswith("[拒绝原因]") for item in result["evidence"])


def test_rejection_without_a_proposal_uses_the_phrases_it_wrote():
    result = transcribe_bladeai_report(load("terminal_p2.json"))

    assert {"benchmark controller", "observability infrastructure"} <= set(
        result["scope_decision"]["excluded_targets"]
    )
    assert result["scope_decision"]["kept_targets"] == []


def test_reports_without_a_clear_verdict_are_left_alone():
    failed = {
        **load("terminal_p2.json"),
        "extras": {"failure_detail": {"category": "sdk_error", "context": "boom"}},
    }

    assert transcribe_bladeai_report(None) is None
    assert transcribe_bladeai_report({"type": "something_else"}) is None
    assert transcribe_bladeai_report(failed) is None


def test_a_placeholder_unsafe_target_is_not_read_as_a_refusal():
    proposal = {"unsafe_additional_target": "none", "pod_name": "cart-x", "namespace": "otel-demo"}

    result = transcribe_bladeai_report(load("terminal_c0.json"), proposals=[proposal])

    assert "scope_decision" not in result
