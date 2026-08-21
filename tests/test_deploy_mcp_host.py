from __future__ import annotations

import json
import io
import subprocess
import tarfile
from pathlib import Path

from scripts import deploy_mcp_host as deploy


REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, stdin, timeout_seconds):
        self.calls.append({"argv": argv, "stdin": stdin, "timeout": timeout_seconds})
        return deploy.CommandResult(returncode=0, stdout=b"", stderr=b"")


def runtime_env(tmp_path: Path) -> dict[str, str]:
    identity = tmp_path / "id_ed25519"
    known_hosts = tmp_path / "known_hosts"
    identity.write_text("identity", encoding="utf-8")
    known_hosts.write_text("host key", encoding="utf-8")
    return {
        deploy.HOST_ENV: "benchmark.example",
        deploy.IDENTITY_ENV: str(identity),
        deploy.KNOWN_HOSTS_ENV: str(known_hosts),
        deploy.EXPECTED_HEAD_ENV: local_head(),
    }


def local_head() -> str:
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return completed.stdout.decode("utf-8").strip()


def test_dry_run_does_not_call_runner_or_expose_runtime_values(tmp_path):
    fake = FakeRunner()
    env = runtime_env(tmp_path)

    report = deploy.run_deploy(env, execute=False, runner=fake)
    encoded = json.dumps(report)

    assert report["mode"] == "dry-run"
    assert report["status"] == "not_executed"
    assert set(report["services"]) == set(deploy.SERVICE_NAMES)
    assert fake.calls == []
    assert env[deploy.HOST_ENV] not in encoded
    assert env[deploy.IDENTITY_ENV] not in encoded
    assert env[deploy.KNOWN_HOSTS_ENV] not in encoded
    assert env[deploy.EXPECTED_HEAD_ENV] not in encoded


def test_execute_rejects_unsafe_hosts_and_does_not_connect(tmp_path):
    fake = FakeRunner()
    env = runtime_env(tmp_path)
    env[deploy.HOST_ENV] = "root@host.example:22"

    report = deploy.run_deploy(env, execute=True, runner=fake, repo_root=REPO_ROOT)

    assert report["status"] == "blocked"
    assert any(issue["field"] == deploy.HOST_ENV for issue in report["issues"])
    assert fake.calls == []


def test_execute_requires_absolute_existing_identity_and_known_hosts(tmp_path):
    env = {
        deploy.HOST_ENV: "benchmark.example",
        deploy.IDENTITY_ENV: "relative-id",
        deploy.KNOWN_HOSTS_ENV: str(tmp_path / "missing_known_hosts"),
        deploy.EXPECTED_HEAD_ENV: "b" * 40,
    }

    report = deploy.run_deploy(env, execute=True, runner=FakeRunner(), repo_root=REPO_ROOT)

    assert report["status"] == "blocked"
    messages = [issue["message"] for issue in report["issues"]]
    assert "path must be absolute" in messages
    assert "path must reference an existing file" in messages


def test_execute_rejects_unbounded_timeout_before_connecting(tmp_path):
    fake = FakeRunner()

    report = deploy.run_deploy(runtime_env(tmp_path), execute=True, runner=fake, timeout_seconds=601)

    assert report["status"] == "blocked"
    assert report["issues"][0]["field"] == "timeout"
    assert fake.calls == []


