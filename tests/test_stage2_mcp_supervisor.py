from __future__ import annotations

from stage2_service import mcp_supervisor
from stage2_service.contracts import HarnessKind, McpServerName
from stage2_service.mcp_supervisor import McpSupervisor


class Process:
    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_supervisor_starts_only_profile_selected_servers(monkeypatch, tmp_path):
    processes = []

    def fake_popen(argv, **kwargs):
        process = Process(argv, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(mcp_supervisor, "_port_open", lambda _port: False)
    monkeypatch.setattr(mcp_supervisor, "_wait_process_port", lambda *_a, **_k: None)
    monkeypatch.setattr(mcp_supervisor.subprocess, "Popen", fake_popen)
    runtime = McpSupervisor(private_root=tmp_path, base_environment={})

    environment = runtime.start_trial(
        trial_id="campaign-1234567890abcdef-codex-static",
        harness=HarnessKind.CODEX,
        token="x" * 48,
        token_state_files={
            "k8s_ro": "/private/k8s.token",
            "source_ro": "/private/source.token",
        },
        servers=(McpServerName.K8S_RO, McpServerName.SOURCE_RO),
    )

    assert [process.argv[-1] for process in processes] == [
        "mcp_servers.k8s_ro",
        "mcp_servers.source_ro",
    ]
    assert set(environment) == {
        "RESBENCH_K8S_MCP_URL",
        "RESBENCH_BLADEAI_K8S_MCP_SSE_URL",
        "RESBENCH_SOURCE_MCP_URL",
        "RESBENCH_BLADEAI_SOURCE_MCP_SSE_URL",
    }
    assert environment["RESBENCH_K8S_MCP_URL"].endswith(":18081/mcp")
    assert all(
        process.kwargs["env"]["RESBENCH_MCP_TRANSPORT"] == "streamable-http"
        for process in processes
    )
    runtime.stop()


def test_supervisor_accepts_a_profile_with_no_mcp_servers(tmp_path):
    runtime = McpSupervisor(private_root=tmp_path, base_environment={})

    environment = runtime.start_trial(
        trial_id="campaign-1234567890abcdef-bladeai-native",
        harness=HarnessKind.BLADEAI,
        token="x" * 48,
        token_state_files={},
        servers=(),
    )

    assert environment == {}
    assert runtime.processes == []
