"""第一阶段核心功能工作台：预览、确认、异步日志与 API。"""

from pathlib import Path
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient

from backend.api.phase1 import get_phase1_core_service
from backend.main import create_app
from backend.services.phase1 import CodeGraphQueryInput, GitCloneInput, Phase1CoreService

REPO_ROOT = Path(__file__).resolve().parents[1]


def _inputs(tmp_path: Path) -> dict:
    return {
        "repository_url": "https://github.com/example/project.git",
        "revision": "main",
        "destination_path": str(tmp_path / "clone"),
        "auth_mode": "public_https",
        "github_account": None,
        "clone_depth": 1,
        "existing_policy": "update_existing",
    }


def _wait(service: Phase1CoreService, run_id: str):
    deadline = monotonic() + 2
    while monotonic() < deadline:
        run = service.get_run(run_id)
        if run.status in {"passed", "failed"}:
            return run
        sleep(0.01)
    raise AssertionError("core test run did not finish")


def test_catalog_exposes_only_core_function_groups() -> None:
    catalog = Phase1CoreService(REPO_ROOT).catalog()

    assert len(catalog.features) == 10
    assert {item.group for item in catalog.features} == {
        "source-codegraph",
        "templates-defects",
        "kubernetes",
        "evidence-contracts",
    }
    defect_scan = next(item for item in catalog.features if item.id == "defect-scan")
    codegraph_query = next(item for item in catalog.features if item.id == "codegraph-query")
    assert defect_scan.may_exceed_five_minutes is True
    operation_field = next(item for item in codegraph_query.input_fields if item.name == "operation")
    assert "impact" in operation_field.allowed_values
    assert "services" in operation_field.allowed_values
    assert {item.name for item in defect_scan.input_fields} >= {
        "config_path",
        "active_template_ids",
        "planning_mode",
        "run_id",
    }


def test_static_candidate_feature_exposes_six_plain_language_candidates() -> None:
    service = Phase1CoreService(REPO_ROOT)
    feature = next(item for item in service.catalog().features if item.id == "static-candidates")
    candidate_field = next(item for item in feature.input_fields if item.name == "candidate_ids")

    assert candidate_field.allowed_values == [
        "OTEL-C01", "OTEL-C02", "OTEL-C03",
        "OTEL-C04", "OTEL-C05", "OTEL-C06",
    ]
    assert feature.example_inputs["candidate_ids"] == candidate_field.allowed_values

    preview = service.preview("static-candidates", feature.example_inputs)
    queued = service.start(
        preview.preview_id,
        confirmed=True,
        input_digest=preview.input_digest,
    )
    finished = _wait(service, queued.run_id)

    assert finished.status == "passed"
    assert finished.result["candidate_set_id"] == "otel-demo-2.2.0-core-six"
    assert finished.result["runtime_confirmed"] is False
    assert [item["candidate_id"] for item in finished.result["candidates"]] == candidate_field.allowed_values
    assert all(set(item) == {"candidate_id", "title", "description", "test_note"} for item in finished.result["candidates"])
    assert all(item["description"] for item in finished.result["candidates"])
    assert all(item["test_note"] for item in finished.result["candidates"])

    invalid_inputs = dict(feature.example_inputs)
    invalid_inputs["candidate_ids"] = ["OTEL-C99"]
    with pytest.raises(ValueError, match="unknown OTel Demo candidate IDs"):
        service.preview("static-candidates", invalid_inputs)


def test_harness_prompt_feature_returns_copy_ready_natural_language() -> None:
    service = Phase1CoreService(REPO_ROOT)
    feature = next(item for item in service.catalog().features if item.id == "harness-prompt")
    candidate_field = next(item for item in feature.input_fields if item.name == "candidate_id")

    assert candidate_field.allowed_values == [
        "OTEL-C01", "OTEL-C02", "OTEL-C03",
        "OTEL-C04", "OTEL-C05", "OTEL-C06",
    ]

    preview = service.preview("harness-prompt", feature.example_inputs)
    assert any("只生成Prompt" in warning for warning in preview.warnings)
    queued = service.start(
        preview.preview_id,
        confirmed=True,
        input_digest=preview.input_digest,
    )
    finished = _wait(service, queued.run_id)

    assert finished.status == "passed"
    assert finished.result["candidate_id"] == "OTEL-C04"
    assert finished.result["interaction_mode"] == "direct_natural_language"
    assert finished.result["ready_to_copy"] is True
    assert finished.result["runtime_execution_status"] == "not_executed"
    assert "请在 OTel Demo 2.2.0 中验证" in finished.result["prompt"]
    assert "Checkout Pod 被终止时" in finished.result["prompt"]
    assert "chaos_control" in finished.result["prompt"]
    assert "run_harness_trial.py" not in finished.result["prompt"]
    assert "--execute" not in finished.result["prompt"]

    invalid_inputs = dict(feature.example_inputs)
    invalid_inputs["candidate_id"] = "OTEL-C99"
    with pytest.raises(ValueError):
        service.preview("harness-prompt", invalid_inputs)


