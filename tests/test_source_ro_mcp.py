from __future__ import annotations

import os
import asyncio
from pathlib import Path

import pytest
import yaml

from mcp_servers.source_ro import core
from mcp_servers.source_ro.core import (
    ALLOWED_APPLICATIONS_ENV,
    ALLOWED_REPOSITORIES_ENV,
    MAX_FILE_BYTES,
    SOURCE_ROOT_ENV,
    SourceROError,
    SourceROIndex,
    error_envelope,
)


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


def make_index(tmp_path: Path) -> SourceROIndex:
    source_root = tmp_path / "sources"
    repo = source_root / "demo-lock"
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("# Demo\nneedle here\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def handler():\n    return 'needle'\n", encoding="utf-8")
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (repo / "ground-truth-private").mkdir()
    (repo / "ground-truth-private" / "answer.patch").write_text("secret\n", encoding="utf-8")
    lockfile = tmp_path / "environment" / "shared" / "source-locks.yaml"
    write_lockfile(
        lockfile,
        [
            {
                "id": "demo-lock",
                "application": "demo",
                "component": "api",
                "remote": "https://example.invalid/demo.git",
                "tag": "v1.2.3",
                "commit": "a" * 40,
                "archiveSha256": "b" * 64,
                "agentMountPath": "/workspace/src/demo",
                "runtimeMappingStatus": "test-only",
            }
        ],
    )
    return SourceROIndex(source_root=source_root, lockfile=lockfile, allowed_applications={"demo"})


def make_repository(source_root: Path, lock_id: str) -> None:
    repo = source_root / lock_id
    repo.mkdir(parents=True)
    (repo / "README.md").write_text(f"# {lock_id}\n", encoding="utf-8")


def test_list_repositories_reports_lock_metadata(tmp_path):
    index = make_index(tmp_path)

    result = index.list_repositories()

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["returned"] == 1
    assert result["total"] == 1
    assert result["hasMore"] is False
    repo = result["repositories"][0]
    assert repo["id"] == "demo-lock"
    assert repo["component"] == "api"
    assert repo["materialized"] is True
    assert repo["sourceRootRef"] == "${RESBENCH_SOURCE_ROOT}/demo-lock"


def test_list_repositories_redacts_remote_credentials_queries_and_local_refs(tmp_path):
    source_root = tmp_path / "sources"
    for lock_id in ["public", "local", "private-ip"]:
        make_repository(source_root, lock_id)
    lockfile = tmp_path / "environment" / "shared" / "source-locks.yaml"
    write_lockfile(
        lockfile,
        [
            {
                "id": "public",
                "application": "demo",
                "remote": "https://user:token@example.com/org/repo.git?access_token=secret",
                "commit": "a" * 40,
                "archiveSha256": "b" * 64,
            },
            {
                "id": "local",
                "application": "demo",
                "remote": (tmp_path / "repo.git").as_uri(),
                "commit": "c" * 40,
                "archiveSha256": "d" * 64,
            },
            {
                "id": "private-ip",
                "application": "demo",
                "remote": "https://token@10.0.0.3/org/repo.git?token=secret",
                "commit": "e" * 40,
                "archiveSha256": "f" * 64,
            },
        ],
    )
    index = SourceROIndex(source_root=source_root, lockfile=lockfile, allowed_applications={"demo"})

    repos = {repo["id"]: repo for repo in index.list_repositories(limit=10)["repositories"]}

    assert repos["public"]["remote"] == "https://example.com/org/repo.git"
    assert "user" not in repos["public"]["remote"]
    assert "token" not in repos["public"]["remote"]
    assert "access_token" not in repos["public"]["remote"]
    assert repos["local"]["remote"] == "<redacted-local-or-private-remote>"
    assert repos["private-ip"]["remote"] == "<redacted-local-or-private-remote>"
    assert index.show_commit("public")["commit"]["remote"] == "https://example.com/org/repo.git"


