from __future__ import annotations

import json
import pytest

from stage2_service.bladeai_shim import BladeShim, parse_create


@pytest.mark.parametrize("argv", [["version"], ["create", "k8s", "pod-network", "delay", "--help"]])
def test_real_shim_entrypoint_renders_version_and_help_without_tool_calls(tmp_path, monkeypatch, capsys, argv):
    from stage2_service.bladeai_shim import main

    monkeypatch.delenv("RESBENCH_TRIAL_NAMESPACE", raising=False)
    monkeypatch.delenv("RESBENCH_BLADE_SHIM_STATE_FILE", raising=False)
    monkeypatch.delenv("RESBENCH_BLADEAI_K8S_MCP_SSE_URL", raising=False)
    monkeypatch.delenv("RESBENCH_BLADEAI_CHAOS_CONTROL_MCP_SSE_URL", raising=False)
    monkeypatch.delenv("RESBENCH_MCP_TOKEN", raising=False)
    assert main(argv) == 0
    output = capsys.readouterr()
    assert output.out and output.err == ""
    assert list(tmp_path.iterdir()) == []


class _Tools:
    def __init__(self, *, status_response=None):
        self.calls = []
        self.status_response = status_response

    def call(self, tool, arguments):
        self.calls.append((tool, dict(arguments)))
        if tool == "k8s_get_resource":
            return {"ok": True, "controller_call_id": "controller-k8s-1", "object": {"metadata": {"uid": "bound-pod-uid"}}}
        if tool == "chaos_validate_plan":
            return {"ok": True, "controller_call_id": "controller-validate-1"}
        if tool == "chaos_create_experiment":
            return {
                "ok": True,
                "controller_call_id": "controller-create-1",
                "cleanup_handle": "cleanup-1",
                "operation_id": "cleanup-1",
            }
        if tool == "chaos_destroy_experiment":
            return {
                "ok": True,
                "controller_call_id": "controller-destroy-1",
                "cleanup_handle": arguments["cleanup_handle"],
                "operation_id": arguments["cleanup_handle"],
            }
        if tool == "chaos_operation_status":
            if self.status_response is not None:
                return self.status_response
            return {
                "ok": True, "controller_call_id": "controller-status-1", "operation_outcome": "applied", "state": "running",
                "operation_id": arguments["operation_id"],
                "namespace": "otel-demo", "target_name": "cart-abc",
            }
        raise AssertionError(tool)


def test_create_reads_target_uid_through_controlled_read_then_validates_and_creates(tmp_path):
    tools = _Tools()
    shim = BladeShim(tools, namespace="otel-demo", state_file=tmp_path / "shim-state.json")
    code, stdout, stderr = shim.run(
        ["create", "k8s", "pod-network", "delay", "--names", "cart-abc", "--timeout", "60", "--time", "30"]
    )

    assert (code, stderr) == (0, "")
    payload = json.loads(stdout)
    assert "via_shim" not in stdout
    blade_uid = payload["result"]
    assert len(blade_uid) == 36
    assert payload["_resbench"]["schema_version"] == "resbench.blade_shim_evidence.v1"
    assert payload["_resbench"]["operation_id"] == "cleanup-1"
    assert payload["_resbench"]["target_uid"] == "bound-pod-uid"
    assert {
        (call["tool"], call.get("controller_call_id"))
        for call in payload["_resbench"]["mcp_calls"]
    } == {
        ("k8s_get_resource", "controller-k8s-1"),
        ("chaos_validate_plan", "controller-validate-1"),
        ("chaos_create_experiment", "controller-create-1"),
    }
    state = json.loads((tmp_path / "shim-state.json").read_text())
    assert state[blade_uid]["cleanup_handle"] == "cleanup-1"
    assert state[blade_uid]["target_uid"] == "bound-pod-uid"
    assert state[blade_uid]["fault_type"] == "network-delay"
    evidence_rows = (tmp_path / "shim-state.evidence.jsonl").read_text().splitlines()
    assert json.loads(evidence_rows[0]) == payload["_resbench"]
    assert [name for name, _ in tools.calls] == ["k8s_get_resource", "chaos_validate_plan", "chaos_create_experiment"]
    assert tools.calls[1][1] == {
        "namespace": "otel-demo",
        "target_name": "cart-abc",
        "target_uid": "bound-pod-uid",
        "fault_type": "network-delay",
        "duration_seconds": 60,
        "intensity": {"delay_ms": 30},
    }


