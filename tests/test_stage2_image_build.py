from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import build_stage2_image as build


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _upstream_repo(tmp_path: Path, *, version: str | None = None, mcp_dependency: str | None = None, native_blade: bool = False) -> tuple[Path, str]:
    repo = tmp_path / "chaosblade"
    repo.mkdir()
    _git(repo, "init")
    package_version = version or build.BLADEAI_PACKAGE_VERSION
    dependency = mcp_dependency or build.BLADEAI_MCP_DEPENDENCY
    _write(
        repo / "blade-ai/pyproject.toml",
        f"""
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "blade-ai"
version = "{package_version}"
dependencies = [
    "{dependency}",
    "langchain-core>=1.0",
]
""".lstrip(),
    )
    _write(repo / "blade-ai/hatch_build.py", "HOOK = True\n")
    _write(repo / "blade-ai/src/chaos_agent/__init__.py", f'__version__ = "{package_version}"\n')
    _write(repo / "blade-ai/tui/package.json", f'{{"version": "{package_version}"}}\n')
    _write(repo / ".github/workflows/release-blade-ai.yml", "name: Release blade-ai\n")
    if native_blade:
        _write(repo / "blade-ai/vendor/chaosblade/blade", "native-binary-placeholder\n")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=test@example.invalid", "-c", "user.name=Test", "commit", "-m", "fixture")
    _git(repo, "tag", build.BLADEAI_RELEASE_TAG)
    return repo, _git(repo, "rev-parse", "HEAD")


def test_bladeai_source_context_uses_fixed_tag_archive_and_ignores_dirty_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, commit = _upstream_repo(tmp_path)
    monkeypatch.setattr(build, "BLADEAI_RELEASE_COMMIT", commit)
    _write(repo / "blade-ai/dirty.py", "must not enter archive\n")
    _write(repo / "blade-ai/pyproject.toml", "version = \"9.9.9\"\n")

    metadata = build.prepare_bladeai_build_context(repo, tmp_path / "context")

    assert metadata.release_tag == build.BLADEAI_RELEASE_TAG
    assert metadata.release_commit == commit
    assert metadata.package_version == "0.3.0"
    assert metadata.mcp_dependency == "mcp>=1.0,<2.0"
    assert metadata.mcp_pin.startswith("1.")
    assert (tmp_path / "context/pyproject.toml").read_text(encoding="utf-8").count('version = "0.3.0"') == 1
    assert not (tmp_path / "context/dirty.py").exists()
    assert not (tmp_path / "context/vendor/chaosblade").exists()


def test_bladeai_source_context_rejects_wrong_release_commit(tmp_path: Path) -> None:
    repo, _commit = _upstream_repo(tmp_path)

    with pytest.raises(RuntimeError, match="resolved to .* expected"):
        build.prepare_bladeai_build_context(repo, tmp_path / "context")


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"version": "0.6.2"}, "package version is 0.6.2"),
        ({"mcp_dependency": "mcp>=2.0,<3.0"}, "must declare mcp>=1.0,<2.0"),
        ({"native_blade": True}, "native ChaosBlade material"),
    ],
)
def test_bladeai_source_context_rejects_wrong_version_dependency_or_native_blade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, object], match: str,
) -> None:
    repo, commit = _upstream_repo(tmp_path, **kwargs)
    monkeypatch.setattr(build, "BLADEAI_RELEASE_COMMIT", commit)

    with pytest.raises(RuntimeError, match=match):
        build.prepare_bladeai_build_context(repo, tmp_path / "context")