def test_list_repositories_filters_and_paginates_with_limit_cap(tmp_path):
    source_root = tmp_path / "sources"
    locks = []
    for index in range(105):
        lock_id = f"repo-{index:03d}"
        make_repository(source_root, lock_id)
        locks.append(
            {
                "id": lock_id,
                "application": "sock-shop" if index % 2 == 0 else "otel-demo",
                "component": "orders" if index % 4 == 0 else "catalogue",
                "remote": f"https://example.com/{lock_id}.git",
                "commit": f"{index:040x}"[-40:],
                "archiveSha256": f"{index:064x}"[-64:],
            }
        )
    lockfile = tmp_path / "environment" / "shared" / "source-locks.yaml"
    write_lockfile(lockfile, locks)
    index = SourceROIndex(
        source_root=source_root,
        lockfile=lockfile,
        allowed_applications={"sock-shop", "otel-demo"},
    )

    capped = index.list_repositories(limit=500)
    filtered = index.list_repositories(application="sock-shop", component="orders", limit=3, offset=1)

    assert capped["limit"] == 100
    assert capped["returned"] == 100
    assert capped["total"] == 105
    assert capped["hasMore"] is True
    assert capped["nextOffset"] == 100
    assert filtered["limit"] == 3
    assert filtered["offset"] == 1
    assert filtered["total"] == 27
    assert filtered["returned"] == 3
    assert filtered["hasMore"] is True
    assert all(repo["application"] == "sock-shop" for repo in filtered["repositories"])
    assert all(repo["component"] == "orders" for repo in filtered["repositories"])


def test_list_files_paginates_and_hides_private_paths(tmp_path):
    index = make_index(tmp_path)

    result = index.list_files("demo-lock", recursive=True, limit=2, offset=0, format="markdown")

    assert result["ok"] is True
    assert result["returned"] == 2
    assert result["nextOffset"] == 2
    assert "| path | type | sizeBytes |" in result["markdown"]
    paths = {entry["path"] for entry in result["entries"]}
    assert ".git/config" not in paths
    assert "ground-truth-private/answer.patch" not in paths


def test_allowlist_filters_repositories_and_rejects_out_of_scope_repo(tmp_path):
    source_root = tmp_path / "sources"
    make_repository(source_root, "train-ticket-upstream")
    make_repository(source_root, "otel-demo-2.2.0")
    lockfile = tmp_path / "environment" / "shared" / "source-locks.yaml"
    write_lockfile(
        lockfile,
        [
            {
                "id": "train-ticket-upstream",
                "application": "train-ticket",
                "remote": "https://example.com/train-ticket.git",
                "commit": "a" * 40,
                "archiveSha256": "b" * 64,
            },
            {
                "id": "otel-demo-2.2.0",
                "application": "otel-demo",
                "remote": "https://example.com/otel-demo.git",
                "commit": "c" * 40,
                "archiveSha256": "d" * 64,
            },
        ],
    )
    index = SourceROIndex(
        source_root=source_root,
        lockfile=lockfile,
        allowed_applications={"train-ticket"},
    )

    repositories = index.list_repositories()["repositories"]
    assert [repo["id"] for repo in repositories] == ["train-ticket-upstream"]
    with pytest.raises(SourceROError) as exc:
        index.show_commit("otel-demo-2.2.0")
    assert exc.value.code == "repository_out_of_scope"
    structured = error_envelope(exc.value)
    assert structured["error"]["code"] == "repository_out_of_scope"
    assert "source_list_repositories" in structured["error"]["action"]


def test_repository_allowlist_can_narrow_episode_application(tmp_path):
    source_root = tmp_path / "sources"
    for lock_id in ["orders", "catalogue"]:
        make_repository(source_root, lock_id)
    lockfile = tmp_path / "environment" / "shared" / "source-locks.yaml"
    write_lockfile(
        lockfile,
        [
            {
                "id": "orders",
                "application": "sock-shop",
                "component": "orders",
                "remote": "https://example.com/orders.git",
                "commit": "a" * 40,
                "archiveSha256": "b" * 64,
            },
            {
                "id": "catalogue",
                "application": "sock-shop",
                "component": "catalogue",
                "remote": "https://example.com/catalogue.git",
                "commit": "c" * 40,
                "archiveSha256": "d" * 64,
            },
        ],
    )
    index = SourceROIndex(
        source_root=source_root,
        lockfile=lockfile,
        allowed_applications={"sock-shop"},
        allowed_repositories={"orders"},
    )

    assert [repo["id"] for repo in index.list_repositories()["repositories"]] == ["orders"]
    with pytest.raises(SourceROError, match="outside this episode scope"):
        index.read_file("catalogue", "README.md")