def test_shim_never_delegates_to_native_blade_for_unknown_or_scope_broadened_commands(tmp_path):
    tools = _Tools()
    for argv in (
        ["create", "k8s", "node-cpu", "fullload", "--names", "node-1", "--timeout", "60"],
        ["create", "k8s", "pod-network", "delay", "--names", "cart", "--namespace", "other", "--timeout", "60"],
        ["exec", "anything"],
    ):
        code, _, stderr = BladeShim(tools, namespace="otel-demo", state_file=tmp_path / "shim-state.json").run(argv)
        assert code == 1
        assert stderr.startswith("Error:")
    assert tools.calls == []


def test_parser_refuses_unit_conversion_and_duration_overrun(tmp_path):
    for argv in (
        ["create", "k8s", "pod-cpu", "load", "--names", "cart", "--timeout", "1m"],
        ["create", "k8s", "pod-cpu", "load", "--names", "cart", "--timeout", "1201"],
    ):
        code, _, _ = BladeShim(_Tools(), namespace="otel-demo", state_file=tmp_path / "shim-state.json").run(argv)
        assert code == 1


def test_status_query_and_destroy_resolve_only_a_trial_owned_uuid(tmp_path):
    tools = _Tools()
    kubeconfig = str(tmp_path / "bladeai-home" / "proxy.kubeconfig")
    shim = BladeShim(
        tools,
        namespace="otel-demo",
        state_file=tmp_path / "shim-state.json",
        kubeconfig_path=kubeconfig,
    )
    code, created, _ = shim.run(
        ["create", "k8s", "pod-network", "delay", "--names", "cart-abc", "--timeout", "60", "--time", "20"]
    )
    assert code == 0
    blade_uid = json.loads(created)["result"]
    code, status, _ = shim.run(["status", blade_uid])
    assert code == 0
    assert json.loads(status)["result"]["Status"] == "Success"
    code, status_by_flag, _ = shim.run(["status", "--uid", blade_uid, "--kubeconfig", kubeconfig])
    assert code == 0
    status_payload = json.loads(status_by_flag)
    assert status_payload["result"]["Uid"] == blade_uid
    assert status_payload["result"]["operation_id"] == "cleanup-1"
    assert status_payload["result"]["operation_outcome"] == "applied"
    assert status_payload["result"]["ledger_state"] == "running"
    assert status_payload["_resbench"]["mcp_calls"][0]["controller_call_id"] == "controller-status-1"
    assert status_payload["_resbench"]["mcp_calls"][0]["operation_outcome"] == "applied"
    code, query, _ = shim.run(["query", "k8s", "create", blade_uid, "--kubeconfig", kubeconfig])
    assert code == 0
    query_payload = json.loads(query)
    assert query_payload["result"]["operation_id"] == "cleanup-1"
    assert query_payload["result"]["operation_outcome"] == "applied"
    assert query_payload["result"]["ledger_state"] == "running"
    assert query_payload["result"]["statuses"][0]["identifier"] == "otel-demo/cart-abc"
    assert query_payload["result"]["statuses"][0]["uid"] == "bound-pod-uid"
    assert query_payload["result"]["statuses"][0]["operation_outcome"] == "applied"
    code, listed, _ = shim.run(["status", "--type", "create", "--kubeconfig", kubeconfig])
    assert code == 0
    assert json.loads(listed)["result"][0]["Uid"] == blade_uid
    assert tools.calls[-1] == ("chaos_operation_status", {"operation_id": "cleanup-1"})
    code, destroyed, _ = shim.run(["destroy", blade_uid, "--kubeconfig", kubeconfig])
    assert code == 0
    destroyed_payload = json.loads(destroyed)
    assert destroyed_payload["result"] == blade_uid
    assert destroyed_payload["_resbench"]["mcp_calls"][0]["tool"] == "chaos_destroy_experiment"
    assert destroyed_payload["_resbench"]["mcp_calls"][0]["controller_call_id"] == "controller-destroy-1"
    assert tools.calls[-1] == ("chaos_destroy_experiment", {"cleanup_handle": "cleanup-1"})
    assert json.loads((tmp_path / "shim-state.json").read_text())[blade_uid]["status"] == "Destroyed"


