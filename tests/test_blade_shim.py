from __future__ import annotations

import json
import pytest

from stage2_service.bladeai_shim import BladeShim, parse_create


@pytest.mark.parametrize("argv", [["version"], ["create", "k8s", "pod-network", "delay", "--help"]])
def test_real_shim_entrypoint_renders_version_and_help_without_tool_calls(tmp_path, monkeypatch, capsys, argv):
    from stage2_service.bladeai_shim import main

    monkeypatch.setenv("RESBENCH_TRIAL_NAMESPACE", "otel-demo")
    monkeypatch.setenv("RESBENCH_BLADE_SHIM_STATE_FILE", str(tmp_path / "aliases.json"))
    monkeypatch.setenv("RESBENCH_BLADEAI_K8S_MCP_SSE_URL", "http://127.0.0.1:18181/sse")
    monkeypatch.setenv("RESBENCH_BLADEAI_CHAOS_CONTROL_MCP_SSE_URL", "http://127.0.0.1:18184/sse")
    monkeypatch.setenv("RESBENCH_MCP_TOKEN", "x" * 32)
    assert main(argv) == 0
    output = capsys.readouterr()
    assert output.out and output.err == ""
    assert not (tmp_path / "aliases.json").exists()


class _Tools:
    def __init__(self):
        self.calls = []

    def call(self, tool, arguments):
        self.calls.append((tool, dict(arguments)))
        if tool == "k8s_get_resource":
            return {"object": {"metadata": {"uid": "bound-pod-uid"}}}
        if tool == "chaos_validate_plan":
            return {"ok": True}
        if tool == "chaos_create_experiment":
            return {"ok": True, "cleanup_handle": "cleanup-1"}
        if tool == "chaos_destroy_experiment":
            return {"ok": True}
        if tool == "chaos_operation_status":
            return {
                "ok": True, "operation_outcome": "applied", "state": "running",
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
    blade_uid = json.loads(stdout)["result"]
    assert len(blade_uid) == 36
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
    shim = BladeShim(tools, namespace="otel-demo", state_file=tmp_path / "shim-state.json")
    code, created, _ = shim.run(
        ["create", "k8s", "pod-network", "delay", "--names", "cart-abc", "--timeout", "60", "--time", "20"]
    )
    assert code == 0
    blade_uid = json.loads(created)["result"]
    code, status, _ = shim.run(["status", "--uid", blade_uid, "--kubeconfig", "/loopback/kubeconfig"])
    assert code == 0
    assert json.loads(status)["result"]["Status"] == "Success"
    code, query, _ = shim.run(["query", "k8s", "create", blade_uid, "--kubeconfig", "/loopback/kubeconfig"])
    assert code == 0
    assert json.loads(query)["result"]["statuses"][0]["identifier"] == "otel-demo/cart-abc"
    code, _, _ = shim.run(["destroy", blade_uid])
    assert code == 0
    assert tools.calls[-1] == ("chaos_destroy_experiment", {"cleanup_handle": "cleanup-1"})


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


def test_network_delay_accepts_only_inert_loopback_kubeconfig_and_fixed_interface():
    parsed = parse_create(
        ["create", "k8s", "pod-network", "delay", "--names", "cart", "--timeout", "42", "--time", "30", "--interface", "eth0", "--kubeconfig", "/loopback/kubeconfig"],
        namespace="otel-demo", max_duration_seconds=1200, expected_kubeconfig="/loopback/kubeconfig",
    )
    assert parsed.intensity == {"delay_ms": 30}
    for value in ("/etc/kubernetes/admin.conf",):
        with pytest.raises(Exception):
            parse_create(["create", "k8s", "pod-network", "delay", "--names", "cart", "--timeout", "42", "--time", "30", "--kubeconfig", value], namespace="otel-demo", max_duration_seconds=1200)