def test_execute_uses_fixed_ssh_argv_stdin_install_and_read_only_verify(tmp_path):
    env = runtime_env(tmp_path)
    fake = FakeRunner()
    head = env[deploy.EXPECTED_HEAD_ENV]

    report = deploy.run_deploy(env, execute=True, runner=fake, repo_root=REPO_ROOT, timeout_seconds=17)

    assert report["status"] == "installed"
    assert [step["name"] for step in report["steps"]] == [
        "ssh_preflight_tools",
        "materialize_pinned_release",
        "install_mcp_host_units_and_sources_from_stdin",
        "verify_mcp_unit_files",
    ]
    assert len(fake.calls) == 4
    first_argv = fake.calls[0]["argv"]
    assert first_argv[:10] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={env[deploy.KNOWN_HOSTS_ENV]}",
        "-i",
    ]
    assert first_argv[10] == env[deploy.IDENTITY_ENV]
    assert first_argv[11] == f"root@{env[deploy.HOST_ENV]}"
    assert first_argv[12:] == ["/bin/sh", "-c", deploy.preflight_command()]
    assert fake.calls[1]["argv"][12:] == ["/bin/sh", "-s", "--", head]
    with tarfile.open(fileobj=io.BytesIO(fake.calls[1]["stdin"]), mode="r:*") as archive:
        member = archive.getmember(".resbench-head")
        content = archive.extractfile(member).read().decode("utf-8")
    assert content == f"{head}\n"
    assert fake.calls[2]["argv"][12:] == [
        "/bin/bash",
        "-s",
        "--",
        "--repo",
        deploy.REMOTE_REPO,
        "--head",
        head,
        "--materialize-sources",
    ]
    assert b"systemctl daemon-reload" in fake.calls[2]["stdin"]
    assert b"materialize_sources.py" in fake.calls[2]["stdin"]
    assert fake.calls[3]["argv"][12:] == ["/bin/sh", "-c", deploy.verify_units_command()]
    assert "systemctl cat" in deploy.verify_units_command()
    assert "resbench-mcp-k8s-ro-sse.service" in deploy.verify_units_command()
    assert "resbench-mcp-chaos-control-sse.service" not in deploy.verify_units_command()
    assert "command -v runuser" in deploy.preflight_command()
    assert "command -v git" in deploy.preflight_command()
    assert all(call["timeout"] == 17 for call in fake.calls)


def test_execute_can_skip_source_materialization_but_reports_not_ready(tmp_path):
    env = runtime_env(tmp_path)
    fake = FakeRunner()

    report = deploy.run_deploy(
        env,
        execute=True,
        runner=fake,
        repo_root=REPO_ROOT,
        materialize_sources=False,
    )

    assert report["status"] == "installed_source_not_ready"
    assert report["sourceMaterialization"] == {"requested": False, "status": "not_ready_skipped"}
    assert "--materialize-sources" not in fake.calls[2]["argv"]


def test_execute_redacts_host_paths_and_token_like_output(tmp_path):
    env = runtime_env(tmp_path)

    class FailingRunner(FakeRunner):
        def __call__(self, argv, stdin, timeout_seconds):
            self.calls.append({"argv": argv, "stdin": stdin, "timeout": timeout_seconds})
            return deploy.CommandResult(
                returncode=255,
                stdout=f"{env[deploy.HOST_ENV]} {deploy.REMOTE_REPO} Bearer abcdefghijklmnop".encode("utf-8"),
                stderr=f"{env[deploy.IDENTITY_ENV]} {env[deploy.KNOWN_HOSTS_ENV]} {deploy.REMOTE_ENV_DIR}".encode(
                    "utf-8"
                ),
            )

    report = deploy.run_deploy(env, execute=True, runner=FailingRunner(), repo_root=REPO_ROOT)
    encoded = json.dumps(report)

    assert report["status"] == "failed"
    assert env[deploy.HOST_ENV] not in encoded
    assert env[deploy.IDENTITY_ENV] not in encoded
    assert env[deploy.KNOWN_HOSTS_ENV] not in encoded
    assert deploy.REMOTE_REPO not in encoded
    assert deploy.REMOTE_ENV_DIR not in encoded
    assert "abcdefghijklmnop" not in encoded