def test_status_and_destroy_reject_unknown_or_non_uuid_handles(tmp_path):
    shim = BladeShim(_Tools(), namespace="otel-demo", state_file=tmp_path / "shim-state.json")
    for argv in (["status", "--uid", "cleanup-1"], ["destroy", "00000000-0000-0000-0000-000000000000"]):
        assert shim.run(argv)[0] == 1


def test_parse_create_maps_native_numeric_flags_to_canonical_controller_values():
    parsed = parse_create(
        ["create", "k8s", "pod-cpu", "fullload", "--names", "cart", "--timeout", "42", "--cpu-percent", "80"],
        namespace="otel-demo",
        max_duration_seconds=1200,
    )
    assert parsed.duration_seconds == 42
    assert parsed.intensity == {"cpu_percent": 80}


def test_network_drop_maps_to_controller_loss_without_percent_units():
    parsed = parse_create(
        ["create", "k8s", "pod-network", "drop", "--names", "cart", "--timeout", "42", "--interface", "eth0"],
        namespace="otel-demo",
        max_duration_seconds=1200,
    )
    assert parsed.intensity == {"loss_percent": 100}


def test_failed_create_and_unknown_status_do_not_persist_or_report_success(tmp_path):
    class DenyingTools(_Tools):
        def call(self, tool, arguments):
            if tool == "k8s_get_resource":
                return {"object": {"metadata": {"uid": "bound-pod-uid"}}}
            if tool == "chaos_validate_plan":
                return {"ok": False, "reason": "not approved"}
            raise AssertionError(tool)

    state_file = tmp_path / "shim-state.json"
    shim = BladeShim(DenyingTools(), namespace="otel-demo", state_file=state_file)
    code, _, stderr = shim.run(
        ["create", "k8s", "pod-cpu", "fullload", "--names", "cart", "--timeout", "60", "--cpu-percent", "80"]
    )
    assert code == 1
    assert "denied" in stderr
    assert not state_file.exists()

    tools = _Tools(status_response={"ok": True, "state": "unknown"})
    shim = BladeShim(tools, namespace="otel-demo", state_file=state_file)
    code, created, _ = shim.run(
        ["create", "k8s", "pod-cpu", "fullload", "--names", "cart-abc", "--timeout", "60", "--cpu-percent", "80"]
    )
    assert code == 0
    blade_uid = json.loads(created)["result"]
    code, status, _ = shim.run(["status", blade_uid])
    assert code == 0
    assert json.loads(status)["result"]["Status"] == "Error"
    code, query, _ = shim.run(["query", "k8s", "create", blade_uid])
    assert code == 0
    queried = json.loads(query)["result"]["statuses"][0]
    assert queried["state"] == "Error"
    assert queried["identifier"] == "otel-demo/cart-abc"
    assert queried["uid"] == "bound-pod-uid"