def test_preview_never_executes_and_returns_digest_bound_plan(tmp_path: Path) -> None:
    calls: list[dict] = []

    def executor(inputs, log):
        calls.append(inputs.model_dump(mode="json"))
        return {"test_passed": True}

    service = Phase1CoreService(
        REPO_ROOT, executor_overrides={"git-clone": executor}
    )
    preview = service.preview("git-clone", _inputs(tmp_path))

    assert calls == []
    assert preview.normalized_inputs["destination_path"] == str((tmp_path / "clone").resolve())
    assert len(preview.input_digest) == 64
    assert preview.input_digest[:12] in preview.confirmation_text
    assert preview.operations
    assert any("git clone" in operation for operation in preview.operations)
    checkout = next(operation for operation in preview.operations if "checkout --detach" in operation)
    assert "checkout --detach main" in checkout
    assert "checkout --detach -- main" not in checkout


def test_git_clone_preview_rejects_nonempty_destination(tmp_path: Path) -> None:
    destination = tmp_path / "clone"
    destination.mkdir()
    (destination / "user-file.txt").write_text("keep")
    inputs = _inputs(tmp_path)

    with pytest.raises(ValueError, match="choose an empty directory"):
        Phase1CoreService(REPO_ROOT).preview("git-clone", inputs)


def test_git_clone_preview_updates_existing_git_repository(tmp_path: Path) -> None:
    destination = tmp_path / "clone"
    (destination / ".git").mkdir(parents=True)
    inputs = _inputs(tmp_path)

    preview = Phase1CoreService(REPO_ROOT).preview("git-clone", inputs)

    assert any("remote add origin" in item for item in preview.operations)
    assert any("git -C" in item and "fetch" in item for item in preview.operations)
    assert any("checkout --detach FETCH_HEAD" in item for item in preview.operations)


