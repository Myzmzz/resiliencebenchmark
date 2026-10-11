"""第一阶段核心功能的预览、确认、异步执行、日志与结果服务。"""

from __future__ import annotations

import hashlib
import json
import configparser
import re
import shlex
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Literal
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from backend.models.phase1 import (
    CoreFeature,
    CoreFeatureCatalog,
    CoreTestLog,
    CoreTestPreview,
    CoreTestRun,
    FeatureInputField,
)
from resilience_agent.semantic_scan.codegraph_driver import CodeGraphDriver
from resilience_agent.semantic_scan.config import (
    CodebaseConfig,
    CodeGraphConfig,
    KubernetesConfig,
    KubernetesSourceConfig,
    load_semantic_scan_config,
)
from resilience_agent.semantic_scan.contracts import SemanticScanReport
from resilience_agent.semantic_scan.evidence import EvidenceLedger
from resilience_agent.semantic_scan.kubernetes_scanner import (
    KubernetesConfigScanner,
    LiveKubernetesScanner,
)
from resilience_agent.semantic_scan.registry import ACTIVE_TEMPLATE_IDS
from resilience_agent.semantic_scan.workflow import SemanticScanWorkflow

PREVIEW_TTL_MINUTES = 15
MAX_LOGS = 2_000
OTEL_DEMO_CANDIDATE_IDS = (
    "OTEL-C01",
    "OTEL-C02",
    "OTEL-C03",
    "OTEL-C04",
    "OTEL-C05",
    "OTEL-C06",
)


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GitCloneInput(InputModel):
    repository_url: str = Field(min_length=12, max_length=500)
    revision: str = Field(default="main", min_length=1, max_length=200)
    destination_path: Path
    auth_mode: Literal["public_https", "gh_cli"] = "public_https"
    github_account: str | None = Field(default=None, max_length=100)
    clone_depth: int = Field(default=0, ge=0, le=1000)
    existing_policy: Literal["update_existing", "fail_if_exists"] = "update_existing"

    @field_validator("revision")
    @classmethod
    def safe_revision(cls, value: str) -> str:
        if value.startswith("-") or any(character.isspace() for character in value):
            raise ValueError("revision must not start with '-' or contain whitespace")
        return value

    @field_validator("repository_url")
    @classmethod
    def github_url_only(cls, value: str) -> str:
        if not value.startswith("https://github.com/") or not value.endswith(".git"):
            raise ValueError("repository_url must be an HTTPS github.com URL ending in .git")
        if any(token in value for token in ("@github.com", "?", "#")):
            raise ValueError("repository_url must not contain credentials, query, or fragment")
        return value

    @field_validator("github_account")
    @classmethod
    def safe_github_account(cls, value: str | None) -> str | None:
        if value is not None and not value.replace("-", "").isalnum():
            raise ValueError("github_account may contain only letters, digits, and hyphens")
        return value


class CodeGraphIndexInput(InputModel):
    source_path: Path
    source_identity: str = Field(min_length=3, max_length=200)
    command: str = "codegraph"
    force_reindex: bool = False
    timeout_seconds: int = Field(default=600, ge=30, le=1800)


class CodeGraphQueryInput(InputModel):
    source_path: Path
    source_identity: str = Field(min_length=3, max_length=200)
    command: str = "codegraph"
    operation: Literal["query", "callers", "callees", "context", "impact", "services"]
    expression: str = Field(min_length=1, max_length=4000)
    kind: str | None = None
    limit: int = Field(default=20, ge=1, le=100)
    max_nodes: int = Field(default=120, ge=10, le=200)
    max_code_blocks: int = Field(default=8, ge=0, le=20)
    depth: int = Field(default=2, ge=1, le=5)


class StaticCandidateSetInput(InputModel):
    candidate_set_path: Path
    candidate_ids: list[str] = Field(min_length=1, max_length=6)

    @field_validator("candidate_ids")
    @classmethod
    def valid_candidate_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("candidate_ids must be unique")
        unknown = sorted(set(value) - set(OTEL_DEMO_CANDIDATE_IDS))
        if unknown:
            raise ValueError("unknown OTel Demo candidate IDs: " + ", ".join(unknown))
        return value