def test_d6_unknown_create_persists_operation_id_for_status_query_and_destroy(tmp_path):
    class UnknownCreateTools(_Tools):
        def call(self, tool, arguments):
            self.calls.append((tool, dict(arguments)))
            if tool == "k8s_get_resource":
                return {"ok": True, "controller_call_id": "controller-k8s-1", "object": {"metadata": {"uid": "bound-pod-uid"}}}
            if tool == "chaos_validate_plan":
                return {"ok": True, "controller_call_id": "controller-validate-1"}
            if tool == "chaos_create_experiment":
                return {
                    "ok": False,
                    "controller_call_id": "controller-create-unknown",
                    "error": {
                        "code": "OPERATION_OUTCOME_UNKNOWN",
                        "details": {
                            "operation_id": "cleanup-d6-unknown",
                            "cleanup_handle": "cleanup-d6-unknown",
                        },
                    },
                }
            if tool == "chaos_operation_status":
                return {
                    "ok": True,
                    "controller_call_id": "controller-status-1",
                    "operation_id": arguments["operation_id"],
                    "cleanup_handle": arguments["operation_id"],
                    "operation_outcome": "absent",
                    "state": "operation_outcome_unknown",
                }
            if tool == "chaos_destroy_experiment":
                return {"ok": True, "controller_call_id": "controller-destroy-1"}
            raise AssertionError(tool)

    shim = BladeShim(UnknownCreateTools(), namespace="otel-demo", state_file=tmp_path / "shim-state.json")
    code, stdout, stderr = shim.run(
        ["create", "k8s", "pod-network", "drop", "--names", "cart-abc", "--timeout", "60", "--interface", "eth0"]
    )
    assert (code, stderr) == (1, "")
    payload = json.loads(stdout)
    assert payload["code"] == 54000
    assert payload["success"] is False
    assert payload["error"]["code"] == "OPERATION_OUTCOME_UNKNOWN"
    blade_uid = payload["result"]["uid"]
    assert payload["result"]["operation_id"] == "cleanup-d6-unknown"
    assert payload["_resbench"]["operation_id"] == "cleanup-d6-unknown"
    create_call = payload["_resbench"]["mcp_calls"][-1]
    assert create_call["controller_call_id"] == "controller-create-unknown"
    assert create_call["error"]["code"] == "OPERATION_OUTCOME_UNKNOWN"
    assert create_call["operation_id"] == "cleanup-d6-unknown"
    state = json.loads((tmp_path / "shim-state.json").read_text())
    assert state[blade_uid]["cleanup_handle"] == "cleanup-d6-unknown"

    code, status, stderr = shim.run(["status", blade_uid])
    assert (code, stderr) == (0, "")
    status_payload = json.loads(status)
    assert status_payload["result"]["Status"] == "Absent"
    assert status_payload["result"]["operation_outcome"] == "absent"
    assert status_payload["result"]["ledger_state"] == "operation_outcome_unknown"

    code, query, stderr = shim.run(["query", "k8s", "create", blade_uid])
    assert (code, stderr) == (0, "")
    query_payload = json.loads(query)
    assert query_payload["result"]["operation_outcome"] == "absent"
    assert query_payload["result"]["ledger_state"] == "operation_outcome_unknown"
    assert query_payload["result"]["statuses"][0]["state"] == "Absent"

    code, destroyed, stderr = shim.run(["destroy", blade_uid])
    assert (code, stderr) == (0, "")
    assert json.loads(destroyed)["_resbench"]["operation_id"] == "cleanup-d6-unknown"


def test_network_delay_accepts_only_inert_loopback_kubeconfig_and_fixed_interface():
    parsed = parse_create(
        ["create", "k8s", "pod-network", "delay", "--names", "cart", "--timeout", "42", "--time", "30", "--interface", "eth0", "--kubeconfig", "/loopback/kubeconfig"],
        namespace="otel-demo", max_duration_seconds=1200, expected_kubeconfig="/loopback/kubeconfig",
    )
    assert parsed.intensity == {"delay_ms": 30}
    for value in ("/etc/kubernetes/admin.conf",):
        with pytest.raises(Exception):
            parse_create(["create", "k8s", "pod-network", "delay", "--names", "cart", "--timeout", "42", "--time", "30", "--kubeconfig", value], namespace="otel-demo", max_duration_seconds=1200)