def test_cli_defaults_to_dry_run(monkeypatch, capsys):
    monkeypatch.setenv(deploy.HOST_ENV, "benchmark.example")
    monkeypatch.setenv(deploy.IDENTITY_ENV, "/secret/id")
    monkeypatch.setenv(deploy.KNOWN_HOSTS_ENV, "/secret/known_hosts")
    monkeypatch.setenv(deploy.EXPECTED_HEAD_ENV, local_head())

    rc = deploy.main([], runner=FakeRunner())

    captured = capsys.readouterr()
    assert rc == 0
    assert "benchmark.example" not in captured.out
    assert "/secret/id" not in captured.out
    assert "/secret/known_hosts" not in captured.out
    report = json.loads(captured.out)
    assert report["mode"] == "dry-run"


def test_cli_skip_source_materialization_marks_not_ready(monkeypatch, tmp_path, capsys):
    env = runtime_env(tmp_path)
    monkeypatch.setenv(deploy.HOST_ENV, env[deploy.HOST_ENV])
    monkeypatch.setenv(deploy.IDENTITY_ENV, env[deploy.IDENTITY_ENV])
    monkeypatch.setenv(deploy.KNOWN_HOSTS_ENV, env[deploy.KNOWN_HOSTS_ENV])
    monkeypatch.setenv(deploy.EXPECTED_HEAD_ENV, env[deploy.EXPECTED_HEAD_ENV])

    rc = deploy.main(["--execute", "--skip-source-materialization"], runner=FakeRunner())

    captured = capsys.readouterr()
    assert rc == 0
    report = json.loads(captured.out)
    assert report["status"] == "installed_source_not_ready"
    assert report["sourceMaterialization"]["status"] == "not_ready_skipped"