def test_git_clone_ignores_generated_codegraph_index(monkeypatch, tmp_path: Path) -> None:
    destination = tmp_path / "clone"
    (destination / ".git").mkdir(parents=True)
    (destination / ".codegraph").mkdir()
    (destination / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/example/project.git\n',
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    def fake_run_process(argv, log, *, timeout):
        commands.append(argv)
        joined = " ".join(argv)
        if "status --porcelain" in joined:
            return "?? .codegraph/"
        if "rev-parse HEAD" in joined:
            return "a" * 40
        if "remote get-url origin" in joined:
            return "https://github.com/example/project.git"
        if "status --short --branch" in joined:
            return "## HEAD (no branch)"
        if "ls-files" in joined:
            return "src/main.ts"
        return ""

    monkeypatch.setattr(Phase1CoreService, "_run_process", staticmethod(fake_run_process))

    result = Phase1CoreService._execute_git_clone(
        GitCloneInput.model_validate(_inputs(tmp_path)),
        lambda level, message, details=None: None,
    )

    assert result["test_passed"] is True
    assert any("fetch" in command for command in result["clone_commands"])
    assert ["git", "-C", str(destination), "reset", "--hard", "HEAD"] not in commands


def test_git_clone_rejects_real_source_changes(monkeypatch, tmp_path: Path) -> None:
    destination = tmp_path / "clone"
    (destination / ".git").mkdir(parents=True)
    (destination / ".codegraph").mkdir()
    (destination / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/example/project.git\n',
        encoding="utf-8",
    )

    def fake_run_process(argv, log, *, timeout):
        if " ".join(argv).endswith("status --porcelain"):
            return "?? .codegraph/\n M src/main.ts"
        return ""

    monkeypatch.setattr(Phase1CoreService, "_run_process", staticmethod(fake_run_process))

    with pytest.raises(RuntimeError, match="existing repository has local changes"):
        Phase1CoreService._execute_git_clone(
            GitCloneInput.model_validate(_inputs(tmp_path)),
            lambda level, message, details=None: None,
        )


def test_run_requires_explicit_confirmation_and_matching_digest(tmp_path: Path) -> None:
    service = Phase1CoreService(
        REPO_ROOT,
        executor_overrides={
            "git-clone": lambda inputs, log: {"test_passed": True}
        },
    )
    preview = service.preview("git-clone", _inputs(tmp_path))

    with pytest.raises(PermissionError):
        service.start(preview.preview_id, confirmed=False, input_digest=preview.input_digest)
    with pytest.raises(PermissionError):
        service.start(preview.preview_id, confirmed=True, input_digest="0" * 64)


def test_confirmed_run_retains_logs_inputs_and_structured_result(tmp_path: Path) -> None:
    def executor(inputs, log):
        log("info", "读取输入", {"path": str(inputs.destination_path)})
        log("warning", "fixture warning", {"code": "FIXTURE"})
        return {"test_passed": True, "value": {"status": "exact"}}

    service = Phase1CoreService(
        REPO_ROOT, executor_overrides={"git-clone": executor}
    )
    preview = service.preview("git-clone", _inputs(tmp_path))
    queued = service.start(
        preview.preview_id,
        confirmed=True,
        input_digest=preview.input_digest,
    )
    finished = _wait(service, queued.run_id)

    assert finished.status == "passed"
    assert finished.inputs == preview.normalized_inputs
    assert finished.result["value"]["status"] == "exact"
    assert [entry.sequence for entry in finished.logs] == list(
        range(1, len(finished.logs) + 1)
    )
    assert {entry.message for entry in finished.logs} >= {
        "测试开始",
        "读取输入",
        "fixture warning",
        "测试完成",
    }


def test_api_preview_start_and_poll(tmp_path: Path) -> None:
    service = Phase1CoreService(
        REPO_ROOT,
        executor_overrides={
            "git-clone": lambda inputs, log: {
                "test_passed": True,
                "snapshot": {"status": "exact"},
            }
        },
    )
    app = create_app()
    app.dependency_overrides[get_phase1_core_service] = lambda: service
    client = TestClient(app)

    catalog = client.get("/api/v1/phase1/core/features")
    preview = client.post(
        "/api/v1/phase1/core/features/git-clone/preview",
        json={"inputs": _inputs(tmp_path)},
    )
    preview_body = preview.json()
    rejected = client.post(
        f"/api/v1/phase1/core/previews/{preview_body['previewId']}/start",
        json={"confirmed": False, "inputDigest": preview_body["inputDigest"]},
    )
    started = client.post(
        f"/api/v1/phase1/core/previews/{preview_body['previewId']}/start",
        json={"confirmed": True, "inputDigest": preview_body["inputDigest"]},
    )
    finished = _wait(service, started.json()["runId"])
    polled = client.get(f"/api/v1/phase1/core/runs/{finished.run_id}")

    assert catalog.status_code == 200
    assert preview.status_code == 200
    assert rejected.status_code == 409
    assert started.status_code == 202
    assert polled.status_code == 200
    assert polled.json()["status"] == "passed"


def test_codegraph_query_returns_full_index_overview(monkeypatch, tmp_path: Path) -> None:
    class FakeCodeGraphDriver:
        def status_snapshot(self):
            return {
                "initialized": True,
                "fileCount": 311,
                "nodeCount": 8770,
                "edgeCount": 12667,
                "languages": ["cpp", "java", "typescript"],
                "pendingChanges": {"added": 0, "modified": 0, "removed": 0},
            }

        def context(self, task: str, *, max_nodes: int, max_code_blocks: int):
            return {
                "query": task,
                "nodes": [
                    {
                        "id": "class:a",
                        "kind": "class",
                        "qualifiedName": "CheckoutService",
                        "filePath": "src/checkout.ts",
                    }
                ],
                "edges": [],
                "stats": {"nodeCount": 1, "edgeCount": 0, "fileCount": 1},
            }

    monkeypatch.setattr(
        Phase1CoreService,
        "_codegraph_driver",
        staticmethod(lambda item: FakeCodeGraphDriver()),
    )
    result = Phase1CoreService._execute_codegraph_query(
        CodeGraphQueryInput(
            source_path=tmp_path,
            source_identity="fixture-source",
            operation="context",
            expression="checkout to cart",
        ),
        lambda level, message, details=None: None,
    )

    assert result["index_overview"] == {
        "initialized": True,
        "file_count": 311,
        "node_count": 8770,
        "edge_count": 12667,
        "languages": ["cpp", "java", "typescript"],
        "pending_changes": {"added": 0, "modified": 0, "removed": 0},
    }
    assert result["raw_result"]["stats"]["nodeCount"] == 1


def test_codegraph_query_supports_impact(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, int]] = []

    class FakeCodeGraphDriver:
        def status_snapshot(self):
            return {
                "initialized": True,
                "fileCount": 1,
                "nodeCount": 3,
                "edgeCount": 2,
                "languages": ["typescript"],
                "pendingChanges": {"added": 0, "modified": 0, "removed": 0},
            }

        def impact(self, symbol: str, *, depth: int):
            calls.append((symbol, depth))
            return {
                "symbol": symbol,
                "depth": depth,
                "nodeCount": 1,
                "edgeCount": 0,
                "affected": [
                    {
                        "kind": "function",
                        "name": "checkout",
                        "filePath": "src/checkout.ts",
                        "startLine": 12,
                    }
                ],
            }

    monkeypatch.setattr(
        Phase1CoreService,
        "_codegraph_driver",
        staticmethod(lambda item: FakeCodeGraphDriver()),
    )
    result = Phase1CoreService._execute_codegraph_query(
        CodeGraphQueryInput(
            source_path=tmp_path,
            source_identity="fixture-source",
            operation="impact",
            expression="checkout",
            depth=3,
        ),
        lambda level, message, details=None: None,
    )

    assert calls == [("checkout", 3)]
    assert result["raw_result"]["affected"][0]["name"] == "checkout"
    assert result["query_status"] == "matched"