def test_repository_allowlist_cannot_cross_allowed_application(tmp_path):
    source_root = tmp_path / "sources"
    make_repository(source_root, "train-ticket-upstream")
    make_repository(source_root, "otel-demo-2.2.0")
    lockfile = tmp_path / "environment" / "shared" / "source-locks.yaml"
    write_lockfile(
        lockfile,
        [
            {
                "id": "train-ticket-upstream",
                "application": "train-ticket",
                "remote": "https://example.com/train-ticket.git",
                "commit": "a" * 40,
                "archiveSha256": "b" * 64,
            },
            {
                "id": "otel-demo-2.2.0",
                "application": "otel-demo",
                "remote": "https://example.com/otel-demo.git",
                "commit": "c" * 40,
                "archiveSha256": "d" * 64,
            },
        ],
    )

    with pytest.raises(SourceROError) as exc:
        SourceROIndex(
            source_root=source_root,
            lockfile=lockfile,
            allowed_applications={"train-ticket"},
            allowed_repositories={"otel-demo-2.2.0"},
        )

    assert exc.value.code == "invalid_source_scope"
    assert "outside the allowed application" in exc.value.message


def test_read_file_caps_output_and_supports_start_line(tmp_path):
    index = make_index(tmp_path)

    result = index.read_file("demo-lock", "README.md", start_line=2, max_chars=6)

    assert result["ok"] is True
    assert result["content"] == "needle"
    assert result["truncated"] is True
    assert result["maxChars"] == 6


def test_path_security_rejects_absolute_traversal_backslash_and_hidden(tmp_path):
    index = make_index(tmp_path)

    for unsafe_path in [
        "/etc/passwd",
        "../README.md",
        "src\\app.py",
        ".git/config",
        "ground-truth/answer.md",
        "ground_truth/answer.md",
        "ground-truth-private/answer.patch",
        "oracle-private/answer.md",
        "evaluator-private/score.md",
    ]:
        with pytest.raises(SourceROError):
            index.read_file("demo-lock", unsafe_path)


