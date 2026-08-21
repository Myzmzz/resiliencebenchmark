from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest
import yaml

from scripts import materialize_sources


def git(args, *, cwd: Path, capture_stdout: bool = True, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=check,
        stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def create_git_repo(path: Path, content: str = "ok\n") -> tuple[str, str]:
    path.mkdir(parents=True)
    git(["init"], cwd=path, capture_stdout=False)
    git(["config", "user.email", "test@example.invalid"], cwd=path, capture_stdout=False)
    git(["config", "user.name", "Source Test"], cwd=path, capture_stdout=False)
    (path / "README.md").write_text(content, encoding="utf-8")
    git(["add", "README.md"], cwd=path, capture_stdout=False)
    git(["commit", "-m", "initial"], cwd=path, capture_stdout=False)
    commit = git(["rev-parse", "HEAD"], cwd=path).stdout.decode("utf-8").strip()
    archive = git(["archive", "HEAD"], cwd=path).stdout
    digest = materialize_sources.hashlib.sha256(archive).hexdigest()
    return commit, digest


def write_lockfile(path: Path, locks: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "resiliencebenchmark.io/v1alpha1",
                "kind": "SourceLockSet",
                "spec": {"locks": locks},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def lock_for(repo: Path, *, lock_id: str, application: str, component: str | None = None) -> dict:
    commit, digest = create_git_repo(repo)
    lock = {
        "id": lock_id,
        "application": application,
        "remote": repo.as_uri(),
        "commit": commit,
        "archiveSha256": digest,
        "agentMountPath": f"/workspace/src/{lock_id}",
        "runtimeMappingStatus": "test",
    }
    if component:
        lock["component"] = component
    return lock


def test_materialize_clones_detached_commit_and_writes_redacted_manifest(tmp_path):
    lockfile = tmp_path / "environment" / "shared" / "source-locks.yaml"
    lock = lock_for(tmp_path / "remote", lock_id="demo-source", application="demo")
    write_lockfile(lockfile, [lock])

    destination = tmp_path / "sources"
    output = tmp_path / "artifacts" / "manifest.json"

    manifest = materialize_sources.materialize_sources(
        lockfile=lockfile,
        destination=destination,
        output=output,
    )

    checkout = destination / "demo-source"
    assert git(["rev-parse", "HEAD"], cwd=checkout).stdout.decode("utf-8").strip() == lock["commit"]
    assert git(["symbolic-ref", "-q", "HEAD"], cwd=checkout, capture_stdout=False, check=False).returncode != 0
    assert git(["status", "--porcelain", "--untracked-files=normal"], cwd=checkout).stdout == b""
    assert manifest["spec"]["sources"][0]["destinationRef"] == "${RESBENCH_SOURCE_ROOT}/demo-source"
    assert manifest["spec"]["sources"][0]["remoteRef"] == "<redacted-local-or-private-remote>"
    assert str(tmp_path) not in output.read_text(encoding="utf-8")


def test_https_remote_ref_removes_userinfo_query_and_fragment_credentials():
    remote = "https://private-user:private-token@example.com/org/repo.git;params?access_token=secret#private"

    redacted = materialize_sources.redacted_remote_ref(remote)

    assert redacted == "https://example.com/org/repo.git"
    assert "private-user" not in redacted
    assert "private-token" not in redacted
    assert "access_token" not in redacted


def test_git_failure_redacts_remote_from_command_and_stderr(monkeypatch):
    remote = "https://private-user:private-token@example.com/org/repo.git?access_token=secret"

    def fail_run(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout=b"", stderr=f"fatal: unable to access {remote}".encode())

    monkeypatch.setattr(materialize_sources.subprocess, "run", fail_run)

    with pytest.raises(materialize_sources.SourceMaterializationError) as exc:
        materialize_sources.run_git(["clone", remote, "/tmp/destination"])

    message = str(exc.value)
    assert "private-user" not in message
    assert "private-token" not in message
    assert "access_token" not in message
    assert "https://example.com/org/repo.git" in message


def test_refuses_existing_non_empty_destination_by_default(tmp_path):
    lockfile = tmp_path / "environment" / "shared" / "source-locks.yaml"
    lock = lock_for(tmp_path / "remote", lock_id="demo-source", application="demo")
    write_lockfile(lockfile, [lock])
    destination = tmp_path / "sources"
    target = destination / "demo-source"
    target.mkdir(parents=True)
    (target / "existing.txt").write_text("already here\n", encoding="utf-8")

    with pytest.raises(materialize_sources.SourceMaterializationError, match="already exists and is not empty"):
        materialize_sources.materialize_sources(
            lockfile=lockfile,
            destination=destination,
            output=tmp_path / "manifest.json",
        )


def test_verify_existing_is_offline_and_rejects_dirty_worktree(tmp_path):
    lockfile = tmp_path / "environment" / "shared" / "source-locks.yaml"
    lock = lock_for(tmp_path / "remote", lock_id="demo-source", application="demo")
    write_lockfile(lockfile, [lock])
    destination = tmp_path / "sources"
    output = tmp_path / "manifest.json"
    materialize_sources.materialize_sources(lockfile=lockfile, destination=destination, output=output)

    offline_lock = dict(lock)
    offline_lock["remote"] = "file:///this/path/does/not/exist"
    write_lockfile(lockfile, [offline_lock])
    manifest = materialize_sources.materialize_sources(
        lockfile=lockfile,
        destination=destination,
        output=output,
        verify_existing=True,
    )
    assert manifest["spec"]["mode"] == "verify-existing"

    (destination / "demo-source" / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(materialize_sources.SourceMaterializationError, match="uncommitted or untracked changes"):
        materialize_sources.materialize_sources(
            lockfile=lockfile,
            destination=destination,
            output=output,
            verify_existing=True,
        )


def test_filters_by_application_and_component(tmp_path):
    lockfile = tmp_path / "environment" / "shared" / "source-locks.yaml"
    locks = [
        lock_for(tmp_path / "remote-a", lock_id="orders", application="sock-shop", component="orders"),
        lock_for(tmp_path / "remote-b", lock_id="catalogue", application="sock-shop", component="catalogue"),
        lock_for(tmp_path / "remote-c", lock_id="ticket", application="train-ticket"),
    ]
    write_lockfile(lockfile, locks)

    manifest = materialize_sources.materialize_sources(
        lockfile=lockfile,
        destination=tmp_path / "sources",
        output=tmp_path / "manifest.json",
        applications={"sock-shop"},
        components={"orders"},
    )

    ids = [source["id"] for source in manifest["spec"]["sources"]]
    assert ids == ["orders"]
    assert (tmp_path / "sources" / "orders").exists()
    assert not (tmp_path / "sources" / "catalogue").exists()
    assert not (tmp_path / "sources" / "ticket").exists()


def test_archive_sha_mismatch_is_rejected(tmp_path):
    lockfile = tmp_path / "environment" / "shared" / "source-locks.yaml"
    lock = lock_for(tmp_path / "remote", lock_id="demo-source", application="demo")
    lock["archiveSha256"] = "0" * 64
    write_lockfile(lockfile, [lock])

    with pytest.raises(materialize_sources.SourceMaterializationError, match="archive SHA-256 mismatch"):
        materialize_sources.materialize_sources(
            lockfile=lockfile,
            destination=tmp_path / "sources",
            output=tmp_path / "manifest.json",
        )


def test_cli_requires_explicit_destination():
    parser = materialize_sources.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_manifest_output_is_json(tmp_path):
    lockfile = tmp_path / "environment" / "shared" / "source-locks.yaml"
    lock = lock_for(tmp_path / "remote", lock_id="demo-source", application="demo")
    write_lockfile(lockfile, [lock])
    output = tmp_path / "manifest.json"

    materialize_sources.run(["--lockfile", str(lockfile), "--destination", str(tmp_path / "sources"), "--output", str(output)])

    parsed = json.loads(output.read_text(encoding="utf-8"))
    assert parsed["kind"] == "SourceMaterializationManifest"
