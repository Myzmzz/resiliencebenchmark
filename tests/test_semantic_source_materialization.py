from __future__ import annotations

import sys

import pytest

from scripts.run_semantic_pipeline import (
    EXPECTED_OTEL_COMMIT,
    PipelineError,
    _clone_source_workspace,
    _materialize_source_workspace,
    _resolve_dulwich_revision,
    _subprocess_json,
    _write_source_cache_bundle,
)


class FakeCommit:
    type_name = b"commit"

    def __init__(self, oid: bytes):
        self.id = oid


class FakeTag:
    def __init__(self, oid: bytes):
        self.object = (b"commit", oid)


class FakeRepo:
    def __init__(self, refs, objects):
        self.refs = refs
        self._objects = objects

    def __getitem__(self, oid):
        return self._objects[oid]


def test_resolve_revision_peels_tag_to_expected_commit() -> None:
    commit = EXPECTED_OTEL_COMMIT.encode("ascii")
    tag = b"1" * 40
    repo = FakeRepo(
        refs={b"refs/tags/2.2.0": tag},
        objects={tag: FakeTag(commit), commit: FakeCommit(commit)},
    )

    assert _resolve_dulwich_revision(repo, "2.2.0", FakeTag) == EXPECTED_OTEL_COMMIT


def test_clone_source_rejects_non_https_or_credentialed_git_urls(tmp_path) -> None:
    with pytest.raises(PipelineError, match="HTTPS Git URL"):
        _clone_source_workspace(
            "git://github.com/open-telemetry/opentelemetry-demo.git",
            "2.2.0",
            EXPECTED_OTEL_COMMIT,
            tmp_path,
        )

    with pytest.raises(PipelineError, match="must not contain credentials"):
        _clone_source_workspace(
            "https://user:token@github.com/open-telemetry/opentelemetry-demo.git",
            "2.2.0",
            EXPECTED_OTEL_COMMIT,
            tmp_path,
        )

    with pytest.raises(PipelineError, match="must end with .git"):
        _clone_source_workspace(
            "https://github.com/open-telemetry/opentelemetry-demo",
            "2.2.0",
            EXPECTED_OTEL_COMMIT,
            tmp_path,
        )


def test_subprocess_json_retains_streamed_events(tmp_path) -> None:
    stdout = tmp_path / "stdout.json"
    stderr = tmp_path / "events.jsonl"

    result = _subprocess_json(
        [
            sys.executable,
            "-c",
            (
                "import json,sys; "
                "print(json.dumps({'event':'template_started'}), file=sys.stderr); "
                "print(json.dumps({'status':'completed'}))"
            ),
        ],
        cwd=tmp_path,
        stdout_path=stdout,
        stderr_path=stderr,
        timeout=10,
    )

    assert result == {"status": "completed"}
    assert "template_started" in stderr.read_text(encoding="utf-8")


def test_verified_source_cache_skips_network_clone(tmp_path) -> None:
    repository_url = "https://github.com/open-telemetry/opentelemetry-demo.git"
    source = tmp_path / "source"
    source.mkdir()
    (source / "service.go").write_text("package fixture\n", encoding="utf-8")
    (source / ".source-revision").write_text(
        (
            f"repository_url={repository_url}\n"
            "revision=2.2.0\n"
            f"commit={EXPECTED_OTEL_COMMIT}\n"
        ),
        encoding="utf-8",
    )
    cache_dir = tmp_path / "cache"
    archive, manifest = _write_source_cache_bundle(
        source,
        cache_dir,
        repository_url=repository_url,
        revision="2.2.0",
        expected_commit=EXPECTED_OTEL_COMMIT,
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    workspace, acquisition = _materialize_source_workspace(
        repository_url,
        "2.2.0",
        EXPECTED_OTEL_COMMIT,
        run_dir,
        cache_dir,
    )

    assert archive.is_file()
    assert manifest.is_file()
    assert acquisition == "verified_cache"
    assert (workspace / "service.go").read_text(encoding="utf-8") == "package fixture\n"
    assert not (workspace / ".git").exists()


def test_source_cache_rejects_tampered_archive(tmp_path) -> None:
    repository_url = "https://github.com/open-telemetry/opentelemetry-demo.git"
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("fixture\n", encoding="utf-8")
    (source / ".source-revision").write_text(
        (
            f"repository_url={repository_url}\n"
            "revision=2.2.0\n"
            f"commit={EXPECTED_OTEL_COMMIT}\n"
        ),
        encoding="utf-8",
    )
    cache_dir = tmp_path / "cache"
    archive, _ = _write_source_cache_bundle(
        source,
        cache_dir,
        repository_url=repository_url,
        revision="2.2.0",
        expected_commit=EXPECTED_OTEL_COMMIT,
    )
    with archive.open("ab") as handle:
        handle.write(b"tampered")
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with pytest.raises(PipelineError, match="SHA256 mismatch"):
        _materialize_source_workspace(
            repository_url,
            "2.2.0",
            EXPECTED_OTEL_COMMIT,
            run_dir,
            cache_dir,
        )
