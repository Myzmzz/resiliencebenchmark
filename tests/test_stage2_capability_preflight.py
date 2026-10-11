"""Capability descriptors are evidence-driven, not inferred from installed CLIs."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import yaml

from stage2_service.contracts import D0QualificationRef, STAGE2_SUPPORTED_MODELS
from stage2_service.capability_preflight import (
    CAPABILITY_QUALIFICATION_SCHEMA,
    harness_capabilities_from_qualification,
)
from stage2_service.gateway_config import GatewayConfigSnapshot
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


def _d0_ref(snapshot: GatewayConfigSnapshot) -> D0QualificationRef:
    return D0QualificationRef(
        campaign_id="d0-otel-accounting-gpt-5-5-codex",
        manifest_sha256="a" * 64,
        agent_status="PASS",
        model_alias="gpt-5.5",
        gateway_route=snapshot.route("gpt-5.5"),
        gateway_config_sha256=snapshot.config_sha256,
        gateway_evidence_verified=True,
        gateway_request_ids=("codex-req-1",),
        gateway_evidence_ref="native/d0-preflight/codex/gateway-requests.json",
        gateway_trial_id="codex-trial",
    )


class D0Gate:
    def __init__(self, ref: D0QualificationRef):
        self.ref = ref

    def inventory(self) -> dict:
        return {"artifact_root_configured": True, "campaigns": []}

    def select_verified_ref(self, *, harness, model_alias, gateway):
        if harness.value == "codex" and model_alias == "gpt-5.5":
            return self.ref, "qualified"
        return None, "no verified D0 qualification matches current gateway route"


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
    # A Harness without a published record falls back to its declaration.
    assert descriptors["bladeai"]["code_execution"] == "none"


def test_runtime_preflight_uses_only_qualification_record_for_readiness(
    tmp_path: Path, monkeypatch
):
    record = tmp_path / "capabilities.json"
    record.write_text(json.dumps(_qualification_payload()), encoding="utf-8")
    gateway_config = tmp_path / "litellm.yaml"
    gateway_config.write_text(
        yaml.safe_dump(
                {
                    "model_list": [
                        {
                            "model_name": alias,
                            "litellm_params": {
                                "model": f"openai/{alias}",
                                "api_base": "http://127.0.0.1:4000/v1",
                                "api_key": "os.environ/UPSTREAM_API_KEY",
                            },
                        }
                        for alias in STAGE2_SUPPORTED_MODELS
                    ]
                }
            ),
        encoding="utf-8",
    )
    snapshot = GatewayConfigSnapshot.from_file(gateway_config, required_aliases=STAGE2_SUPPORTED_MODELS)
    system = object.__new__(Stage2System)
    system.config = SimpleNamespace(
        repo_root=Path(__file__).resolve().parents[1],
        llm_base_url="http://127.0.0.1:4000/v1",
        llm_api_key="runtime-key",
        gateway_config_file=snapshot.config_path,
        gateway_snapshot=snapshot,
    )
    system.d0_gate = D0Gate(_d0_ref(snapshot))
    system._gateway_models = lambda: (set(STAGE2_SUPPORTED_MODELS), None)
    system._model_probe_runner = lambda _snapshot, _aliases: {
        "schemaVersion": "resiliencebenchmark.model_probe/v1",
        "issues": [],
        "models": [
            {
                "alias": alias,
                "overallStatus": "supported",
                "probes": [{"check": "openai_chat_completions_basic", "status": "supported"}],
            }
            for alias in STAGE2_SUPPORTED_MODELS
        ],
    }
    system._probe_cache_ttl_seconds = 300.0
    system._probe_lock = __import__("threading").Lock()
    system._gateway_readiness = {}
    monkeypatch.setenv("STAGE2_HARNESS_CAPABILITIES_FILE", str(record))
    monkeypatch.delenv("RESBENCH_CODEX_EVAL_BIN", raising=False)
    monkeypatch.delenv("STAGE2_BLADEAI_PYTHON", raising=False)
    monkeypatch.setattr("stage2_service.runtime_factory.shutil.which", lambda _: None)

    system.refresh_gateway_readiness()
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
    assert all(all(models[model] is True for model in STAGE2_SUPPORTED_MODELS) for models in result["model_matrix"].values())
    d0_selection = result["d0"]["selection_by_harness_model"]
    assert d0_selection["codex"]["gpt-5.5"]["verified"] is True
    assert d0_selection["codex"]["gpt-5.5"]["qualification_ref"][
        "campaign_id"
    ] == "d0-otel-accounting-gpt-5-5-codex"
    assert d0_selection["bladeai"]["gpt-5.5"]["verified"] is False


def test_failed_qualification_cannot_be_overridden_by_capability_claim(tmp_path: Path):
    payload = _qualification_payload()
    payload["harnesses"]["deepseek-harness"]["qualification"]["status"] = "failed"
    record = tmp_path / "capabilities.json"
    record.write_text(json.dumps(payload), encoding="utf-8")

    descriptors, source = harness_capabilities_from_qualification(record)

    assert descriptors["deepseek-harness"]["qualification_passed"] is False
    assert source["harnesses"]["deepseek-harness"]["status"] == "qualification_not_passed"
    # The failed record claimed platform_sandbox; the loader keeps the declared "none".
    assert descriptors["deepseek-harness"]["code_execution"] == "none"
