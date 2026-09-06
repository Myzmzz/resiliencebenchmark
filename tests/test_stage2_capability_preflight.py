"""Capability descriptors are evidence-driven, not inferred from installed CLIs."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from stage2_service.capability_preflight import (
    CAPABILITY_QUALIFICATION_SCHEMA,
    harness_capabilities_from_qualification,
)
from stage2_service.runtime_factory import Stage2System


def _capability(kind: str) -> dict:
    return {
        "kind": kind,
        "execution_model": "stream",
        "streams_tool_results": True,
        "post_hoc_trace": False,
        "supports_resume": True,
        "supports_mid_turn_feedback": True,
        "feedback_channels": ["in_band_mcp"],
        "code_execution": "platform_sandbox",
    }


def _qualification_payload(*, include_all: bool = True) -> dict:
    harnesses = ("codex", "claude-code", "deepseek-harness", "bladeai")
    if not include_all:
        harnesses = harnesses[:-1]
    return {
        "schema_version": CAPABILITY_QUALIFICATION_SCHEMA,
        "harnesses": {
            name: {
                "qualification": {
                    "status": "passed",
                    "evidence_ref": f"artifacts/qualification/{name}.json",
                },
                "capability": _capability(name),
            }
            for name in harnesses
        },
    }


def test_missing_qualification_file_never_upgrades_declared_adapter_capabilities():
    descriptors, source = harness_capabilities_from_qualification(None)

    assert source["status"] == "capability_probe_missing"
    assert set(descriptors) == {
        "codex", "claude-code", "deepseek-harness", "bladeai"
    }
    assert all(row["qualification_passed"] is False for row in descriptors.values())
    assert all(
        row["probe"]["qualification_status"] == "capability_probe_missing"
        for row in descriptors.values()
    )


def test_qualification_record_requires_each_harness_and_evidence_reference(tmp_path: Path):
    record = tmp_path / "capabilities.json"
    record.write_text(json.dumps(_qualification_payload(include_all=False)), encoding="utf-8")

    descriptors, source = harness_capabilities_from_qualification(record)

    assert source["status"] == "qualification_records_loaded"
    assert descriptors["codex"]["qualification_passed"] is True
    assert descriptors["codex"]["code_execution"] == "platform_sandbox"
    assert descriptors["bladeai"]["qualification_passed"] is False
    assert source["harnesses"]["bladeai"]["status"] == "capability_probe_missing"


def test_runtime_preflight_uses_only_qualification_record_for_readiness(
    tmp_path: Path, monkeypatch
):
    record = tmp_path / "capabilities.json"
    record.write_text(json.dumps(_qualification_payload()), encoding="utf-8")
    system = object.__new__(Stage2System)
    system.config = SimpleNamespace()
    system.d0_gate = SimpleNamespace(inventory=lambda: {"campaigns": []})
    system._gateway_models = lambda: ({"gpt-5.5"}, None)
    monkeypatch.setenv("STAGE2_HARNESS_CAPABILITIES_FILE", str(record))
    monkeypatch.delenv("RESBENCH_CODEX_EVAL_BIN", raising=False)
    monkeypatch.delenv("STAGE2_BLADEAI_PYTHON", raising=False)
    monkeypatch.setattr("stage2_service.runtime_factory.shutil.which", lambda _: None)

    result = system.preflight()

    assert result["schema_version"] == "stage2-preflight.v3"
    assert "bidirectional_sessions" not in result
    assert result["harness_capability_qualification"]["status"] == "qualification_records_loaded"
    assert all(
        item["qualification_passed"] is True
        and item["feedback_channels"] == ["in_band_mcp"]
        and item["code_execution"] == "platform_sandbox"
        for item in result["harness_capabilities"].values()
    )
    assert all(models["gpt-5.5"] is True for models in result["model_matrix"].values())


def test_failed_qualification_cannot_be_overridden_by_capability_claim(tmp_path: Path):
    payload = _qualification_payload()
    payload["harnesses"]["deepseek-harness"]["qualification"]["status"] = "failed"
    record = tmp_path / "capabilities.json"
    record.write_text(json.dumps(payload), encoding="utf-8")

    descriptors, source = harness_capabilities_from_qualification(record)

    assert descriptors["deepseek-harness"]["qualification_passed"] is False
    assert source["harnesses"]["deepseek-harness"]["status"] == "qualification_not_passed"