def test_codegraph_query_auto_routes_service_inventory_question(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "src" / "checkout").mkdir(parents=True)
    (tmp_path / "src" / "cart").mkdir(parents=True)
    (tmp_path / "docker-compose.yml").write_text(
        """
services:
  checkout:
    build:
      context: .
      dockerfile: src/checkout/Dockerfile
    environment:
      - OTEL_SERVICE_NAME=checkout
    depends_on:
      - cart
  cart:
    build:
      context: .
      dockerfile: src/cart/Dockerfile
    environment:
      OTEL_SERVICE_NAME: cart
  postgresql:
    image: postgres:16
""".lstrip(),
        encoding="utf-8",
    )

    class FakeCodeGraphDriver:
        def status_snapshot(self):
            return {
                "initialized": True,
                "fileCount": 3,
                "nodeCount": 30,
                "edgeCount": 20,
                "languages": ["go", "yaml"],
                "pendingChanges": {"added": 0, "modified": 0, "removed": 0},
            }

        def context(self, task: str, *, max_nodes: int, max_code_blocks: int):
            raise AssertionError(f"context should not be called for service inventory: {task}")

    monkeypatch.setattr(
        Phase1CoreService,
        "_codegraph_driver",
        staticmethod(lambda item: FakeCodeGraphDriver()),
    )
    result = Phase1CoreService._execute_codegraph_query(
        CodeGraphQueryInput(
            source_path=tmp_path,
            source_identity="fixture-source",
            operation="context",
            expression="有哪些服务呢",
        ),
        lambda level, message, details=None: None,
    )

    assert result["test_passed"] is True
    assert result["resolved_operation"] == "services"
    assert result["query_status"] == "matched"
    assert result["raw_result"]["service_count"] == 3
    assert [item["name"] for item in result["raw_result"]["services"]] == [
        "cart",
        "checkout",
        "postgresql",
    ]
    assert "checkout" in result["raw_result"]["answer"]
    assert any(node["kind"] == "service" for node in result["raw_result"]["nodes"])
    assert any(edge["kind"] == "implemented_by" for edge in result["raw_result"]["edges"])


def test_codegraph_query_marks_empty_result_as_no_match(monkeypatch, tmp_path: Path) -> None:
    class FakeCodeGraphDriver:
        def status_snapshot(self):
            return {
                "initialized": True,
                "fileCount": 1,
                "nodeCount": 10,
                "edgeCount": 3,
                "languages": ["typescript"],
                "pendingChanges": {"added": 0, "modified": 0, "removed": 0},
            }

        def context(self, task: str, *, max_nodes: int, max_code_blocks: int):
            return {
                "query": task,
                "summary": "Found 0 relevant code symbols across 0 files.",
                "nodes": [],
                "edges": [],
                "codeBlocks": [],
                "stats": {"nodeCount": 0, "edgeCount": 0, "fileCount": 0},
            }

    monkeypatch.setattr(
        Phase1CoreService,
        "_codegraph_driver",
        staticmethod(lambda item: FakeCodeGraphDriver()),
    )
    result = Phase1CoreService._execute_codegraph_query(
        CodeGraphQueryInput(
            source_path=tmp_path,
            source_identity="fixture-source",
            operation="context",
            expression="does not exist",
        ),
        lambda level, message, details=None: None,
    )

    assert result["test_passed"] is False
    assert result["query_status"] == "no_match"
