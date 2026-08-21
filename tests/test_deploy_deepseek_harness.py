import io
import json
from pathlib import Path
import shutil
import tarfile

from scripts import deploy_deepseek_harness as deploy


REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, stdin, timeout_seconds):
        self.calls.append({"argv": argv, "stdin": stdin, "timeout": timeout_seconds})
        return deploy.CommandResult(returncode=0, stdout=b"0.1.0-rc.7\n", stderr=b"")


def write_install_bundle(repo_root: Path) -> None:
    script_path = repo_root / deploy.INSTALL_SCRIPT
    lock_dir = repo_root / deploy.RUNTIME_LOCK_DIR
    script_path.parent.mkdir(parents=True, exist_ok=True)
    lock_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO_ROOT / deploy.INSTALL_SCRIPT, script_path)
    shutil.copyfile(REPO_ROOT / deploy.RUNTIME_LOCK_DIR / "package.json", lock_dir / "package.json")
    shutil.copyfile(REPO_ROOT / deploy.RUNTIME_LOCK_DIR / "package-lock.json", lock_dir / "package-lock.json")


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


def test_runtime_lock_pins_every_dsh_package_and_integrity():
    lock_path = REPO_ROOT / deploy.RUNTIME_LOCK_DIR / "package-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))

    assert deploy.hashlib.sha256(lock_path.read_bytes()).hexdigest() == deploy.RUNTIME_LOCK_SHA256
    dsh_entries = [
        info
        for path, info in lock["packages"].items()
        if deploy.re.search(r"(^|/)node_modules/@deepseek-ai/dsh(?:$|[^/]+$)", path)
    ]
    assert dsh_entries
    assert all(info["version"] == deploy.PACKAGE_VERSION for info in dsh_entries)
    assert all(
        info.get("integrity")
        for path, info in lock["packages"].items()
        if path and info.get("resolved") and not info.get("link")
    )


def test_dry_run_does_not_call_runner_or_expose_env_values(tmp_path):
    fake = FakeRunner()
    env = runtime_env(tmp_path)

    report = deploy.run_deploy(env, execute=False, runner=fake)
    encoded = json.dumps(report)

    assert report["mode"] == "dry-run"
    assert report["status"] == "not_executed"
    assert report["pinnedPackage"]["spec"] == deploy.PACKAGE_SPEC
    assert report["pinnedPackage"]["runtimeLockSha256"] == deploy.RUNTIME_LOCK_SHA256
    assert fake.calls == []
    assert env[deploy.HOST_ENV] not in encoded
    assert env[deploy.IDENTITY_ENV] not in encoded
    assert env[deploy.KNOWN_HOSTS_ENV] not in encoded


def test_execute_rejects_unsafe_hosts_and_does_not_connect(tmp_path):
    repo_root = tmp_path / "repo"
    write_install_bundle(repo_root)
    fake = FakeRunner()
    env = runtime_env(tmp_path)
    env[deploy.HOST_ENV] = "root@host.example:22;touch x"

    report = deploy.run_deploy(env, execute=True, runner=fake, repo_root=repo_root)

    assert report["status"] == "blocked"
    assert any(issue["field"] == deploy.HOST_ENV for issue in report["issues"])
    assert fake.calls == []


def test_execute_requires_absolute_existing_identity_and_known_hosts(tmp_path):
    repo_root = tmp_path / "repo"
    write_install_bundle(repo_root)
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


def test_execute_uses_fixed_ssh_argv_and_stdin_locked_install_bundle(tmp_path):
    repo_root = tmp_path / "repo"
    write_install_bundle(repo_root)
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
    install_argv = fake.calls[1]["argv"][12:]
    assert len(install_argv) == 1
    assert install_argv[0].startswith("/bin/sh -c ")
    assert "--lock-dir" in install_argv[0]
    with tarfile.open(fileobj=io.BytesIO(fake.calls[1]["stdin"]), mode="r:") as archive:
        assert sorted(archive.getnames()) == [
            "install.sh",
            "runtime-lock/package-lock.json",
            "runtime-lock/package.json",
        ]
        lock_bytes = archive.extractfile("runtime-lock/package-lock.json").read()
    assert deploy.hashlib.sha256(lock_bytes).hexdigest() == deploy.RUNTIME_LOCK_SHA256
    assert fake.calls[2]["argv"][12:] == [deploy.DSH_BINARY, "--version"]
    assert fake.calls[3]["argv"][12:] == ["test", "-s", deploy.DEPENDENCY_TREE_FILE]
    assert all(call["timeout"] == 17 for call in fake.calls)


def test_execute_redacts_errors_from_failed_runner(tmp_path):
    repo_root = tmp_path / "repo"
    write_install_bundle(repo_root)
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


def test_execute_rejects_tampered_runtime_lock_before_ssh(tmp_path):
    repo_root = tmp_path / "repo"
    write_install_bundle(repo_root)
    lock_path = repo_root / deploy.RUNTIME_LOCK_DIR / "package-lock.json"
    lock_path.write_text(lock_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    fake = FakeRunner()

    report = deploy.run_deploy(runtime_env(tmp_path), execute=True, runner=fake, repo_root=repo_root)

    assert report["status"] == "blocked"
    assert any("SHA-256" in issue["message"] for issue in report["issues"])
    assert fake.calls == []


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
