from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from scripts import qualify_remote_preparation as qualify


NOW = datetime(2026, 8, 21, 9, 0, 0, tzinfo=timezone.utc)


def runtime_env(tmp_path: Path) -> dict[str, str]:
    identity = tmp_path / "identity"
    known_hosts = tmp_path / "known_hosts"
    identity.write_text("identity", encoding="utf-8")
    known_hosts.write_text("host-key", encoding="utf-8")
    return {
        qualify.HOST_ENV: "benchmark.example",
        qualify.IDENTITY_ENV: str(identity),
        qualify.KNOWN_HOSTS_ENV: str(known_hosts),
        qualify.EXPECTED_HEAD_ENV: "a" * 40,
    }


class FakeRunner:
    def __init__(self, *, ready: str = "True", ssh_returncode: int = 0):
        self.ready = ready
        self.ssh_returncode = ssh_returncode
        self.calls = []

    def __call__(self, argv, stdin, timeout_seconds):
        self.calls.append((argv, stdin, timeout_seconds))
        if argv[0] == "kubectl" and "node" in argv:
            return qualify.CommandResult(
                0,
                json.dumps(
                    {"status": {"conditions": [{"type": "Ready", "status": self.ready}]}}
                ).encode(),
                b"",
            )
        if argv[0] == "kubectl" and "lease" in argv:
            return qualify.CommandResult(
                0,
                json.dumps({"spec": {"renewTime": "2026-08-21T08:59:30Z"}}).encode(),
                b"",
            )
        output = "\n".join(
            [
                f"repo_head={'a' * 40}",
                "root_dsh=0.1.0-rc.7",
                "resbench_dsh=0.1.0-rc.7",
                "source_summary=ok:11",
                "active_units=7",
                "listeners=7",
                "chaos_execute=false",
                "mem_available_kib=8388608",
            ]
        )
        return qualify.CommandResult(self.ssh_returncode, output.encode(), b"sensitive remote error")


def test_dry_run_never_calls_runner_or_exposes_runtime_values(tmp_path):
    env = runtime_env(tmp_path)
    runner = FakeRunner()

    report = qualify.run_qualification(
        env,
        execute=False,
        kubeconfig=None,
        node=None,
        runner=runner,
    )

    assert report["status"] == "not_executed"
    assert runner.calls == []
    encoded = json.dumps(report)
    assert env[qualify.HOST_ENV] not in encoded
    assert env[qualify.IDENTITY_ENV] not in encoded


def test_execute_qualifies_current_node_and_remote_runtime(tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    runner = FakeRunner()

    report = qualify.run_qualification(
        runtime_env(tmp_path),
        execute=True,
        kubeconfig=kubeconfig,
        node="tcse-v100-03",
        runner=runner,
        now=NOW,
    )

    assert report["status"] == "qualified"
    assert len(runner.calls) == 3
    assert all(item["passed"] for item in report["checks"])
    ssh_argv, ssh_stdin, _ = runner.calls[2]
    assert "BatchMode=yes" in ssh_argv
    assert "StrictHostKeyChecking=yes" in ssh_argv
    assert ssh_stdin == qualify.REMOTE_CHECK_SCRIPT.encode()


def test_stale_node_or_failed_ssh_fails_without_remote_output(tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")

    report = qualify.run_qualification(
        runtime_env(tmp_path),
        execute=True,
        kubeconfig=kubeconfig,
        node="tcse-v100-03",
        runner=FakeRunner(ready="Unknown", ssh_returncode=255),
        now=NOW,
    )

    assert report["status"] == "failed"
    checks = {item["name"]: item["passed"] for item in report["checks"]}
    assert checks["node_ready"] is False
    assert checks["ssh_and_remote_checks"] is False
    assert "sensitive remote error" not in json.dumps(report)


def test_input_validation_rejects_partial_head_and_unsafe_host(tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    env = runtime_env(tmp_path)
    env[qualify.HOST_ENV] = "root@host;bad"
    env[qualify.EXPECTED_HEAD_ENV] = "abc123"

    try:
        qualify.validate_inputs(env, kubeconfig, "tcse-v100-03")
    except qualify.QualificationError:
        pass
    else:
        raise AssertionError("unsafe recovery inputs were accepted")