def test_symlink_escape_is_rejected(tmp_path):
    index = make_index(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    repo = tmp_path / "sources" / "demo-lock"
    (repo / "outside-link").symlink_to(outside)

    with pytest.raises(SourceROError, match="outside"):
        index.read_file("demo-lock", "outside-link")


def test_search_text_is_literal_python_search_and_does_not_execute_shell(tmp_path):
    index = make_index(tmp_path)
    marker = tmp_path / "should-not-exist"

    result = index.search_text("demo-lock", f"$(touch {marker})")

    assert result["ok"] is True
    assert result["totalMatches"] == 0
    assert not marker.exists()


def test_search_text_finds_matches_with_case_control(tmp_path):
    index = make_index(tmp_path)

    result = index.search_text("demo-lock", "NEEDLE", case_sensitive=False)

    assert result["ok"] is True
    assert result["totalMatches"] == 2
    assert {match["path"] for match in result["matches"]} == {"README.md", "src/app.py"}


def test_recursive_list_files_stops_at_scan_limit_with_actionable_warning(monkeypatch, tmp_path):
    index = make_index(tmp_path)
    repo = tmp_path / "sources" / "demo-lock"
    for file_index in range(6):
        (repo / f"scan-{file_index}.txt").write_text("visible\n", encoding="utf-8")
    monkeypatch.setattr(core, "MAX_LIST_SCAN_ENTRIES", 3)

    result = index.list_files("demo-lock", recursive=True, limit=2)

    assert result["ok"] is True
    assert result["truncated"] is True
    assert result["warning"]["code"] == "scan_limit_reached"
    assert "narrower path" in result["warning"]["action"]
    assert result["scannedEntries"] == 3
    assert result["returned"] == 2


def test_search_text_stops_at_file_scan_limit_with_actionable_warning(monkeypatch, tmp_path):
    index = make_index(tmp_path)
    repo = tmp_path / "sources" / "demo-lock"
    for file_index in range(5):
        (repo / f"needle-{file_index}.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(core, "MAX_SEARCH_SCAN_FILES", 2)

    result = index.search_text("demo-lock", "needle", limit=10)

    assert result["ok"] is True
    assert result["truncated"] is True
    assert result["warning"]["code"] == "scan_limit_reached"
    assert "narrower path" in result["warning"]["action"]
    assert result["scannedFiles"] == 2
    assert result["totalMatchesIsLowerBound"] is True


def test_binary_and_oversized_files_are_rejected(tmp_path):
    index = make_index(tmp_path)
    repo = tmp_path / "sources" / "demo-lock"
    (repo / "blob.bin").write_bytes(b"\x00abc")
    (repo / "large.txt").write_text("x" * (MAX_FILE_BYTES + 1), encoding="utf-8")

    with pytest.raises(SourceROError, match="binary"):
        index.read_file("demo-lock", "blob.bin")
    with pytest.raises(SourceROError, match="exceeding"):
        index.read_file("demo-lock", "large.txt")


def test_show_commit_uses_lockfile_not_git_internals(tmp_path):
    index = make_index(tmp_path)

    result = index.show_commit("demo-lock")

    assert result["ok"] is True
    assert result["commit"]["commit"] == "a" * 40
    assert result["commit"]["archiveSha256"] == "b" * 64
    assert result["commit"]["runtimeMappingStatus"] == "test-only"


def test_missing_environment_source_root_is_actionable(monkeypatch):
    monkeypatch.delenv("RESBENCH_SOURCE_ROOT", raising=False)

    with pytest.raises(SourceROError) as exc:
        SourceROIndex(lockfile=Path("does-not-matter.yaml"), allowed_applications={"demo"})

    assert exc.value.code == "missing_source_root"
    assert "Set RESBENCH_SOURCE_ROOT" in exc.value.action


def test_invalid_lockfile_item_is_actionable(tmp_path):
    source_root = tmp_path / "sources"
    source_root.mkdir()
    lockfile = tmp_path / "environment" / "shared" / "source-locks.yaml"
    write_lockfile(lockfile, ["not-a-mapping"])

    with pytest.raises(SourceROError) as exc:
        SourceROIndex(source_root=source_root, lockfile=lockfile, allowed_applications={"demo"})

    assert exc.value.code == "invalid_lockfile"
    assert "must be a mapping" in exc.value.message


def test_mcp_server_annotations_when_sdk_is_available(monkeypatch, tmp_path):
    pytest.importorskip("mcp.server")
    index = make_index(tmp_path)
    monkeypatch.setenv("RESBENCH_SOURCE_ROOT", str(index.source_root))
    monkeypatch.setenv(ALLOWED_APPLICATIONS_ENV, "demo")
    monkeypatch.chdir(tmp_path)
    os.makedirs("environment/shared", exist_ok=True)
    (tmp_path / "environment/shared/source-locks.yaml").write_text(index.lockfile.read_text(encoding="utf-8"), encoding="utf-8")

    from mcp_servers.source_ro.server import create_server

    server = create_server()
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}

    assert set(tools) == {
        "source_list_repositories",
        "source_list_files",
        "source_search_text",
        "source_read_file",
        "source_show_commit",
    }
    for tool in tools.values():
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.idempotent_hint is True
        assert tool.annotations.open_world_hint is False


def test_source_ro_stdio_server_creation_does_not_require_mcp_token(monkeypatch):
    monkeypatch.delenv("RESBENCH_MCP_TOKEN", raising=False)

    from mcp_servers.http_runtime import create_server_for_transport
    from mcp_servers.source_ro.server import create_server

    server, config = create_server_for_transport(create_server, transport="stdio", env={})

    assert server.name == "source_ro"
    assert config is None


def test_from_environment_requires_episode_application_scope(monkeypatch, tmp_path):
    index = make_index(tmp_path)
    monkeypatch.setenv(SOURCE_ROOT_ENV, str(index.source_root))
    monkeypatch.chdir(tmp_path)
    os.makedirs("environment/shared", exist_ok=True)
    (tmp_path / "environment/shared/source-locks.yaml").write_text(index.lockfile.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.delenv(ALLOWED_APPLICATIONS_ENV, raising=False)

    with pytest.raises(SourceROError) as exc:
        SourceROIndex.from_environment()

    assert exc.value.code == "missing_source_scope"


def test_from_environment_rejects_multiple_episode_applications(monkeypatch, tmp_path):
    index = make_index(tmp_path)
    monkeypatch.setenv(SOURCE_ROOT_ENV, str(index.source_root))
    monkeypatch.setenv(ALLOWED_APPLICATIONS_ENV, "demo,other")
    monkeypatch.chdir(tmp_path)
    os.makedirs("environment/shared", exist_ok=True)
    (tmp_path / "environment/shared/source-locks.yaml").write_text(index.lockfile.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(SourceROError) as exc:
        SourceROIndex.from_environment()

    assert exc.value.code == "invalid_source_scope"


def test_server_tool_calls_apply_episode_scope(monkeypatch, tmp_path):
    source_root = tmp_path / "sources"
    make_repository(source_root, "train-ticket-upstream")
    make_repository(source_root, "otel-demo-2.2.0")
    lockfile = tmp_path / "environment" / "shared" / "source-locks.yaml"
    write_lockfile(
        lockfile,
        [
            {
                "id": "train-ticket-upstream",
                "application": "train-ticket",
                "remote": "https://example.com/train-ticket.git",
                "commit": "a" * 40,
                "archiveSha256": "b" * 64,
            },
            {
                "id": "otel-demo-2.2.0",
                "application": "otel-demo",
                "remote": "https://example.com/otel-demo.git",
                "commit": "c" * 40,
                "archiveSha256": "d" * 64,
            },
        ],
    )
    monkeypatch.setenv(SOURCE_ROOT_ENV, str(source_root))
    monkeypatch.setenv(ALLOWED_APPLICATIONS_ENV, "train-ticket")
    monkeypatch.chdir(tmp_path)
    os.makedirs("environment/shared", exist_ok=True)
    (tmp_path / "environment/shared/source-locks.yaml").write_text(lockfile.read_text(encoding="utf-8"), encoding="utf-8")

    from mcp_servers.source_ro.server import create_server

    server = create_server()
    listed = asyncio.run(server.call_tool("source_list_repositories", {}))
    denied = asyncio.run(server.call_tool("source_show_commit", {"repo_id": "otel-demo-2.2.0"}))

    assert listed.structured_content["repositories"][0]["id"] == "train-ticket-upstream"
    assert denied.structured_content["ok"] is False
    assert denied.structured_content["error"]["code"] == "repository_out_of_scope"


def test_source_ro_http_server_creation_requires_token(monkeypatch):
    from mcp_servers.http_runtime import (
        ISSUER_URL_ENV,
        MCPRuntimeConfigError,
        RESOURCE_URL_ENV,
        SCOPE_ENV,
        create_server_for_transport,
    )
    from mcp_servers.source_ro.server import create_server

    env = {
        ISSUER_URL_ENV: "https://issuer.example.test",
        RESOURCE_URL_ENV: "https://mcp.example.test/source",
        SCOPE_ENV: "source_ro:read",
    }

    with pytest.raises(MCPRuntimeConfigError):
        create_server_for_transport(create_server, transport="streamable-http", env=env)


def test_source_ro_http_server_creation_uses_authenticated_runtime():
    from mcp_servers.http_runtime import (
        ISSUER_URL_ENV,
        RESOURCE_URL_ENV,
        SCOPE_ENV,
        TOKEN_ENV,
        create_server_for_transport,
    )
    from mcp_servers.source_ro.server import create_server

    server, config = create_server_for_transport(
        create_server,
        transport="streamable-http",
        env={
            TOKEN_ENV: "t" * 40,
            ISSUER_URL_ENV: "https://issuer.example.test",
            RESOURCE_URL_ENV: "https://mcp.example.test/source",
            SCOPE_ENV: "source_ro:read",
        },
    )

    assert server.name == "source_ro"
    assert config is not None
    assert config.host == "127.0.0.1"
    assert config.path == "/mcp"