class HarnessPromptInput(InputModel):
    prompt_catalog_path: Path
    candidate_id: str = Field(pattern=r"^OTEL-C0[1-6]$")
    additional_instruction: str | None = Field(default=None, max_length=1200)

    @field_validator("additional_instruction")
    @classmethod
    def normalized_additional_instruction(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class DefectScanInput(InputModel):
    config_path: Path
    active_template_ids: list[str] = Field(min_length=1, max_length=10)
    planning_mode: Literal["deterministic", "model"] = "deterministic"
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$")
    resume: bool = False

    @field_validator("active_template_ids")
    @classmethod
    def unique_template_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("active_template_ids must be unique")
        unknown = sorted(set(value) - set(ACTIVE_TEMPLATE_IDS))
        if unknown:
            raise ValueError("unknown active template IDs: " + ", ".join(unknown))
        return value


class ManifestSourceInput(InputModel):
    alias: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    path: Path


class KubernetesManifestInput(InputModel):
    namespace: str = Field(min_length=1, max_length=253)
    sources: list[ManifestSourceInput] = Field(min_length=1, max_length=20)
    authoritative_for_namespace: bool = False
    max_resources_per_agent: int = Field(default=80, ge=10, le=500)


class KubernetesLiveInput(InputModel):
    namespace: str = Field(min_length=1, max_length=253)
    context: str = Field(min_length=1, max_length=253)
    kubeconfig_path: Path | None = None
    authoritative_for_namespace: bool = False
    max_resources_per_agent: int = Field(default=160, ge=10, le=500)


class EvidenceTransformInput(InputModel):
    source_type: Literal["codegraph", "kubernetes"]
    query_label: str = Field(default="user-defined", min_length=1, max_length=200)
    payload: Any


class ContractReplayInput(InputModel):
    report_path: Path


LogSink = Callable[[Literal["info", "warning", "error"], str, dict[str, Any] | None], None]
Executor = Callable[[BaseModel, LogSink], Any]
PlanBuilder = Callable[[BaseModel], tuple[list[str], list[str]]]


@dataclass(frozen=True)
class FeatureDefinition:
    public: CoreFeature
    input_model: type[BaseModel]
    executor: Executor
    plan_builder: PlanBuilder


@dataclass
class PreviewRecord:
    preview: CoreTestPreview
    inputs: BaseModel
    used: bool = False


@dataclass
class RunRecord:
    run_id: str
    feature_id: str
    inputs: dict[str, Any]
    created_at: datetime
    status: Literal["queued", "running", "passed", "failed"] = "queued"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    logs: list[CoreTestLog] = field(default_factory=list)
    result: Any | None = None
    error: str | None = None


def _input_field(
    name: str,
    label: str,
    value_type: str,
    description: str,
    *,
    required: bool = True,
    allowed_values: list[str] | None = None,
) -> FeatureInputField:
    return FeatureInputField(
        name=name,
        label=label,
        valueType=value_type,
        required=required,
        description=description,
        allowedValues=allowed_values or [],
    )


class Phase1CoreService:
    """本地开发工作台。预览不执行，只有显式确认后才进入线程执行。"""

    def __init__(
        self,
        repo_path: Path,
        *,
        executor_overrides: dict[str, Executor] | None = None,
    ):
        self.repo_path = repo_path.resolve()
        self._lock = threading.RLock()
        self._previews: dict[str, PreviewRecord] = {}
        self._runs: dict[str, RunRecord] = {}
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="phase1-core")
        self._features = self._build_features(executor_overrides or {})

    def catalog(self) -> CoreFeatureCatalog:
        return CoreFeatureCatalog(features=[item.public for item in self._features.values()])

    def preview(self, feature_id: str, raw_inputs: dict[str, Any]) -> CoreTestPreview:
        definition = self._definition(feature_id)
        prepared = self._resolve_input_paths(feature_id, raw_inputs)
        validated = definition.input_model.model_validate(prepared)
        self._validate_safe_inputs(feature_id, validated)
        operations, warnings = definition.plan_builder(validated)
        normalized = validated.model_dump(mode="json")
        digest = hashlib.sha256(
            json.dumps(
                {"feature_id": feature_id, "inputs": normalized},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        now = datetime.now(UTC)
        preview = CoreTestPreview(
            previewId=f"preview-{uuid4().hex[:16]}",
            featureId=feature_id,
            createdAt=now,
            expiresAt=now + timedelta(minutes=PREVIEW_TTL_MINUTES),
            normalizedInputs=normalized,
            operations=operations,
            warnings=warnings,
            confirmationText=(
                f"我确认使用输入摘要 {digest[:12]} 执行「{definition.public.title}」"
            ),
            inputDigest=digest,
        )
        with self._lock:
            self._previews[preview.preview_id] = PreviewRecord(preview, validated)
        return preview

    def start(self, preview_id: str, *, confirmed: bool, input_digest: str) -> CoreTestRun:
        if not confirmed:
            raise PermissionError("explicit confirmation is required")
        with self._lock:
            record = self._previews.get(preview_id)
            if record is None:
                raise KeyError(preview_id)
            if record.preview.expires_at < datetime.now(UTC):
                raise TimeoutError("preview expired; generate a new preview")
            if record.used:
                raise RuntimeError("preview has already started one run")
            if input_digest != record.preview.input_digest:
                raise PermissionError("input digest does not match the confirmed preview")
            record.used = True
            run = RunRecord(
                run_id=f"phase1-{uuid4().hex[:16]}",
                feature_id=record.preview.feature_id,
                inputs=record.preview.normalized_inputs,
                created_at=datetime.now(UTC),
            )
            self._runs[run.run_id] = run
            definition = self._definition(run.feature_id)
            self._pool.submit(self._execute, run.run_id, definition, record.inputs)
            return self._snapshot(run)

    def get_run(self, run_id: str) -> CoreTestRun:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise KeyError(run_id)
            return self._snapshot(run)

    def _execute(
        self,
        run_id: str,
        definition: FeatureDefinition,
        inputs: BaseModel,
    ) -> None:
        started = monotonic()
        with self._lock:
            run = self._runs[run_id]
            run.status = "running"
            run.started_at = datetime.now(UTC)
        self._append_log(run_id, "info", "测试开始", {"feature_id": run.feature_id})

        def log(level: Literal["info", "warning", "error"], message: str, details=None):
            self._append_log(run_id, level, message, details or {})

        try:
            result = self._jsonable(definition.executor(inputs, log))
            test_passed = not (
                isinstance(result, dict) and result.get("test_passed") is False
            )
            with self._lock:
                run = self._runs[run_id]
                run.result = result
                run.status = "passed" if test_passed else "failed"
                run.finished_at = datetime.now(UTC)
                run.duration_seconds = round(monotonic() - started, 3)
            self._append_log(
                run_id,
                "info" if test_passed else "error",
                "测试完成" if test_passed else "功能执行完成，但断言未通过",
                {"duration_seconds": round(monotonic() - started, 3)},
            )
        except Exception as exc:  # noqa: BLE001 - the run must retain the failure.
            with self._lock:
                run = self._runs[run_id]
                run.status = "failed"
                run.error = f"{type(exc).__name__}: {exc}"
                run.finished_at = datetime.now(UTC)
                run.duration_seconds = round(monotonic() - started, 3)
            self._append_log(
                run_id,
                "error",
                "测试异常终止",
                {"error_type": type(exc).__name__, "error": str(exc)[:2000]},
            )

    def _append_log(
        self,
        run_id: str,
        level: Literal["info", "warning", "error"],
        message: str,
        details: dict[str, Any],
    ) -> None:
        with self._lock:
            run = self._runs[run_id]
            entry = CoreTestLog(
                sequence=len(run.logs) + 1,
                occurredAt=datetime.now(UTC),
                level=level,
                message=message,
                details=self._jsonable(details),
            )
            run.logs.append(entry)
            if len(run.logs) > MAX_LOGS:
                run.logs[:] = run.logs[-MAX_LOGS:]

    def _definition(self, feature_id: str) -> FeatureDefinition:
        definition = self._features.get(feature_id)
        if definition is None:
            raise KeyError(feature_id)
        return definition

    def _snapshot(self, run: RunRecord) -> CoreTestRun:
        return CoreTestRun(
            runId=run.run_id,
            featureId=run.feature_id,
            status=run.status,
            createdAt=run.created_at,
            startedAt=run.started_at,
            finishedAt=run.finished_at,
            durationSeconds=run.duration_seconds,
            inputs=run.inputs,
            logs=list(run.logs),
            result=run.result,
            error=run.error,
        )

    def _resolve_input_paths(self, feature_id: str, inputs: dict[str, Any]) -> dict[str, Any]:
        value = json.loads(json.dumps(inputs))
        path_fields = {
            "git-clone": ["destination_path"],
            "codegraph-index": ["source_path"],
            "codegraph-query": ["source_path"],
            "static-candidates": ["candidate_set_path"],
            "harness-prompt": ["prompt_catalog_path"],
            "defect-scan": ["config_path"],
            "kubernetes-live": ["kubeconfig_path"],
            "contract-replay": ["report_path"],
        }
        for name in path_fields.get(feature_id, []):
            if value.get(name):
                value[name] = str(self._resolve_path(value[name]))
        if feature_id == "kubernetes-manifest":
            for source in value.get("sources", []):
                if isinstance(source, dict) and source.get("path"):
                    source["path"] = str(self._resolve_path(source["path"]))
        return value

    def _resolve_path(self, value: str | Path) -> Path:
        path = Path(value).expanduser()
        return path.resolve() if path.is_absolute() else (self.repo_path / path).resolve()

    @staticmethod
    def _reject_broad_path(path: Path, label: str) -> None:
        resolved = path.resolve()
        if resolved in {Path("/"), Path.home().resolve()}:
            raise ValueError(f"{label} cannot be a broad root path: {resolved}")

    def _validate_safe_inputs(self, feature_id: str, inputs: BaseModel) -> None:
        for name in (
            "source_path",
            "destination_path",
            "candidate_set_path",
            "prompt_catalog_path",
            "config_path",
            "report_path",
        ):
            path = getattr(inputs, name, None)
            if isinstance(path, Path):
                self._reject_broad_path(path, name)
        source_path = getattr(inputs, "source_path", None)
        if isinstance(source_path, Path) and not source_path.is_dir():
            raise ValueError(f"source_path is not a directory: {source_path}")
        for name in ("candidate_set_path", "prompt_catalog_path", "config_path", "report_path"):
            path = getattr(inputs, name, None)
            if isinstance(path, Path) and not path.is_file():
                raise ValueError(f"{name} is not a file: {path}")
        kubeconfig_path = getattr(inputs, "kubeconfig_path", None)
        if isinstance(kubeconfig_path, Path) and not kubeconfig_path.is_file():
            raise ValueError(f"kubeconfig_path is not a file: {kubeconfig_path}")
        if isinstance(inputs, KubernetesManifestInput):
            for source in inputs.sources:
                if not source.path.exists():
                    raise ValueError(f"Kubernetes source does not exist: {source.path}")
        if isinstance(inputs, GitCloneInput):
            if inputs.destination_path.exists() and not inputs.destination_path.is_dir():
                raise ValueError(
                    f"destination_path exists and is not a directory: {inputs.destination_path}"
                )
            if inputs.destination_path.is_dir() and any(inputs.destination_path.iterdir()):
                is_git_repository = (inputs.destination_path / ".git").is_dir()
                if inputs.existing_policy == "fail_if_exists" or not is_git_repository:
                    raise ValueError(
                        "destination_path is non-empty; choose an empty directory or "
                        f"an existing Git repository with existing_policy=update_existing: {inputs.destination_path}"
                    )
            if inputs.auth_mode == "gh_cli":
                # Preview remains available in an empty environment so the user
                # can see the missing authentication prerequisite before run.
                pass
            is_full_commit = len(inputs.revision) == 40 and all(
                character in "0123456789abcdef" for character in inputs.revision.lower()
            )
            if is_full_commit and inputs.clone_depth > 0:
                raise ValueError("clone_depth must be 0 when revision is a full commit SHA")
        command = getattr(inputs, "command", None)
        if command:
            resolved = shutil.which(str(command)) if "/" not in str(command) else str(command)
            if not resolved or Path(resolved).name != "codegraph":
                raise ValueError("command must resolve to the codegraph executable")

    def _build_features(self, overrides: dict[str, Executor]) -> dict[str, FeatureDefinition]:
        root = self.repo_path
        source = root.parent / "benchmark-sources/user-clones/otel-demo-2.2.0"
        candidate_set = root / "tasks/catalog/otel-demo-static-candidates.v1.yaml"
        prompt_catalog = root / "tasks/catalog/otel-demo-harness-prompts.v1.yaml"
        positive_config = root / "resilience_agent/config/semantic-scan.rd05-positive.yaml"
        positive_report = root / "resilience_agent/examples/semantic/generic-10-contract-report.json"

        def definition(
            feature: CoreFeature,
            input_model: type[BaseModel],
            executor: Executor,
            planner: PlanBuilder,
        ) -> FeatureDefinition:
            return FeatureDefinition(
                feature,
                input_model,
                overrides.get(feature.id, executor),
                planner,
            )

        features = [
            definition(
                CoreFeature(
                    id="git-clone",
                    group="source-codegraph",
                    title="GitHub 拉取源码",
                    description="使用用户指定 GitHub 地址、身份、revision 和目标目录拉取待分析代码。",
                    evidenceOutput="clone commands + observed Git HEAD/remote/files",
                    mutatesLocalState=True,
                    mayExceedFiveMinutes=True,
                    inputFields=[
                        _input_field("repository_url", "GitHub 地址", "string", "HTTPS Git URL，必须以 .git 结尾"),
                        _input_field("revision", "Revision", "string", "branch、tag 或完整 commit"),
                        _input_field("destination_path", "目标目录", "string", "源码拉取位置；已有同源 Git 仓库可更新"),
                        _input_field("auth_mode", "认证方式", "string", "public_https 或 gh_cli", allowed_values=["public_https", "gh_cli"]),
                        _input_field("github_account", "GitHub 身份", "string", "gh_cli 模式下核对当前登录账号", required=False),
                        _input_field("clone_depth", "Clone 深度", "integer", "0 表示完整历史，1–1000 表示浅克隆", required=False),
                        _input_field("existing_policy", "已有目录策略", "string", "更新同一 Git 仓库或目录存在即失败", required=False, allowed_values=["update_existing", "fail_if_exists"]),
                    ],
                    exampleInputs={
                        "repository_url": "https://github.com/open-telemetry/opentelemetry-demo.git",
                        "revision": "2.2.0",
                        "destination_path": str(root.parent / "benchmark-sources/user-clones/otel-demo-2.2.0"),
                        "auth_mode": "gh_cli",
                        "github_account": "Myzmzz",
                        "clone_depth": 1,
                        "existing_policy": "update_existing",
                    },
                ),
                GitCloneInput,
                self._execute_git_clone,
                self._plan_git_clone,
            ),
            definition(
                CoreFeature(
                    id="codegraph-index",
                    group="source-codegraph",
                    title="CodeGraph 建图/同步",
                    description="对用户指定源码执行 init/index/sync 并输出索引清单。",
                    evidenceOutput="CodeGraphManifest",
                    mutatesLocalState=True,
                    mayExceedFiveMinutes=True,
                    inputFields=[
                        _input_field("source_path", "源码目录", "string", "需要建立图索引的目录"),
                        _input_field("source_identity", "源码身份", "string", "用于索引 provenance"),
                        _input_field("command", "CodeGraph 命令", "string", "必须解析为 codegraph", required=False),
                        _input_field("force_reindex", "强制重建", "boolean", "忽略现有索引重新构建", required=False),
                        _input_field("timeout_seconds", "超时秒数", "integer", "30–1800", required=False),
                    ],
                    exampleInputs={
                        "source_path": str(source),
                        "source_identity": "otel-demo-2.2.0@b74a7bc7bbe66099c61951f42b24dab8b6f02d18",
                        "command": "codegraph",
                        "force_reindex": False,
                        "timeout_seconds": 600,
                    },
                ),
                CodeGraphIndexInput,
                self._execute_codegraph_index,
                lambda item: (
                    [
                        f"检查索引状态：{item.source_path}",
                        "根据状态执行 init/index/sync",
                        "验证 node/edge 数并计算 index SHA-256",
                    ],
                    ["会创建或修改源码目录下的 .codegraph"] ,
                ),
            ),
            definition(
                CoreFeature(
                    id="codegraph-query",
                    group="source-codegraph",
                    title="CodeGraph 交互查询",
                    description="执行 query/callers/callees/context/impact/services，并把结果转换为稳定 Evidence。",
                    evidenceOutput="Raw CodeGraph JSON + EvidenceRef[]",
                    mutatesLocalState=False,
                    mayExceedFiveMinutes=False,
                    inputFields=[
                        _input_field("source_path", "源码目录", "string", "已有 CodeGraph 索引的目录"),
                        _input_field("source_identity", "源码身份", "string", "证据 provenance"),
                        _input_field("command", "CodeGraph 命令", "string", "必须解析为 codegraph", required=False),
                        _input_field("operation", "查询类型", "string", "查询操作", allowed_values=["query", "callers", "callees", "context", "impact", "services"]),
                        _input_field("expression", "查询表达式", "string", "符号、搜索词或 context task"),
                        _input_field("kind", "符号类型", "string", "仅 query 使用", required=False),
                        _input_field("limit", "结果上限", "integer", "1–100", required=False),
                        _input_field("max_nodes", "Context 节点上限", "integer", "10–200", required=False),
                        _input_field("max_code_blocks", "代码块上限", "integer", "0–20", required=False),
                        _input_field("depth", "Impact 深度", "integer", "1–5，仅 impact 使用", required=False),
                    ],
                    exampleInputs={
                        "source_path": str(source),
                        "source_identity": "otel-demo-2.2.0@b74a7bc7bbe66099c61951f42b24dab8b6f02d18",
                        "command": "codegraph",
                        "operation": "services",
                        "expression": "有哪些服务呢",
                        "kind": None,
                        "limit": 20,
                        "max_nodes": 120,
                        "max_code_blocks": 8,
                        "depth": 2,
                    },
                ),
                CodeGraphQueryInput,
                self._execute_codegraph_query,
                lambda item: (
                    [
                        f"调用 codegraph {item.operation}",
                        "如果是服务清单问题，从 compose/k8s/source 派生确定性结果",
                        "保留原始 JSON",
                        "转换 node/edge/code block 为稳定 EvidenceRef",
                    ],
                    [],
                ),
            ),
            definition(
                CoreFeature(
                    id="static-candidates",
                    group="templates-defects",
                    title="OTel Demo 六个静态候选",
                    description="查看已经确定的六个源码级候选及其自然语言测试说明。",
                    evidenceOutput="selected candidate descriptions + test notes",
                    mutatesLocalState=False,
                    mayExceedFiveMinutes=False,
                    inputFields=[
                        _input_field("candidate_set_path", "候选集", "string", "OTel Demo静态候选YAML"),
                        _input_field(
                            "candidate_ids",
                            "候选 ID",
                            "string_array",
                            "需要查看或后续测试的一个到六个静态候选",
                            allowed_values=list(OTEL_DEMO_CANDIDATE_IDS),
                        ),
                    ],
                    exampleInputs={
                        "candidate_set_path": str(candidate_set),
                        "candidate_ids": list(OTEL_DEMO_CANDIDATE_IDS),
                    },
                ),
                StaticCandidateSetInput,
                self._execute_static_candidates,
                lambda item: (
                    ["读取已确定的静态候选集", f"选择候选：{', '.join(item.candidate_ids)}", "返回自然语言说明和后续测试关注点"],
                    [],
                ),
            ),
            definition(
                CoreFeature(
                    id="harness-prompt",
                    group="templates-defects",
                    title="生成 Harness 自然语言 Prompt",
                    description="为选定静态候选返回可以直接发送给任意Harness的完整中文Prompt。",
                    evidenceOutput="copy-ready natural-language Harness prompt",
                    mutatesLocalState=False,
                    mayExceedFiveMinutes=False,
                    inputFields=[
                        _input_field(
                            "prompt_catalog_path",
                            "Prompt目录",
                            "string",
                            "六个OTel Demo Harness Prompt注册表",
                        ),
                        _input_field(
                            "candidate_id",
                            "静态候选",
                            "string",
                            "选择六个OTel Demo候选之一",
                            allowed_values=list(OTEL_DEMO_CANDIDATE_IDS),
                        ),
                        _input_field(
                            "additional_instruction",
                            "补充要求",
                            "string",
                            "可选；追加到Prompt末尾的用户要求",
                            required=False,
                        ),
                    ],
                    exampleInputs={
                        "prompt_catalog_path": str(prompt_catalog),
                        "candidate_id": "OTEL-C04",
                        "additional_instruction": None,
                    },
                ),
                HarnessPromptInput,
                self._execute_harness_prompt,
                lambda item: (
                    [
                        f"读取候选Prompt：{item.candidate_id}",
                        "校验Prompt文件位于受控Harness Prompt目录",
                        "返回可直接复制给Harness的完整自然语言Prompt",
                    ],
                    ["这里只生成Prompt，不会调用Harness、访问集群或注入故障。"],
                ),
            ),
            definition(
                CoreFeature(
                    id="defect-scan",
                    group="templates-defects",
                    title="缺陷模板扫描与 Verifier",
                    description="按用户指定配置和模板运行 prepare→plan→analyze→verify→synthesize。",
                    evidenceOutput="SemanticScanReport + Evidence Ledger + Tool Traces",
                    mutatesLocalState=True,
                    mayExceedFiveMinutes=True,
                    inputFields=[
                        _input_field("config_path", "扫描配置", "string", "完整 semantic-scan config"),
                        _input_field(
                            "active_template_ids",
                            "激活模板",
                            "string_array",
                            "一个或多个通用模板",
                            allowed_values=list(ACTIVE_TEMPLATE_IDS),
                        ),
                        _input_field("planning_mode", "规划模式", "string", "deterministic 或 model", allowed_values=["deterministic", "model"]),
                        _input_field("run_id", "运行 ID", "string", "输出目录标识"),
                        _input_field("resume", "断点恢复", "boolean", "复用同一 run_id checkpoint", required=False),
                    ],
                    exampleInputs={
                        "config_path": str(positive_config),
                        "active_template_ids": list(ACTIVE_TEMPLATE_IDS),
                        "planning_mode": "deterministic",
                        "run_id": "semantic-user-generic-10",
                        "resume": False,
                    },
                ),
                DefectScanInput,
                self._execute_defect_scan,
                lambda item: (
                    [
                        f"加载扫描配置：{item.config_path}",
                        f"激活模板：{', '.join(item.active_template_ids)}",
                        "执行 CodeGraph/K8s prepare、模板分析、逐 Finding Verifier、结果合成",
                        "落盘 report、ledger、attempts、tool traces",
                    ],
                    [
                        "模板 Agent 和 Verifier 会访问模型网关并产生费用；deterministic 只替代 Coordinator 规划",
                        "可能建立/同步 CodeGraph 索引并写入输出目录",
                    ],
                ),
            ),
            definition(
                CoreFeature(
                    id="kubernetes-manifest",
                    group="kubernetes",
                    title="Kubernetes Manifest 扫描",
                    description="扫描用户指定 YAML/目录，规范化资源、脱敏并计算完整性与快照。",
                    evidenceOutput="KubernetesManifest + normalized resources",
                    mutatesLocalState=False,
                    mayExceedFiveMinutes=False,
                    inputFields=[
                        _input_field("namespace", "逻辑 Namespace", "string", "测试范围"),
                        _input_field("sources", "YAML 数据源", "object_array", "每项包含 alias/path"),
                        _input_field("authoritative_for_namespace", "是否完整权威", "boolean", "决定能否用缺失资源作反证", required=False),
                        _input_field("max_resources_per_agent", "资源上限", "integer", "10–500", required=False),
                    ],
                    exampleInputs={
                        "namespace": "semantic-fixture",
                        "sources": [{"alias": "positive-fixture", "path": "resilience_agent/examples/semantic/rd05-instance-positive/k8s.yaml"}],
                        "authoritative_for_namespace": True,
                        "max_resources_per_agent": 80,
                    },
                ),
                KubernetesManifestInput,
                self._execute_kubernetes_manifest,
                lambda item: (
                    [
                        f"解析 {len(item.sources)} 个 YAML/目录数据源",
                        "规范化 Workload/Service/PDB/HPA/Probe/资源字段并脱敏",
                        "输出 completeness、resource inventory 和 snapshot hash",
                    ],
                    ["authoritative=true 时，扫描结果会被允许用于资源缺失反证"] if item.authoritative_for_namespace else [],
                ),
            ),
            definition(
                CoreFeature(
                    id="kubernetes-live",
                    group="kubernetes",
                    title="Kubernetes Live 扫描",
                    description="使用用户指定 context/namespace 只读扫描 API Server。",
                    evidenceOutput="Live KubernetesManifest + runtime resources",
                    mutatesLocalState=False,
                    mayExceedFiveMinutes=False,
                    inputFields=[
                        _input_field("namespace", "Namespace", "string", "只读扫描范围"),
                        _input_field("context", "Kubernetes Context", "string", "明确指定，不使用模糊默认值"),
                        _input_field("kubeconfig_path", "Kubeconfig", "string", "为空时使用标准配置", required=False),
                        _input_field("authoritative_for_namespace", "是否完整权威", "boolean", "决定缺失证据语义", required=False),
                        _input_field("max_resources_per_agent", "资源上限", "integer", "10–500", required=False),
                    ],
                    exampleInputs={
                        "namespace": "semantic-fixture",
                        "context": "docker-desktop",
                        "kubeconfig_path": None,
                        "authoritative_for_namespace": True,
                        "max_resources_per_agent": 160,
                    },
                ),
                KubernetesLiveInput,
                self._execute_kubernetes_live,
                lambda item: (
                    [
                        f"只读连接 context={item.context}",
                        f"扫描 namespace={item.namespace} 的 Workload/Pod/Service/Endpoint/PDB/HPA/网格对象",
                        "记录每类资源 completeness、UID、imageID、Ready 和快照 hash",
                    ],
                    [f"将读取 Kubernetes context：{item.context}；不执行 create/update/delete"],
                ),
            ),
            definition(
                CoreFeature(
                    id="evidence-transform",
                    group="evidence-contracts",
                    title="Evidence 转换",
                    description="把用户提供的 CodeGraph 或 Kubernetes JSON 转换为稳定 EvidenceRef。",
                    evidenceOutput="EvidenceRef[]",
                    mutatesLocalState=False,
                    mayExceedFiveMinutes=False,
                    inputFields=[
                        _input_field("source_type", "数据类型", "string", "codegraph 或 kubernetes", allowed_values=["codegraph", "kubernetes"]),
                        _input_field("query_label", "查询标签", "string", "进入 Evidence statement"),
                        _input_field("payload", "原始 JSON", "object", "待转换的工具输出"),
                    ],
                    exampleInputs={
                        "source_type": "codegraph",
                        "query_label": "user-defined",
                        "payload": {"nodes": [], "edges": [], "codeBlocks": []},
                    },
                ),
                EvidenceTransformInput,
                self._execute_evidence_transform,
                lambda item: ([f"解析 {item.source_type} 原始 JSON", "生成稳定 EV-ID 和 source_hash"], []),
            ),
            definition(
                CoreFeature(
                    id="contract-replay",
                    group="evidence-contracts",
                    title="当前合同回放",
                    description="使用当前 SemanticScanReport Schema 重放用户指定历史结果。",
                    evidenceOutput="valid + validation_errors",
                    mutatesLocalState=False,
                    mayExceedFiveMinutes=False,
                    inputFields=[_input_field("report_path", "扫描报告", "string", "semantic-scan-report.json")],
                    exampleInputs={"report_path": str(positive_report)},
                ),
                ContractReplayInput,
                self._execute_contract_replay,
                lambda item: ([f"读取报告：{item.report_path}", "使用当前 Pydantic 合同验证全部字段"], []),
            ),
        ]
        return {item.public.id: item for item in features}

    @staticmethod
    def _jsonable(value: Any) -> Any:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))

    @classmethod
    def _plan_git_clone(cls, inputs: BaseModel) -> tuple[list[str], list[str]]:
        item = GitCloneInput.model_validate(inputs.model_dump())
        operations = []
        if item.auth_mode == "gh_cli":
            if item.github_account:
                operations.append(
                    f"使用 gh CLI 核对当前 GitHub 登录身份是否为 {item.github_account}"
                )
            else:
                operations.append("使用 gh CLI 读取并记录当前 GitHub 登录身份")
        else:
            operations.append("使用公开 HTTPS 方式访问 GitHub，不读取或接收 Token")
        existing_repository = (item.destination_path / ".git").is_dir()
        if existing_repository:
            configured_remote = cls._configured_origin(item.destination_path)
            if configured_remote is None:
                operations.append(
                    "执行："
                    + shlex.join(
                        [
                            "git",
                            "-C",
                            str(item.destination_path),
                            "remote",
                            "add",
                            "origin",
                            item.repository_url,
                        ]
                    )
                )
            else:
                operations.append(
                    f"核对已有仓库 origin={configured_remote} 与输入 repository_url 完全一致"
                )
            operations.append("检查已有源码工作区是否干净；忽略工具生成的 .codegraph 索引目录")
            visible_entries = [
                entry
                for entry in item.destination_path.iterdir()
                if entry.name not in {".git", ".codegraph"}
            ]
            if not visible_entries:
                operations.append(
                    "检测到只有 .git 的未完成 clone；恢复 HEAD 工作树后继续 fetch"
                )
                operations.append(
                    "执行："
                    + shlex.join(
                        [
                            "git",
                            "-C",
                            str(item.destination_path),
                            "reset",
                            "--hard",
                            "HEAD",
                        ]
                    )
                )
            operations.extend(
                f"执行：{shlex.join(command)}"
                for command in cls._git_update_commands(item)
            )
            warnings = [f"将在已有 Git 仓库中更新 revision：{item.destination_path}"]
            if not visible_entries:
                warnings.append(
                    "该目录只有 .git 且没有用户工作树文件；确认后会执行 reset --hard HEAD 恢复失败的 checkout"
                )
        else:
            operations.extend(
                f"执行：{shlex.join(command)}"
                for command in cls._git_clone_commands(item)
            )
            warnings = [f"将在目标位置创建源码目录：{item.destination_path}"]
        if item.clone_depth > 0:
            warnings.append(
                f"浅克隆深度为 {item.clone_depth}；若 revision 不在浅历史中，checkout 会失败"
            )
        if item.auth_mode == "gh_cli" and shutil.which("gh") is None:
            warnings.append(
                "当前环境未安装 gh CLI；执行前需安装 gh 并运行 gh auth login，或改用 public_https"
            )
        return operations, warnings

    @staticmethod
    def _configured_origin(destination: Path) -> str | None:
        config_path = destination / ".git" / "config"
        if not config_path.is_file():
            return None
        parser = configparser.RawConfigParser()
        parser.read(config_path, encoding="utf-8")
        section = 'remote "origin"'
        return parser.get(section, "url", fallback=None) if parser.has_section(section) else None

    @staticmethod
    def _git_clone_commands(item: GitCloneInput) -> list[list[str]]:
        git_flags = ["--no-checkout"]
        is_full_commit = len(item.revision) == 40 and all(
            character in "0123456789abcdef" for character in item.revision.lower()
        )
        if not is_full_commit:
            git_flags.extend(["--branch", item.revision])
        if item.clone_depth > 0:
            git_flags.extend(["--depth", str(item.clone_depth)])
        destination = str(item.destination_path)
        if item.auth_mode == "gh_cli":
            clone = [
                "gh",
                "repo",
                "clone",
                item.repository_url,
                destination,
                "--",
                *git_flags,
            ]
        else:
            clone = ["git", "clone", *git_flags, item.repository_url, destination]
        checkout = [
            "git",
            "-C",
            destination,
            "checkout",
            "--detach",
            item.revision,
        ]
        return [clone, checkout]

    @staticmethod
    def _git_update_commands(item: GitCloneInput) -> list[list[str]]:
        destination = str(item.destination_path)
        fetch = ["git", "-C", destination, "fetch", "--tags", "--prune"]
        if item.clone_depth > 0:
            fetch.extend(["--depth", str(item.clone_depth)])
        fetch.extend(["origin", item.revision])
        checkout = [
            "git",
            "-C",
            destination,
            "checkout",
            "--detach",
            "FETCH_HEAD",
        ]
        return [fetch, checkout]

    @staticmethod
    def _run_process(argv: list[str], log: LogSink, *, timeout: int) -> str:
        log("info", "执行命令", {"command": shlex.join(argv)})
        completed = subprocess.run(
            argv,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=timeout,
        )
        output = completed.stdout.strip()
        if output:
            log("info", "命令输出", {"output": output[-4000:]})
        if completed.returncode != 0:
            raise RuntimeError(
                f"command failed ({completed.returncode}): {shlex.join(argv)}: {output[-2000:]}"
            )
        return output

    @staticmethod
    def _is_generated_codegraph_status(line: str) -> bool:
        path = line[3:].strip().strip('"') if len(line) > 3 else ""
        return path == ".codegraph" or path.startswith(".codegraph/")

    @classmethod
    def _source_dirty_lines(cls, lines: list[str]) -> list[str]:
        return [
            line
            for line in lines
            if not cls._is_generated_codegraph_status(line)
        ]

    @classmethod
    def _execute_git_clone(cls, inputs: BaseModel, log: LogSink) -> dict[str, Any]:
        item = GitCloneInput.model_validate(inputs.model_dump())
        observed_account = None
        if item.auth_mode == "gh_cli":
            if shutil.which("gh") is None:
                raise RuntimeError(
                    "AUTH_NOT_READY: gh CLI is not installed; install gh and run "
                    "'gh auth login -h github.com -p https', or use auth_mode=public_https"
                )
            try:
                observed_account = cls._run_process(
                    ["gh", "api", "user", "--jq", ".login"], log, timeout=30
                ).strip()
            except RuntimeError as exc:
                raise RuntimeError(
                    "AUTH_NOT_READY: gh CLI is not authenticated; run "
                    "'gh auth login -h github.com -p https' and retry"
                ) from exc
            if item.github_account and observed_account != item.github_account:
                raise RuntimeError(
                    f"GitHub account mismatch: expected {item.github_account}, observed {observed_account}"
                )
            log("info", "GitHub 身份确认通过", {"account": observed_account})

        item.destination_path.parent.mkdir(parents=True, exist_ok=True)
        existing_repository = (item.destination_path / ".git").is_dir()
        if existing_repository:
            remote_before = cls._configured_origin(item.destination_path)
            if remote_before is None:
                log(
                    "warning",
                    "已有 Git 仓库没有 origin；按确认输入添加 remote",
                    {"repository_url": item.repository_url},
                )
                cls._run_process(
                    [
                        "git",
                        "-C",
                        str(item.destination_path),
                        "remote",
                        "add",
                        "origin",
                        item.repository_url,
                    ],
                    log,
                    timeout=30,
                )
                remote_before = item.repository_url
            if remote_before.rstrip("/") != item.repository_url.rstrip("/"):
                raise RuntimeError(
                    f"existing repository remote mismatch: expected {item.repository_url}, observed {remote_before}"
                )
            dirty = cls._run_process(
                ["git", "-C", str(item.destination_path), "status", "--porcelain"],
                log,
                timeout=30,
            ).splitlines()
            source_dirty = cls._source_dirty_lines(dirty)
            if dirty and not source_dirty:
                log(
                    "info",
                    "忽略工具生成的 CodeGraph 索引目录",
                    {"ignored_status_entries": dirty},
                )
            visible_entries = [
                entry
                for entry in item.destination_path.iterdir()
                if entry.name not in {".git", ".codegraph"}
            ]
            incomplete_checkout = (
                not visible_entries
                and bool(source_dirty)
                and all(line[:2].strip() == "D" for line in source_dirty)
            )
            if source_dirty and not incomplete_checkout:
                raise RuntimeError(
                    "existing repository has local changes; commit/stash them or choose another destination"
                )
            if incomplete_checkout:
                log(
                    "warning",
                    "恢复上一次失败留下的未完成 checkout",
                    {"deleted_index_entries": len(source_dirty)},
                )
                cls._run_process(
                    ["git", "-C", str(item.destination_path), "reset", "--hard", "HEAD"],
                    log,
                    timeout=300,
                )
            fetch_command, checkout_command = cls._git_update_commands(item)
            cls._run_process(fetch_command, log, timeout=1800)
            cls._run_process(checkout_command, log, timeout=300)
            clone_commands = [
                shlex.join(fetch_command),
                shlex.join(checkout_command),
            ]
        else:
            clone_command, checkout_command = cls._git_clone_commands(item)
            cls._run_process(clone_command, log, timeout=1800)
            cls._run_process(checkout_command, log, timeout=300)
            clone_commands = [
                shlex.join(clone_command),
                shlex.join(checkout_command),
            ]
        head = cls._run_process(
            ["git", "-C", str(item.destination_path), "rev-parse", "HEAD"],
            log,
            timeout=30,
        ).strip()
        remote = cls._run_process(
            ["git", "-C", str(item.destination_path), "remote", "get-url", "origin"],
            log,
            timeout=30,
        ).strip()
        status = cls._run_process(
            ["git", "-C", str(item.destination_path), "status", "--short", "--branch"],
            log,
            timeout=30,
        )
        files = cls._run_process(
            ["git", "-C", str(item.destination_path), "ls-files"],
            log,
            timeout=60,
        ).splitlines()
        log(
            "info",
            "源码拉取完成，可以进入 CodeGraph 建图",
            {"head": head, "tracked_files": len(files)},
        )
        return {
            "test_passed": True,
            "repository_url": item.repository_url,
            "revision": item.revision,
            "destination_path": str(item.destination_path),
            "auth_mode": item.auth_mode,
            "github_account": observed_account,
            "observed_remote": remote,
            "observed_head": head,
            "tracked_file_count": len(files),
            "git_status": status,
            "clone_commands": clone_commands,
        }

    @staticmethod
    def _codegraph_driver(item: CodeGraphIndexInput | CodeGraphQueryInput) -> CodeGraphDriver:
        return CodeGraphDriver(
            CodebaseConfig(
                id="user-defined-codebase",
                path=item.source_path,
                source_identity=item.source_identity,
            ),
            CodeGraphConfig(
                command=item.command,
                force_reindex=getattr(item, "force_reindex", False),
                timeout_seconds=getattr(item, "timeout_seconds", 600),
                max_nodes=getattr(item, "max_nodes", 40),
                max_code_blocks=getattr(item, "max_code_blocks", 8),
                max_query_results=getattr(item, "limit", 20),
            ),
        )

    @classmethod
    def _execute_codegraph_index(cls, inputs: BaseModel, log: LogSink) -> dict[str, Any]:
        item = CodeGraphIndexInput.model_validate(inputs.model_dump())
        log("info", "检查并建立 CodeGraph 索引", {"source_path": str(item.source_path)})
        started = monotonic()
        driver = cls._codegraph_driver(item)
        manifest = driver.ensure_index()
        indexing_seconds = round(monotonic() - started, 3)
        action_labels = {
            "indexed": "首次建立索引",
            "reindexed": "强制重建索引",
            "synced": "同步源码变更",
            "reused": "复用已有索引",
        }
        log(
            "info",
            f"CodeGraph：{action_labels.get(driver.last_index_action, '完成索引检查')}",
            {
                "index_action": driver.last_index_action,
                "indexing_seconds": indexing_seconds,
                "nodes": manifest.node_count,
                "edges": manifest.edge_count,
                "pending_changes": manifest.status.get("pendingChanges"),
            },
        )
        return {
            "test_passed": manifest.initialized and manifest.node_count > 0,
            "index_action": driver.last_index_action,
            "indexing_seconds": indexing_seconds,
            "manifest": manifest,
        }

    @staticmethod
    def _is_service_inventory_question(expression: str) -> bool:
        normalized = expression.strip().lower()
        if not normalized:
            return False
        chinese_service_question = "服务" in normalized and any(
            token in normalized
            for token in ("哪些", "有哪些", "列出", "列表", "清单", "多少", "什么")
        )
        english_service_question = any(
            token in normalized
            for token in (
                "what services",
                "which services",
                "list services",
                "service list",
                "service inventory",
            )
        )
        return chinese_service_question or english_service_question

    @staticmethod
    def _yaml_documents(path: Path) -> list[dict[str, Any]]:
        try:
            with path.open(encoding="utf-8") as handle:
                return [
                    document
                    for document in yaml.safe_load_all(handle)
                    if isinstance(document, dict)
                ]
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid YAML document: {path}: {exc}") from exc

    @staticmethod
    def _relative_path(path: Path, root: Path) -> str:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return str(path)

    @staticmethod
    def _line_number(path: Path, key: str) -> int | None:
        pattern = re.compile(rf"^\s{{2}}{re.escape(key)}:\s*(?:$|#)")
        try:
            with path.open(encoding="utf-8") as handle:
                for index, line in enumerate(handle, start=1):
                    if pattern.match(line):
                        return index
        except OSError:
            return None
        return None

    @staticmethod
    def _environment_value(environment: Any, key: str) -> str | None:
        if isinstance(environment, dict):
            value = environment.get(key)
            return str(value) if value not in (None, "") else None
        if isinstance(environment, list):
            for item in environment:
                if not isinstance(item, str) or "=" not in item:
                    continue
                name, value = item.split("=", 1)
                if name == key and value:
                    return value
        return None

    @staticmethod
    def _build_source_dir(config: dict[str, Any], source_path: Path) -> str | None:
        build = config.get("build")
        if isinstance(build, str):
            candidate = (source_path / build).resolve()
            if candidate.is_dir():
                return Phase1CoreService._relative_path(candidate, source_path)
        if isinstance(build, dict):
            context = build.get("context")
            if isinstance(context, str) and context not in {"", "."}:
                candidate = (source_path / context).resolve()
                if candidate.is_dir():
                    return Phase1CoreService._relative_path(candidate, source_path)
            dockerfile = build.get("dockerfile")
            if isinstance(dockerfile, str):
                candidate = (source_path / dockerfile).resolve().parent
                if candidate.is_dir() and candidate != source_path:
                    return Phase1CoreService._relative_path(candidate, source_path)
        return None

    @staticmethod
    def _known_source_dir(name: str, otel_service_name: str | None, source_path: Path) -> str | None:
        candidates = [
            name,
            otel_service_name or "",
            name.removesuffix("-web"),
            name.removesuffix("-service"),
        ]
        seen: set[str] = set()
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            path = source_path / "src" / candidate
            if path.is_dir():
                return Phase1CoreService._relative_path(path, source_path)
        return None

    @staticmethod
    def _service_category(name: str, config: dict[str, Any], otel_service_name: str | None) -> str:
        infrastructure_names = {
            "grafana",
            "jaeger",
            "kafka",
            "opensearch",
            "otel-collector",
            "postgresql",
            "prometheus",
            "redis",
            "valkey",
            "valkey-cart",
        }
        if name in infrastructure_names and not config.get("build"):
            return "infrastructure"
        if config.get("build") or otel_service_name:
            return "application"
        return "infrastructure" if name in infrastructure_names else "support"

    @staticmethod
    def _normalize_service_name(name: str) -> str:
        stripped = re.sub(r"^(opentelemetry-demo|otel-demo)-", "", name)
        return re.sub(r"[^a-z0-9]+", "", stripped.lower())

    @classmethod
    def _service_inventory(cls, source_path: Path, query: str) -> dict[str, Any]:
        services_by_name: dict[str, dict[str, Any]] = {}
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        related_files: set[str] = set()

        for filename in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
            compose_path = source_path / filename
            if not compose_path.is_file():
                continue
            related_files.add(cls._relative_path(compose_path, source_path))
            for document in cls._yaml_documents(compose_path):
                services = document.get("services")
                if not isinstance(services, dict):
                    continue
                for name, raw_config in services.items():
                    if not isinstance(name, str) or not isinstance(raw_config, dict):
                        continue
                    otel_service_name = (
                        cls._environment_value(raw_config.get("environment"), "OTEL_SERVICE_NAME")
                        or cls._environment_value(raw_config.get("environment"), "WEB_OTEL_SERVICE_NAME")
                    )
                    source_dir = (
                        cls._build_source_dir(raw_config, source_path)
                        or cls._known_source_dir(name, otel_service_name, source_path)
                    )
                    service = services_by_name.setdefault(
                        name,
                        {
                            "name": name,
                            "category": cls._service_category(name, raw_config, otel_service_name),
                            "container_name": raw_config.get("container_name"),
                            "otel_service_name": otel_service_name,
                            "image": raw_config.get("image"),
                            "source_dir": source_dir,
                            "compose_files": [],
                            "kubernetes_resources": [],
                            "depends_on": [],
                        },
                    )
                    service["compose_files"].append(cls._relative_path(compose_path, source_path))
                    depends_on = raw_config.get("depends_on")
                    if isinstance(depends_on, list):
                        service["depends_on"] = [str(item) for item in depends_on]
                    elif isinstance(depends_on, dict):
                        service["depends_on"] = [str(item) for item in depends_on]

        normalized_to_service = {
            cls._normalize_service_name(name): service
            for name, service in services_by_name.items()
        }
        manifest_paths = [
            path
            for directory in ("kubernetes", "k8s", "deploy", "deployments")
            if (source_path / directory).is_dir()
            for path in (source_path / directory).rglob("*.yaml")
        ]
        manifest_paths.extend(
            path
            for directory in ("kubernetes", "k8s", "deploy", "deployments")
            if (source_path / directory).is_dir()
            for path in (source_path / directory).rglob("*.yml")
        )
        seen_manifest_paths: set[Path] = set()
        for manifest_path in manifest_paths:
            resolved_manifest = manifest_path.resolve()
            if resolved_manifest in seen_manifest_paths:
                continue
            seen_manifest_paths.add(resolved_manifest)
            related_files.add(cls._relative_path(manifest_path, source_path))
            for document in cls._yaml_documents(manifest_path):
                kind = document.get("kind")
                metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
                name = metadata.get("name")
                if not isinstance(kind, str) or not isinstance(name, str):
                    continue
                if kind not in {"Deployment", "StatefulSet", "DaemonSet", "Service"}:
                    continue
                normalized = cls._normalize_service_name(name)
                service = normalized_to_service.get(normalized)
                if service is None:
                    continue
                service["kubernetes_resources"].append(
                    {
                        "kind": kind,
                        "name": name,
                        "file": cls._relative_path(manifest_path, source_path),
                    }
                )

        for service in sorted(services_by_name.values(), key=lambda item: item["name"]):
            service_node_id = f"service:{service['name']}"
            nodes.append(
                {
                    "id": service_node_id,
                    "kind": "service",
                    "name": service["name"],
                    "qualifiedName": service["name"],
                    "filePath": service["compose_files"][0] if service["compose_files"] else "",
                    "language": "yaml",
                    "startLine": cls._line_number(source_path / service["compose_files"][0], service["name"])
                    if service["compose_files"]
                    else None,
                    "metadata": {
                        "category": service["category"],
                        "otel_service_name": service["otel_service_name"],
                        "container_name": service["container_name"],
                    },
                }
            )
            source_dir = service.get("source_dir")
            if isinstance(source_dir, str) and source_dir:
                source_node_id = f"source_dir:{source_dir}"
                nodes.append(
                    {
                        "id": source_node_id,
                        "kind": "source_dir",
                        "name": source_dir,
                        "qualifiedName": source_dir,
                        "filePath": source_dir,
                        "language": "mixed",
                    }
                )
                edges.append(
                    {
                        "source": service_node_id,
                        "target": source_node_id,
                        "kind": "implemented_by",
                    }
                )
            for dependency in service.get("depends_on", []):
                if dependency in services_by_name:
                    edges.append(
                        {
                            "source": service_node_id,
                            "target": f"service:{dependency}",
                            "kind": "depends_on",
                        }
                    )
            for resource in service.get("kubernetes_resources", []):
                resource_node_id = f"k8s:{resource['kind']}:{resource['name']}"
                nodes.append(
                    {
                        "id": resource_node_id,
                        "kind": f"k8s_{str(resource['kind']).lower()}",
                        "name": resource["name"],
                        "qualifiedName": f"{resource['kind']}/{resource['name']}",
                        "filePath": resource["file"],
                        "language": "yaml",
                    }
                )
                edges.append(
                    {
                        "source": service_node_id,
                        "target": resource_node_id,
                        "kind": "declared_by",
                    }
                )

        unique_nodes: dict[str, dict[str, Any]] = {}
        for node in nodes:
            unique_nodes.setdefault(str(node["id"]), node)
        services = list(sorted(services_by_name.values(), key=lambda item: item["name"]))
        application_services = [
            service["name"]
            for service in services
            if service["category"] == "application"
        ]
        infrastructure_services = [
            service["name"]
            for service in services
            if service["category"] == "infrastructure"
        ]
        answer_parts = [
            f"从源码目录识别到 {len(services)} 个 compose 服务",
            f"其中应用/业务服务 {len(application_services)} 个",
            f"基础设施服务 {len(infrastructure_services)} 个",
        ]
        if application_services:
            answer_parts.append("应用/业务服务包括：" + "、".join(application_services))
        if infrastructure_services:
            answer_parts.append("基础设施服务包括：" + "、".join(infrastructure_services))
        return {
            "query": query,
            "summary": "；".join(answer_parts) + "。",
            "answer": "；".join(answer_parts) + "。",
            "service_count": len(services),
            "services": services,
            "entryPoints": [
                node for node in unique_nodes.values() if node.get("kind") == "service"
            ],
            "nodes": list(unique_nodes.values()),
            "edges": edges,
            "codeBlocks": [],
            "relatedFiles": sorted(related_files),
            "stats": {
                "nodeCount": len(unique_nodes),
                "edgeCount": len(edges),
                "fileCount": len(related_files),
                "codeBlockCount": 0,
                "totalCodeSize": 0,
            },
        }

    @staticmethod
    def _result_match_count(raw: Any) -> int:
        if isinstance(raw, list):
            return len(raw)
        if not isinstance(raw, dict):
            return 0
        total = 0
        for key in (
            "nodes",
            "entryPoints",
            "callers",
            "callees",
            "affected",
            "edges",
            "codeBlocks",
            "services",
        ):
            value = raw.get(key)
            if isinstance(value, list):
                total += len(value)
        service_count = raw.get("service_count")
        if isinstance(service_count, int):
            total += service_count
        return total

    @classmethod
    def _execute_codegraph_query(cls, inputs: BaseModel, log: LogSink) -> dict[str, Any]:
        item = CodeGraphQueryInput.model_validate(inputs.model_dump())
        driver = cls._codegraph_driver(item)
        resolved_operation = (
            "services"
            if item.operation == "services" or cls._is_service_inventory_question(item.expression)
            else item.operation
        )
        log(
            "info",
            "执行 CodeGraph 查询",
            {
                "operation": item.operation,
                "resolved_operation": resolved_operation,
                "expression": item.expression,
            },
        )
        if resolved_operation != item.operation:
            log(
                "info",
                "识别为服务清单查询，改用 compose/k8s/source 确定性抽取",
                {"requested_operation": item.operation, "resolved_operation": resolved_operation},
            )
        index_status = driver.status_snapshot()
        started = monotonic()
        if resolved_operation == "services":
            raw = cls._service_inventory(item.source_path, item.expression)
        elif resolved_operation == "query":
            raw = driver.query(item.expression, kind=item.kind, limit=item.limit)
        elif resolved_operation == "callers":
            raw = driver.callers(item.expression, limit=item.limit)
        elif resolved_operation == "callees":
            raw = driver.callees(item.expression, limit=item.limit)
        elif resolved_operation == "impact":
            raw = driver.impact(item.expression, depth=item.depth)
        else:
            raw = driver.context(
                item.expression,
                max_nodes=item.max_nodes,
                max_code_blocks=item.max_code_blocks,
            )
        ledger = EvidenceLedger()
        evidence = ledger.add_codegraph(raw, query=f"{resolved_operation}:{item.expression}")
        query_seconds = round(monotonic() - started, 3)
        match_count = cls._result_match_count(raw)
        query_status = "matched" if match_count > 0 else "no_match"
        log(
            "info" if query_status == "matched" else "warning",
            "CodeGraph 结果已转换为 Evidence",
            {
                "evidence_count": len(evidence),
                "match_count": match_count,
                "query_status": query_status,
                "query_seconds": query_seconds,
            },
        )
        return {
            "test_passed": query_status == "matched",
            "query_status": query_status,
            "resolved_operation": resolved_operation,
            "query_seconds": query_seconds,
            "index_overview": {
                "initialized": bool(index_status.get("initialized")),
                "file_count": int(index_status.get("fileCount") or 0),
                "node_count": int(index_status.get("nodeCount") or 0),
                "edge_count": int(index_status.get("edgeCount") or 0),
                "languages": [str(item) for item in index_status.get("languages", [])],
                "pending_changes": index_status.get("pendingChanges"),
            },
            "raw_result": raw,
            "evidence": evidence,
        }

    @staticmethod
    def _execute_static_candidates(inputs: BaseModel, log: LogSink) -> dict[str, Any]:
        item = StaticCandidateSetInput.model_validate(inputs.model_dump())
        value = yaml.safe_load(item.candidate_set_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != "otel-demo-static-candidate-set.v1":
            raise ValueError("candidate set must use otel-demo-static-candidate-set.v1")
        candidates = value.get("candidates")
        if not isinstance(candidates, list):
            raise ValueError("candidate set must contain a candidates list")
        known = {
            candidate.get("candidate_id"): candidate
            for candidate in candidates
            if isinstance(candidate, dict) and isinstance(candidate.get("candidate_id"), str)
        }
        missing = [candidate_id for candidate_id in item.candidate_ids if candidate_id not in known]
        if missing:
            raise ValueError("candidate IDs missing from file: " + ", ".join(missing))
        selected = [known[candidate_id] for candidate_id in item.candidate_ids]
        required_fields = {"candidate_id", "title", "description", "test_note"}
        if any(set(candidate) != required_fields for candidate in selected):
            raise ValueError("each candidate must contain only candidate_id, title, description, and test_note")
        runtime_confirmation = value.get("runtime_confirmation")
        runtime_confirmed = bool(
            isinstance(runtime_confirmation, dict) and runtime_confirmation.get("performed")
        )
        candidate_set_id = value.get("candidate_set_id")
        if not isinstance(candidate_set_id, str) or not candidate_set_id:
            raise ValueError("candidate set must define candidate_set_id")
        log(
            "info",
            "OTel Demo静态候选已读取",
            {"candidate_set_id": candidate_set_id, "selected": item.candidate_ids},
        )
        return {
            "test_passed": True,
            "candidate_set_id": candidate_set_id,
            "runtime_confirmed": runtime_confirmed,
            "candidates": selected,
            "recommended_test_order": value.get("recommended_test_order", []),
        }

    def _execute_harness_prompt(self, inputs: BaseModel, log: LogSink) -> dict[str, Any]:
        item = HarnessPromptInput.model_validate(inputs.model_dump())
        catalog = yaml.safe_load(item.prompt_catalog_path.read_text(encoding="utf-8"))
        if not isinstance(catalog, dict) or catalog.get("schema_version") != "otel-demo-harness-prompt-catalog.v1":
            raise ValueError("prompt catalog must use otel-demo-harness-prompt-catalog.v1")
        entries = catalog.get("entries")
        if not isinstance(entries, list):
            raise ValueError("prompt catalog must contain an entries list")
        matches = [
            entry for entry in entries
            if isinstance(entry, dict) and entry.get("candidate_id") == item.candidate_id
        ]
        if len(matches) != 1 or not isinstance(matches[0].get("prompt_file"), str):
            raise ValueError(f"candidate Prompt is missing or duplicated: {item.candidate_id}")
        prompt_root = (self.repo_path / "harness/prompts/otel-demo-core-six").resolve()
        prompt_path = (self.repo_path / matches[0]["prompt_file"]).resolve()
        try:
            prompt_path.relative_to(prompt_root)
        except ValueError as exc:
            raise ValueError("candidate Prompt must stay under harness/prompts/otel-demo-core-six") from exc
        if not prompt_path.is_file():
            raise ValueError(f"candidate Prompt file does not exist: {prompt_path.name}")
        prompt = prompt_path.read_text(encoding="utf-8").strip()
        if len(prompt) < 200 or len(prompt) > 12_000:
            raise ValueError("candidate Prompt length is outside the accepted range")
        if item.additional_instruction:
            prompt = f"{prompt}\n\n补充要求：{item.additional_instruction}"
        candidate_set_path = self.repo_path / "tasks/catalog/otel-demo-static-candidates.v1.yaml"
        candidate_set = yaml.safe_load(candidate_set_path.read_text(encoding="utf-8"))
        candidates = candidate_set.get("candidates", []) if isinstance(candidate_set, dict) else []
        candidate = next(
            (
                value for value in candidates
                if isinstance(value, dict) and value.get("candidate_id") == item.candidate_id
            ),
            None,
        )
        if not isinstance(candidate, dict) or not isinstance(candidate.get("title"), str):
            raise ValueError(f"candidate metadata is missing: {item.candidate_id}")
        log(
            "info",
            "Harness自然语言Prompt已生成",
            {
                "candidate_id": item.candidate_id,
                "execution_status": "not_executed",
            },
        )
        return {
            "test_passed": True,
            "candidate_id": item.candidate_id,
            "candidate_title": candidate["title"],
            "prompt": prompt,
            "prompt_file": prompt_path.relative_to(self.repo_path).as_posix(),
            "interaction_mode": "direct_natural_language",
            "ready_to_copy": True,
            "runtime_execution_status": "not_executed",
        }

    @staticmethod
    def _execute_defect_scan(inputs: BaseModel, log: LogSink) -> dict[str, Any]:
        item = DefectScanInput.model_validate(inputs.model_dump())
        config = load_semantic_scan_config(item.config_path).model_copy(
            update={
                "active_template_ids": item.active_template_ids,
                "planning_mode": item.planning_mode,
            }
        )

        def event_sink(event: dict[str, Any]) -> None:
            kind = str(event.get("event") or event.get("kind") or "workflow_event")
            log("info", kind, event)

        log("info", "创建缺陷扫描 Workflow", {"templates": item.active_template_ids})
        report = SemanticScanWorkflow(
            config,
            run_id=item.run_id,
            resume=item.resume,
            event_sink=event_sink,
        ).run()
        log("info", "扫描报告已生成", {"matches": len(report.matches), "question_eligible": report.question_eligible_count})
        return {"test_passed": True, "report": report}

    @staticmethod
    def _execute_kubernetes_manifest(inputs: BaseModel, log: LogSink) -> dict[str, Any]:
        item = KubernetesManifestInput.model_validate(inputs.model_dump())
        scanner = KubernetesConfigScanner(
            KubernetesConfig(
                mode="manifest",
                namespace=item.namespace,
                sources=[
                    KubernetesSourceConfig(alias=source.alias, path=source.path)
                    for source in item.sources
                ],
                max_resources_per_agent=item.max_resources_per_agent,
                authoritative_for_namespace=item.authoritative_for_namespace,
            )
        )
        log("info", "开始解析 Kubernetes Manifest", {"source_count": len(item.sources)})
        manifest = scanner.scan()
        log("info", "Manifest 扫描完成", {"resources": manifest.resource_count, "kinds": manifest.kinds})
        return {
            "test_passed": True,
            "manifest": manifest,
            "resources": scanner.resources,
            "non_resource_documents": scanner.non_resource_documents,
        }

    @staticmethod
    def _execute_kubernetes_live(inputs: BaseModel, log: LogSink) -> dict[str, Any]:
        item = KubernetesLiveInput.model_validate(inputs.model_dump())
        log("info", "连接 Kubernetes API Server", {"context": item.context, "namespace": item.namespace})
        scanner = LiveKubernetesScanner(
            item.namespace,
            kubeconfig=str(item.kubeconfig_path) if item.kubeconfig_path else None,
            context=item.context,
            max_resources_per_agent=item.max_resources_per_agent,
            authoritative_for_namespace=item.authoritative_for_namespace,
        )
        manifest = scanner.scan()
        unavailable = {
            kind: state
            for kind, state in manifest.completeness.items()
            if state.get("status") == "unavailable"
        }
        if unavailable:
            log("warning", "部分 Kubernetes 资源类别不可用", unavailable)
        log("info", "Kubernetes Live 扫描完成", {"resources": manifest.resource_count})
        return {
            "test_passed": not unavailable,
            "manifest": manifest,
            "resources": scanner.resources,
        }

    @staticmethod
    def _execute_evidence_transform(inputs: BaseModel, log: LogSink) -> dict[str, Any]:
        item = EvidenceTransformInput.model_validate(inputs.model_dump())
        ledger = EvidenceLedger()
        if item.source_type == "codegraph":
            evidence = ledger.add_codegraph(item.payload, query=item.query_label)
        else:
            if not isinstance(item.payload, dict):
                raise ValueError("Kubernetes evidence payload must be an object")
            evidence = ledger.add_kubernetes(item.payload)
        log("info", "Evidence 转换完成", {"count": len(evidence)})
        return {"test_passed": bool(evidence), "evidence": evidence}

    @staticmethod
    def _execute_contract_replay(inputs: BaseModel, log: LogSink) -> dict[str, Any]:
        item = ContractReplayInput.model_validate(inputs.model_dump())
        raw = json.loads(item.report_path.read_text(encoding="utf-8"))
        log("info", "使用当前 SemanticScanReport 合同开始回放", {"report_path": str(item.report_path)})
        try:
            report = SemanticScanReport.model_validate(raw)
        except ValidationError as exc:
            errors = exc.errors(include_url=False)
            log("error", "合同回放失败", {"validation_error_count": len(errors)})
            return {"test_passed": False, "valid": False, "validation_errors": errors}
        log("info", "合同回放通过", {"matches": len(report.matches)})
        return {
            "test_passed": True,
            "valid": True,
            "report": report.model_dump(mode="json"),
        }