def test_host_install_script_has_valid_bash_syntax():
    completed = subprocess.run(
        ["bash", "-n", str(REPO_ROOT / deploy.INSTALL_SCRIPT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")


def test_install_script_uses_resbench_head_and_private_ledger_permissions():
    text = (REPO_ROOT / deploy.INSTALL_SCRIPT).read_text(encoding="utf-8")

    assert "--materialize-sources" in text
    assert ".resbench-head" in text
    assert "repository path is missing .git" not in text
    assert 'tr -d \'\\n\' < "$REPO_DIR/.resbench-head"' in text
    assert 'install -d -m 0750 -o resbench-source-ro -g resbench-source-ro "$SOURCE_STATE_DIR" "$SOURCE_ROOT"' in text
    assert 'runuser -u resbench-source-ro -- "${SOURCE_ARGS[@]}"' in text
    assert "SOURCE_ARGS+=(--verify-existing)" in text
    assert 'install -d -m 0700 -o resbench-chaos-control -g resbench-chaos-control "$ACTIVE_LEDGER_DIR"' in text
    assert 'install -d -m 0700 -o resbench-chaos-control -g resbench-chaos-control "$BASELINE_LEDGER_DIR"' in text
    assert 'rm -rf -- "$SOURCE_ROOT"' not in text
    assert 'rm -r -- "$SOURCE_ROOT"' not in text


def test_materialize_release_command_fails_closed_for_unmanaged_repo_paths():
    command = deploy.materialize_release_command()

    assert 'if [ -e "$repo" ] && [ ! -L "$repo" ]; then' in command
    assert "exit 42" in command
    assert 'target="$(readlink "$repo")"' in command
    assert '"$releases"/*) ;;' in command
    assert "exit 43" in command
    assert 'mv -Tf "$link_tmp" "$repo"' in command


def test_systemd_units_are_explicit_loopback_hardened_and_non_enabling():
    unit_dir = REPO_ROOT / "environment/mcp/host/systemd"
    expected = {
        "resbench-mcp-k8s-ro.service": ("18081", "/mcp", "streamable-http", "k8s_ro.env", "resbench-k8s-ro"),
        "resbench-mcp-telemetry-ro.service": ("18082", "/mcp", "streamable-http", "telemetry_ro.env", "resbench-telemetry-ro"),
        "resbench-mcp-source-ro.service": ("18083", "/mcp", "streamable-http", "source_ro.env", "resbench-source-ro"),
        "resbench-mcp-chaos-control.service": ("18084", "/mcp", "streamable-http", "chaos_control.env", "resbench-chaos-control"),
        "resbench-mcp-k8s-ro-sse.service": ("18181", "/sse", "sse", "k8s_ro.env", "resbench-k8s-ro"),
        "resbench-mcp-telemetry-ro-sse.service": ("18182", "/sse", "sse", "telemetry_ro.env", "resbench-telemetry-ro"),
        "resbench-mcp-source-ro-sse.service": ("18183", "/sse", "sse", "source_ro.env", "resbench-source-ro"),
    }
    for unit_name, (port, path, transport, env_file, run_user) in expected.items():
        text = (unit_dir / unit_name).read_text(encoding="utf-8")
        assert f"User={run_user}" in text
        assert f"Group={run_user}" in text
        assert "WorkingDirectory=/opt/resiliencebenchmark/repo" in text
        assert f"EnvironmentFile=/etc/resiliencebenchmark/mcp/{env_file}" in text
        assert "Restart=on-failure" in text
        assert f"RESBENCH_MCP_TRANSPORT={transport}" in text
        assert "RESBENCH_MCP_HTTP_HOST=127.0.0.1" in text
        assert f"RESBENCH_MCP_HTTP_PORT={port}" in text
        assert f"RESBENCH_MCP_HTTP_PATH={path}" in text
        assert "ProtectSystem=strict" in text
        assert "NoNewPrivileges=yes" in text
        assert "systemctl enable" not in text
        assert "systemctl start" not in text
    assert not (unit_dir / "resbench-mcp-chaos-control-sse.service").exists()


def test_chaos_unit_allows_only_active_ledger_write_and_baseline_read_only():
    text = (REPO_ROOT / "environment/mcp/host/systemd/resbench-mcp-chaos-control.service").read_text(
        encoding="utf-8"
    )

    read_write_lines = [line for line in text.splitlines() if line.startswith("ReadWritePaths=")]
    assert read_write_lines == ["ReadWritePaths=/var/lib/resiliencebenchmark/chaos-control/active"]
    assert "-/var/lib/resiliencebenchmark/chaos-control/baseline" in text


def test_env_examples_do_not_contain_endpoint_or_secret_values():
    env_dir = REPO_ROOT / "environment/mcp/host/env"
    for path in env_dir.glob("*.env.example"):
        text = path.read_text(encoding="utf-8")
        assert "http://" not in text
        assert "https://" not in text
        assert "Bearer " not in text
        assert "sk-" not in text
        for line in text.splitlines():
            if not line or line.startswith("#"):
                continue
            key, value = line.split("=", 1)
            if key.endswith("_URL") or key == "RESBENCH_MCP_TOKEN":
                assert value == ""
    telemetry = (env_dir / "telemetry_ro.env.example").read_text(encoding="utf-8")
    assert "RESBENCH_TELEMETRY_ALLOW_RAW_QUERIES=false" in telemetry
    source = (env_dir / "source_ro.env.example").read_text(encoding="utf-8")
    assert "RESBENCH_SOURCE_ROOT=/opt/resiliencebenchmark/sources" in source


def test_host_readme_documents_baseline_writer_contract():
    text = (REPO_ROOT / "environment/mcp/host/README.md").read_text(encoding="utf-8")

    assert "atomically writing baseline ledger files" in text
    assert "0600" in text
    assert "ordinary directory or unmanaged symlink fails closed" in text
    assert "BladeAI" in text
    assert "v0.6.2 verifier" in text
    assert "same per-Episode env files, token" in text
    assert "no `chaos_control` SSE unit" in text
    assert "/opt/resiliencebenchmark/sources" in text
    assert "--verify-existing" in text
    assert "--skip-source-materialization" in text
