import json
from pathlib import Path

from scripts import deploy_deepseek_harness as deploy


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, stdin, timeout_seconds):
        self.calls.append({"argv": argv, "stdin": stdin, "timeout": timeout_seconds})
        return deploy.CommandResult(returncode=0, stdout=b"0.1.0-rc.7\n", stderr=b"")


def write_install_script(repo_root: Path) -> None:
    path = repo_root / deploy.INSTALL_SCRIPT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                f"npm install --prefix /opt/resiliencebenchmark/deepseek-harness --omit=dev {deploy.PACKAGE_SPEC}",
                "npm list --all --json",
            ]
        ),
        encoding="utf-8",
    )


def runtime_env(tmp_path: Path) -> dict[str, str]:
    identity = tmp_path / "id_ed25519"
    known_hosts = tmp_path / "known_hosts"
    identity.write_text("identity", encoding="utf-8")
    known_hosts.write_text("host key", encoding="utf-8")
    return {
        deploy.HOST_ENV: "benchmark.example",
        deploy.IDENTITY_ENV: str(identity),
        deploy.KNOWN_HOSTS_ENV: str(known_hosts),
    }


def test_dry_run_does_not_call_runner_or_expose_env_values(tmp_path):
    fake = FakeRunner()
    env = runtime_env(tmp_path)

    report = deploy.run_deploy(env, execute=False, runner=fake)
    encoded = json.dumps(report)

    assert report["mode"] == "dry-run"
    assert report["status"] == "not_executed"
    assert report["pinnedPackage"]["spec"] == deploy.PACKAGE_SPEC
    assert fake.calls == []
    assert env[deploy.HOST_ENV] not in encoded
    assert env[deploy.IDENTITY_ENV] not in encoded
    assert env[deploy.KNOWN_HOSTS_ENV] not in encoded


def test_execute_rejects_unsafe_hosts_and_does_not_connect(tmp_path):
    repo_root = tmp_path / "repo"
    write_install_script(repo_root)
    fake = FakeRunner()
    env = runtime_env(tmp_path)
    env[deploy.HOST_ENV] = "root@host.example:22;touch x"

    report = deploy.run_deploy(env, execute=True, runner=fake, repo_root=repo_root)

    assert report["status"] == "blocked"
    assert any(issue["field"] == deploy.HOST_ENV for issue in report["issues"])
    assert fake.calls == []


def test_execute_requires_absolute_existing_identity_and_known_hosts(tmp_path):
    repo_root = tmp_path / "repo"
    write_install_script(repo_root)
    env = {
        deploy.HOST_ENV: "benchmark.example",
        deploy.IDENTITY_ENV: "relative-id",
        deploy.KNOWN_HOSTS_ENV: str(tmp_path / "missing_known_hosts"),
    }

    report = deploy.run_deploy(env, execute=True, runner=FakeRunner(), repo_root=repo_root)

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


def test_execute_uses_fixed_ssh_argv_and_stdin_install_script(tmp_path):
    repo_root = tmp_path / "repo"
    write_install_script(repo_root)
    env = runtime_env(tmp_path)
    fake = FakeRunner()

    report = deploy.run_deploy(env, execute=True, runner=fake, repo_root=repo_root, timeout_seconds=17)

    assert report["status"] == "installed"
    assert [step["name"] for step in report["steps"]] == [
        "ssh_preflight_true",
        "install_deepseek_harness",
        "verify_dsh_version",
        "verify_dependency_tree_recorded",
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
    assert first_argv[12] == "true"
    assert fake.calls[1]["argv"][12:] == ["/bin/bash", "-s"]
    assert deploy.PACKAGE_SPEC.encode("utf-8") in fake.calls[1]["stdin"]
    assert fake.calls[2]["argv"][12:] == [deploy.DSH_BINARY, "--version"]
    assert fake.calls[3]["argv"][12:] == ["test", "-s", deploy.DEPENDENCY_TREE_FILE]
    assert all(call["timeout"] == 17 for call in fake.calls)


def test_execute_redacts_errors_from_failed_runner(tmp_path):
    repo_root = tmp_path / "repo"
    write_install_script(repo_root)
    env = runtime_env(tmp_path)

    class FailingRunner(FakeRunner):
        def __call__(self, argv, stdin, timeout_seconds):
            self.calls.append({"argv": argv, "stdin": stdin, "timeout": timeout_seconds})
            return deploy.CommandResult(
                returncode=255,
                stdout=f"host {env[deploy.HOST_ENV]} {deploy.INSTALL_ROOT}".encode("utf-8"),
                stderr=f"identity {env[deploy.IDENTITY_ENV]} known {env[deploy.KNOWN_HOSTS_ENV]}".encode("utf-8"),
            )

    report = deploy.run_deploy(env, execute=True, runner=FailingRunner(), repo_root=repo_root)
    encoded = json.dumps(report)

    assert report["status"] == "failed"
    assert env[deploy.HOST_ENV] not in encoded
    assert env[deploy.IDENTITY_ENV] not in encoded
    assert env[deploy.KNOWN_HOSTS_ENV] not in encoded
    assert deploy.INSTALL_ROOT not in encoded


def test_cli_defaults_to_dry_run(monkeypatch, capsys):
    monkeypatch.setenv(deploy.HOST_ENV, "benchmark.example")
    monkeypatch.setenv(deploy.IDENTITY_ENV, "/secret/id")
    monkeypatch.setenv(deploy.KNOWN_HOSTS_ENV, "/secret/known_hosts")

    rc = deploy.main([], runner=FakeRunner())

    captured = capsys.readouterr()
    assert rc == 0
    assert "benchmark.example" not in captured.out
    assert "/secret/id" not in captured.out
    assert "/secret/known_hosts" not in captured.out
    report = json.loads(captured.out)
    assert report["mode"] == "dry-run"
