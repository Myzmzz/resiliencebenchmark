"""WP8 publication from synthetic artifacts; not live qualification evidence."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from stage2_service.capability_preflight import harness_capabilities_from_qualification
from stage2_service.capability_qualification import evaluate_wp8_artifacts, publish_capabilities
from stage2_service.contracts import TrialRuntimeContext
from stage2_service.gateway_config import GatewayConfigSnapshot
from tests.test_bladeai_qualification import (
    CLEANUP_HANDLE, MODEL, TARGET, TRIAL_ID, _events, _platform_event, _recovery, _report,
)


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    path.chmod(0o600)


def _artifacts(tmp_path):
    root = tmp_path / "artifacts"
    trial = root / "trial"
    native = trial
    config = tmp_path / "gateway.json"
    _write(config, {"model_list": [{"model_name": MODEL, "litellm_params": {
        "model": f"openai/{MODEL}", "api_base": "https://provider.example/v1",
        "api_key": "os.environ/PROBE_KEY",
    }}]})
    gateway = GatewayConfigSnapshot.from_file(config, required_aliases=(MODEL,))
    result = {
        "status": "completed", "interaction_mode": "guided", "assisted": False,
        "assistance_events": [], "decision": "continue", "clarification_request": None,
        "effect_assessment": "unverified", "recovery_assessment": "verified",
        "missing_conditions": ["effect not scored in this qualification"],
        "retry_summary": {"operation_id": CLEANUP_HANDLE, "attempts": 1,
                          "bounded": True, "outcome_reconciled": True},
        "recovery_trigger": {"condition": "qualification complete", "observed": True,
                             "triggered_by_agent": True},
        "strategy_selection": {"fault_type": "network-delay", "rationale": "canary",
                               "evidence_summary": "controlled canary"},
        "suspected_defect": "not assessed", "evidence": [], "actions_taken": [],
        "recovery_check": "verified", "remaining_risk": "none observed",
    }
    events = [replace(e, payload={**e.payload, "arguments": {"result": result}})
              if e.event_type == "ToolCall" and e.payload.get("call_id") == "submit" else e
              for e in _events()]
    events.extend([
        _platform_event(27, "ToolCall", {"source": "native", "replayed": False,
                        "call_id": "native-status", "tool": "bladeai.blade_status", "arguments": {}}),
        _platform_event(28, "ToolResult", {"source": "native", "replayed": False,
                        "call_id": "native-status", "status": "completed", "payload": {"code": 200}}),
    ])
    rows = []
    for e in events:
        if e.event_type in {"ToolCall", "ToolResult", "Checkpoint"}:
            row = {"event_type": e.event_type, **e.payload}
            if row.get("source") in {"native", "mcp_server"}:
                row["replayed"] = False
            rows.append(row)
    native.mkdir(parents=True)
    (native / "canonical-events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    report = _report()
    report = report.model_copy(update={"artifact_refs": tuple(
        f"trial/{name}" for name in ("canonical-events.jsonl", "gateway-requests.json",
                                     "bladeai-launch.json", "bladeai-shim-evidence.json")
    ), "final_output": {
        **report.final_output, "platform_events": [e.as_dict() for e in events],
        "gateway_route": gateway.route(MODEL), "gateway_config_sha256": gateway.config_sha256,
        "agent_result": result,
    }})
    recovery = _recovery()
    recovery = recovery.model_copy(update={"recovery_attribution": {
        **recovery.recovery_attribution, "trial_id": TRIAL_ID,
    }})
    runtime = TrialRuntimeContext(trial_id=TRIAL_ID, episode_id="fixture", target=TARGET,
                                  main_fault={}, cleanup_handle=CLEANUP_HANDLE, baseline_capability="b" * 40)
    _write(trial / "harness-report.json", report.model_dump(mode="json"))
    _write(trial / "runtime-context.json", runtime.model_dump(mode="json"))
    _write(trial / "recovery.json", recovery.model_dump(mode="json"))
    _write(trial / "canary-evidence.json", {"trial_id": TRIAL_ID, "pod": {
        "kind": "Pod", "metadata": {"namespace": TARGET.namespace, "name": TARGET.name,
        "uid": TARGET.uid, "labels": {"resiliencebenchmark.io/qualification": "bladeai-wp8"}},
    }})
    _write(native / "bladeai-launch.json", report.final_output["bladeai_launch"])
    _write(native / "bladeai-shim-evidence.json", report.final_output["bladeai_shim_evidence"])
    _write(native / "gateway-requests.json", [{"trial_id": TRIAL_ID, "harness": "bladeai",
        "model_alias": MODEL, "gateway_config_sha256": gateway.config_sha256,
        "request_id": "gw-req-1", "outcome": "received"}])
    refs = [str(p.relative_to(root)) for p in sorted(root.rglob("*")) if p.is_file()]
    return root, gateway, refs


def test_wp8_publication_recomputes_artifacts_and_only_grants_base_execution(tmp_path):
    root, gateway, refs = _artifacts(tmp_path)
    record = evaluate_wp8_artifacts(refs, artifact_root=root, gateway=gateway)
    assert record["passed"] is True, record["failure_reasons"]
    path = tmp_path / "wp8.json"
    _write(path, record)
    output = tmp_path / "capabilities.json"
    publish_capabilities([path], artifact_root=root, output=output, gateway=gateway)
    capability = harness_capabilities_from_qualification(output)[0]["bladeai"]
    assert capability["qualification_passed"] is True
    assert capability["execution_model"] == "stream"
    assert capability["code_execution"] == "none"


@pytest.mark.parametrize("corruption", ["checks", "shim_file", "native_stream", "canary_uid", "result"])
def test_wp8_cannot_publish_forged_or_detached_proof(tmp_path, corruption):
    root, gateway, refs = _artifacts(tmp_path)
    record = evaluate_wp8_artifacts(refs, artifact_root=root, gateway=gateway)
    if corruption == "checks":
        record["checks"]["create_destroy_bound"] = False
    else:
        name = {"shim_file": "trial/bladeai-shim-evidence.json",
                "native_stream": "trial/canonical-events.jsonl",
                "canary_uid": "trial/canary-evidence.json",
                "result": "trial/harness-report.json"}[corruption]
        path = root / name
        if corruption == "native_stream":
            path.write_text('{}\n')
        elif corruption == "shim_file":
            _write(path, [])
        else:
            payload = json.loads(path.read_text())
            if corruption == "canary_uid":
                payload["pod"]["metadata"]["uid"] = "another-pod"
            else:
                payload["final_output"]["agent_result"] = {"status": "completed"}
            _write(path, payload)
    path = tmp_path / "wp8.json"
    _write(path, record)
    output = tmp_path / "capabilities.json"
    output.write_text("existing publication")
    with pytest.raises(ValueError):
        publish_capabilities([path], artifact_root=root, output=output, gateway=gateway)
    assert output.read_text() == "existing publication"
