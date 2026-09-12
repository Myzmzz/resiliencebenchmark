"""Published capabilities require real, matching Controller evidence artifacts."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from mcp_servers.http_runtime import TOOL_DISABLED_RESPONSE
from stage2_service.capability_preflight import harness_capabilities_from_qualification
from stage2_service.capability_qualification import SUBSTITUTION_QUALIFICATION_TYPE, publish_capabilities
from stage2_service.channel_qualification import CHANNEL_QUALIFICATION_MODE, EXPECTED_HINT_BODY, SUBSTITUTION_CHECKS
from stage2_service.gateway_config import GatewayConfigSnapshot


CHECKS = (
    "mcp_read_verified", "confirmation_roundtrip_verified", "consult_roundtrip_verified",
    "notice_ack_verified", "result_submission_verified", "gateway_evidence_verified",
    "tool_evidence_verified",
)


def gateway(tmp_path: Path) -> GatewayConfigSnapshot:
    path = tmp_path / "gateway.json"
    if not path.exists():
        path.write_text(json.dumps({"model_list": [{"model_name": "gpt-5.5", "litellm_params": {
            "model": "openai/gpt-5.5", "api_base": "https://provider.example/v1",
            "api_key": "os.environ/PROBE_KEY",
        }}]}))
    return GatewayConfigSnapshot.from_file(path, required_aliases=("gpt-5.5",))


def qualification(tmp_path: Path, harness: str = "codex", *, replayed: bool = False) -> Path:
    root = tmp_path / "artifacts"
    native = root / harness
    native.mkdir(parents=True)
    trial = f"campaign-qualification-{harness}"
    snapshot = gateway(tmp_path)
    version = snapshot.config_sha256
    (native / "gateway-requests.json").write_text(json.dumps([{
        "trial_id": trial, "harness": harness, "model_alias": "gpt-5.5",
        "gateway_config_sha256": version, "request_id": "request-1", "outcome": "received",
    }]))
    tools = ["k8s_ro.k8s_list_resources", "telemetry_ro.telemetry_prom_metric_range",
             "harness_channel.harness_confirm", "harness_channel.harness_consult",
             "harness_channel.harness_poll_notices", "harness_channel.harness_submit_result"]
    events, exchanges = [], []
    for source in ("native", "mcp_server"):
        for index, tool in enumerate(tools):
            call_id = f"{source}-{index}"
            events.extend([
                {"event_type": "ToolCall", "source": source, "replayed": replayed if source == "native" else False,
                 "call_id": call_id, "tool": tool},
                {"event_type": "ToolResult", "source": source, "replayed": replayed if source == "native" else False,
                 "call_id": call_id, "status": "completed", "payload": {"ok": True}},
            ])
            if source == "mcp_server":
                exchanges.append({"call_id": call_id, "tool": tool, "status": "completed"})
    (native / "canonical-events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events))
    record = {
        "schema_version": "stage2-channel-qualification.v1",
        "qualification_type": "BASE_CHANNEL_QUALIFICATION",
        "qualification_profile": "BASE_CHANNEL_QUALIFICATION",
        "harness": harness, "model": "gpt-5.5", "trial_id": trial,
        "status": "passed", "passed": True, "failure_reasons": [], "cleanup_errors": [],
        "harness_report_status": "completed", "base_checks": {key: True for key in CHECKS},
        "ordered_exchanges": exchanges,
        "gateway_route": snapshot.route("gpt-5.5"),
        "gateway_config_sha256": version,
        "gateway_sidecar_evidence": {"verified": True, "request_ids": ["request-1"],
                                     "artifact_ref": "gateway-requests.json"},
        "artifact_refs": [f"{harness}/gateway-requests.json", f"{harness}/canonical-events.jsonl"],
    }
    path = tmp_path / f"base-{harness}.json"
    path.write_text(json.dumps(record))
    return path


def substitution_qualification(tmp_path: Path, harness: str = "codex", *, replayed: bool = False) -> Path:
    """Write a passed WP11 substitution record whose own Trial archive matches it."""
    root = tmp_path / "artifacts"
    archive = root / f"{harness}-substitution"
    archive.mkdir(parents=True)
    trial = f"campaign-substitution-{harness}"
    snapshot = gateway(tmp_path)
    version = snapshot.config_sha256
    (archive / "gateway-requests.json").write_text(json.dumps([{
        "trial_id": trial, "harness": harness, "model_alias": "gpt-5.5",
        "gateway_config_sha256": version, "request_id": "request-2", "outcome": "received",
    }]))
    steps = [
        ("telemetry_ro.telemetry_prom_metric_range", "failed", TOOL_DISABLED_RESPONSE),
        ("harness_channel.harness_consult", "completed", EXPECTED_HINT_BODY),
        ("coroot_ro.coroot_metrics_range", "completed", {"ok": True, "data": []}),
        ("code_sandbox.run_python", "completed", {"ok": True, "stdout": "4\n", "exit_code": 0, "truncated": False}),
        ("harness_channel.harness_poll_notices", "completed", {"ok": True}),
        ("harness_channel.harness_submit_result", "completed", {"ok": True, "valid": True}),
    ]
    events, exchanges = [], []
    for source in ("native", "mcp_server"):
        for index, (tool, status, payload) in enumerate(steps):
            call_id = f"{source}-{index}"
            mode = replayed if source == "native" else False
            events.extend([
                {"event_type": "ToolCall", "source": source, "replayed": mode, "call_id": call_id, "tool": tool},
                {"event_type": "ToolResult", "source": source, "replayed": mode, "call_id": call_id,
                 "status": status, "payload": payload},
            ])
            if source == "mcp_server":
                exchanges.append({"call_sequence": 10 * index + 1, "result_sequence": 10 * index + 3,
                                  "call_id": call_id, "tool": tool, "arguments": {},
                                  "status": status, "payload": payload})
    (archive / "canonical-events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events))
    sandbox = next(item for item in exchanges if item["tool"] == "code_sandbox.run_python")
    record = {
        "schema_version": "stage2-channel-qualification.v1",
        "qualification_type": CHANNEL_QUALIFICATION_MODE,
        "qualification_profile": CHANNEL_QUALIFICATION_MODE,
        "harness": harness, "model": "gpt-5.5", "trial_id": trial,
        "status": "passed", "passed": True, "failure_reasons": [], "cleanup_errors": [],
        "harness_report_status": "completed", "scored_as_d7": False,
        "telemetry_denial_body": TOOL_DISABLED_RESPONSE, "hint_body": EXPECTED_HINT_BODY,
        "substitution_checks": {key: True for key in SUBSTITUTION_CHECKS},
        "observed_capability_evidence": {"sandbox_run": {
            "call_id": sandbox["call_id"], "call_sequence": sandbox["call_sequence"],
            "result_sequence": sandbox["result_sequence"], "ledger_sequence": sandbox["call_sequence"] + 1,
            "status": "completed", "exit_code": 0, "truncated": False, "code_sha256": "b" * 64,
        }},
        "ordered_exchanges": exchanges,
        "gateway_route": snapshot.route("gpt-5.5"),
        "gateway_config_sha256": version,
        "gateway_sidecar_evidence": {"verified": True, "request_ids": ["request-2"],
                                     "artifact_ref": "gateway-requests.json"},
        "artifact_refs": [f"{harness}-substitution/gateway-requests.json",
                          f"{harness}-substitution/canonical-events.jsonl"],
    }
    path = tmp_path / f"substitution-{harness}.json"
    path.write_text(json.dumps(record))
    return path


def test_single_harness_publication_is_consumable_without_substitution_services(tmp_path):
    record = qualification(tmp_path)
    output = tmp_path / "private" / "capabilities.json"
    publish_capabilities([record], artifact_root=tmp_path / "artifacts", output=output, gateway=gateway(tmp_path))
    descriptors, _source = harness_capabilities_from_qualification(output)
    assert descriptors["codex"]["qualification_passed"] is True
    assert descriptors["codex"]["feedback_channels"] == ["in_band_mcp"]
    assert descriptors["codex"]["code_execution"] == "none"
    assert descriptors["codex"]["supports_resume"] is False
    assert descriptors["claude-code"]["qualification_passed"] is False
    assert output.stat().st_mode & 0o777 == 0o600


def test_deepseek_post_hoc_is_derived_from_observed_native_records(tmp_path):
    record = qualification(tmp_path, "deepseek-harness", replayed=True)
    output = tmp_path / "private" / "capabilities.json"
    publish_capabilities([record], artifact_root=tmp_path / "artifacts", output=output, gateway=gateway(tmp_path))
    descriptors, _ = harness_capabilities_from_qualification(output)
    assert descriptors["deepseek-harness"]["execution_model"] == "post_hoc"
    assert descriptors["deepseek-harness"]["post_hoc_trace"] is True
    assert descriptors["deepseek-harness"]["streams_tool_results"] is False


def test_bladeai_base_record_does_not_claim_wp8_full_chain(tmp_path):
    record = qualification(tmp_path, "bladeai")
    output = tmp_path / "private" / "capabilities.json"
    publish_capabilities([record], artifact_root=tmp_path / "artifacts", output=output, gateway=gateway(tmp_path))
    payload = json.loads(output.read_text())
    assert payload["harnesses"]["bladeai"]["qualification"]["reason"] == "bladeai_full_chain_qualification_required"
    descriptors, _ = harness_capabilities_from_qualification(output)
    assert descriptors["bladeai"]["qualification_passed"] is False


@pytest.mark.parametrize("corruption", ["missing_check", "failed", "gateway_mismatch", "missing_native_result", "wrong_type", "duplicate_harness", "stale_gateway", "forged_route"])
def test_invalid_proof_never_overwrites_existing_publication(tmp_path, corruption):
    record = qualification(tmp_path)
    value = json.loads(record.read_text())
    if corruption == "missing_check":
        value["base_checks"]["notice_ack_verified"] = False
    elif corruption == "failed":
        value["passed"] = False
    elif corruption == "gateway_mismatch":
        value["trial_id"] = "different-trial"
    elif corruption == "missing_native_result":
        native = tmp_path / "artifacts/codex/canonical-events.jsonl"
        native.write_text(native.read_text().splitlines()[0] + "\n")
    elif corruption == "wrong_type":
        value["qualification_type"] = "CHANNEL_QUALIFICATION"
    elif corruption == "stale_gateway":
        value["gateway_config_sha256"] = "0" * 64
    elif corruption == "forged_route":
        value["gateway_route"]["api_base_host"] = "different.example"
    record.write_text(json.dumps(value))
    output = tmp_path / "capabilities.json"
    output.write_text("existing publication")
    with pytest.raises(ValueError):
        publish_capabilities([record, record] if corruption == "duplicate_harness" else [record],
                             artifact_root=tmp_path / "artifacts", output=output, gateway=gateway(tmp_path))
    assert output.read_text() == "existing publication"


def test_artifact_symlink_is_rejected(tmp_path):
    record = qualification(tmp_path)
    path = tmp_path / "artifacts/codex/gateway-requests.json"
    actual = tmp_path / "elsewhere.json"
    path.rename(actual)
    path.symlink_to(actual)
    with pytest.raises(ValueError):
        publish_capabilities([record], artifact_root=tmp_path / "artifacts", output=tmp_path / "caps.json", gateway=gateway(tmp_path))


@pytest.mark.parametrize("tool", ["unknown.fake_read", "chaos_control.chaos_create_experiment", "chaos_control.chaos_destroy_experiment"])
def test_arbitrary_or_mutating_native_pair_cannot_establish_base_qualification(tmp_path, tool):
    record = qualification(tmp_path)
    path = tmp_path / "artifacts/codex/canonical-events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        if row["event_type"] == "ToolCall" and row["source"] == "native":
            row["tool"] = tool
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(ValueError):
        publish_capabilities([record], artifact_root=tmp_path / "artifacts", output=tmp_path / "caps.json", gateway=gateway(tmp_path))


def test_record_exchange_must_match_controller_artifact(tmp_path):
    record = qualification(tmp_path)
    value = json.loads(record.read_text())
    value["ordered_exchanges"][0]["call_id"] = "unrelated-call"
    record.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        publish_capabilities([record], artifact_root=tmp_path / "artifacts", output=tmp_path / "caps.json", gateway=gateway(tmp_path))


def test_existing_scoped_workload_tool_is_valid_base_telemetry(tmp_path):
    # This is the production tool observed in the first real base-channel run.
    record = qualification(tmp_path)
    value = json.loads(record.read_text())
    for exchange in value["ordered_exchanges"]:
        if exchange["tool"] == "telemetry_ro.telemetry_prom_metric_range":
            exchange["tool"] = "telemetry_ro.telemetry_workload_current"
    record.write_text(json.dumps(value))
    canonical = tmp_path / "artifacts/codex/canonical-events.jsonl"
    canonical.write_text(canonical.read_text().replace("telemetry_ro.telemetry_prom_metric_range", "telemetry_ro.telemetry_workload_current"))
    output = tmp_path / "caps.json"
    publish_capabilities([record], artifact_root=tmp_path / "artifacts", output=output, gateway=gateway(tmp_path))
    assert harness_capabilities_from_qualification(output)[0]["codex"]["qualification_passed"] is True


@pytest.mark.parametrize("valid_config", [True, False])
def test_real_publish_cli_reports_success_or_structured_rejection(tmp_path, valid_config):
    record = qualification(tmp_path)
    config = tmp_path / "gateway.json"
    if not valid_config:
        config.write_text("not a configuration mapping")
    completed = subprocess.run([
        sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/publish_harness_capabilities.py"),
        "--record", str(record), "--artifact-root", str(tmp_path / "artifacts"),
        "--gateway-config", str(config), "--output", str(tmp_path / "caps.json"),
    ], text=True, capture_output=True, check=False)
    assert completed.returncode == (0 if valid_config else 1), completed.stderr
    assert json.loads(completed.stdout)["status"] == ("published" if valid_config else "rejected")
    assert "Traceback" not in completed.stderr


def test_publisher_is_in_controller_image_and_build_inputs():
    """The build inputs now follow the Dockerfile, so one COPY line covers both.

    This used to assert the same path twice: once in the Dockerfile and once in
    a hand-written list inside build_stage2_image.py. Keeping those two in step
    by hand is what shipped a stale script on 2026-09-11 (O18).
    """
    from stage2_service.image_manifest import copied_sources

    root = Path(__file__).resolve().parents[1]
    path = "scripts/publish_harness_capabilities.py"
    dockerfile = (root / "deploy/stage2/Dockerfile.runtime-overlay").read_text()

    assert f"COPY --chown=10001:10001 {path} /app/{path}" in dockerfile
    assert path in copied_sources(dockerfile)


def test_substitution_dispatch_type_is_the_evaluator_profile():
    assert SUBSTITUTION_QUALIFICATION_TYPE == CHANNEL_QUALIFICATION_MODE


@pytest.mark.parametrize(("harness", "replayed"), [
    ("codex", False), ("claude-code", False), ("deepseek-harness", True),
])
def test_verified_substitution_grants_platform_sandbox_through_preflight(tmp_path, harness, replayed):
    base = qualification(tmp_path, harness, replayed=replayed)
    substitution = substitution_qualification(tmp_path, harness, replayed=replayed)
    output = tmp_path / "private" / "capabilities.json"
    publish_capabilities([base, substitution], artifact_root=tmp_path / "artifacts", output=output,
                         gateway=gateway(tmp_path))
    entry = json.loads(output.read_text())["harnesses"][harness]
    assert entry["qualification"]["evidence_ref"] == str(base.resolve())
    assert entry["qualification"]["substitution_evidence_ref"] == str(substitution.resolve())
    descriptors, source = harness_capabilities_from_qualification(output)
    assert source["harnesses"][harness]["status"] == "qualified"
    assert descriptors[harness]["qualification_passed"] is True
    assert descriptors[harness]["feedback_channels"] == ["in_band_mcp"]
    assert descriptors[harness]["code_execution"] == "platform_sandbox"
    proof = descriptors[harness]["probe"]["substitution_qualification"]
    assert set(proof["substitution_checks"]) == SUBSTITUTION_CHECKS
    assert proof["sandbox_run"]["exit_code"] == 0
    others = {"codex", "claude-code", "deepseek-harness", "bladeai"} - {harness}
    assert all(descriptors[name]["code_execution"] == "none" for name in others)


@pytest.mark.parametrize(("gap", "reason"), [
    ("no_run_python_exchange", "sandbox run evidence is missing or belongs to another call"),
    ("legacy_record_without_positive_checks", "did not pass every required check"),
    ("sandbox_check_false", "did not pass every required check"),
    ("sandbox_run_evidence_missing", "sandbox run evidence is missing"),
    ("ledger_event_outside_call", "not inside its run_python call"),
    ("ledger_event_of_other_call", "belongs to another call"),
    ("failed_exit_in_archive", "does not show a successful sandbox execution"),
    ("truncated_output_in_record", "does not show a successful sandbox execution"),
    ("boolean_exit_code", "did not complete successfully"),
    ("native_run_python_missing", "Harness stream does not show"),
    ("failure_reason_recorded", "did not pass every required check"),
    ("profile_mismatch", "substitution channel qualification record is required"),
    ("stale_gateway", "different gateway configuration"),
    ("other_trial", "gateway receipt artifact does not verify"),
])
def test_substitution_without_verified_run_python_keeps_code_execution_none(tmp_path, gap, reason):
    base = qualification(tmp_path)
    output = tmp_path / "private" / "capabilities.json"
    publish_capabilities([base], artifact_root=tmp_path / "artifacts", output=output, gateway=gateway(tmp_path))
    published = output.read_text()
    substitution = substitution_qualification(tmp_path)
    value = json.loads(substitution.read_text())
    canonical = tmp_path / "artifacts/codex-substitution/canonical-events.jsonl"
    rows = [json.loads(line) for line in canonical.read_text().splitlines()]
    sandbox_calls = {row["call_id"] for row in rows if row.get("tool") == "code_sandbox.run_python"}
    run = value["observed_capability_evidence"]["sandbox_run"]
    recorded = next(item for item in value["ordered_exchanges"] if item["tool"] == "code_sandbox.run_python")
    if gap == "no_run_python_exchange":
        rows = [row for row in rows if row["call_id"] not in sandbox_calls]
        value["ordered_exchanges"].remove(recorded)
    elif gap == "legacy_record_without_positive_checks":
        del value["substitution_checks"], value["observed_capability_evidence"]
    elif gap == "sandbox_check_false":
        value["substitution_checks"]["sandbox_run_verified"] = False
    elif gap == "sandbox_run_evidence_missing":
        value["observed_capability_evidence"]["sandbox_run"] = None
    elif gap == "ledger_event_outside_call":
        run["ledger_sequence"] = recorded["result_sequence"] + 1
    elif gap == "ledger_event_of_other_call":
        run["call_id"] = next(item["call_id"] for item in value["ordered_exchanges"]
                              if item["tool"].startswith("coroot_ro."))
    elif gap == "failed_exit_in_archive":
        for row in rows:
            if row["source"] == "mcp_server" and row["event_type"] == "ToolResult" and row["call_id"] in sandbox_calls:
                row["payload"]["exit_code"] = 1
    elif gap == "truncated_output_in_record":
        recorded["payload"]["truncated"] = True
    elif gap == "boolean_exit_code":
        run["exit_code"] = False
    elif gap == "native_run_python_missing":
        rows = [row for row in rows if not (row["source"] == "native" and row["call_id"] in sandbox_calls)]
    elif gap == "failure_reason_recorded":
        value["failure_reasons"] = ["missing_completed_sandbox_run_evidence"]
    elif gap == "profile_mismatch":
        value["qualification_profile"] = "BASE_CHANNEL_QUALIFICATION"
    elif gap == "stale_gateway":
        value["gateway_config_sha256"] = "0" * 64
    elif gap == "other_trial":
        value["trial_id"] = "different-trial"
    canonical.write_text("".join(json.dumps(row) + "\n" for row in rows))
    substitution.write_text(json.dumps(value))
    # Each gap must be refused for its own reason, not by an incidental check.
    with pytest.raises(ValueError, match=reason):
        publish_capabilities([base, substitution], artifact_root=tmp_path / "artifacts", output=output,
                             gateway=gateway(tmp_path))
    assert output.read_text() == published
    assert harness_capabilities_from_qualification(output)[0]["codex"]["code_execution"] == "none"


def test_substitution_record_never_stands_alone_or_twice(tmp_path):
    base = qualification(tmp_path)
    claude_base = qualification(tmp_path, "claude-code")
    substitution = substitution_qualification(tmp_path)
    output = tmp_path / "capabilities.json"
    for records in ([substitution], [claude_base, substitution], [base, substitution, substitution]):
        with pytest.raises(ValueError):
            publish_capabilities(records, artifact_root=tmp_path / "artifacts", output=output,
                                 gateway=gateway(tmp_path))
    assert not output.exists()


def test_bladeai_never_receives_platform_sandbox(tmp_path):
    base = qualification(tmp_path, "bladeai")
    substitution = substitution_qualification(tmp_path, "bladeai")
    output = tmp_path / "private" / "capabilities.json"
    with pytest.raises(ValueError, match="BladeAI"):
        publish_capabilities([base, substitution], artifact_root=tmp_path / "artifacts", output=output,
                             gateway=gateway(tmp_path))
    assert not output.exists()
    publish_capabilities([base], artifact_root=tmp_path / "artifacts", output=output, gateway=gateway(tmp_path))
    assert harness_capabilities_from_qualification(output)[0]["bladeai"]["code_execution"] == "none"
