#!/usr/bin/env python3
"""Run the OTel semantic scan pipeline on the test-cluster worker.

The command is dry-run by default. ``--execute`` is required before it verifies
source locks, reads the live Kubernetes API, calls the semantic scan agents, or
writes run artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import yaml

from resilience_agent.semantic_scan.codegraph_driver import CodeGraphDriver
from resilience_agent.semantic_scan.config import load_semantic_scan_config
from resilience_agent.semantic_scan.kubernetes_scanner import LiveKubernetesScanner
from resilience_agent.semantic_scan.source_runtime import (
    ImageReference,
    build_source_runtime_manifest,
    detect_git_snapshot,
    live_bindings_from_kubernetes_resources,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SYSTEM_ROOT = Path("/data/mj/resbench-system")
DEFAULT_RUN_ROOT = Path("/data/mj/resbench-runs")
DEFAULT_REPOSITORY_URL = "https://github.com/open-telemetry/opentelemetry-demo.git"
DEFAULT_REVISION = "2.2.0"
DEFAULT_SCAN_CONFIG = REPO_ROOT / "resilience_agent/config/semantic-scan.otel-demo.yaml"
DEFAULT_EPISODE_CONFIG = REPO_ROOT / "resilience_agent/config/episode-generation.rd14-positive.yaml"
DEFAULT_NAMESPACE = "otel-demo"
DEFAULT_KUBECONFIG_REF = "/data/mj/resbench-system/kubeconfig"
EXPECTED_OTEL_COMMIT = "b74a7bc7bbe66099c61951f42b24dab8b6f02d18"
RUN_ID_RE = re.compile(r"^semantic-[a-z0-9-]{8,80}$")
SOURCE_CACHE_SCHEMA = "resbench-source-cache.v1"
SOURCE_CACHE_ROOT = "otel-demo-2.2.0"


class PipelineError(RuntimeError):
    """Expected bounded pipeline failure."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="perform the scan")
    parser.add_argument(
        "--qualification-only",
        action="store_true",
        help="complete source, API, image, CodeGraph and ChaosBlade qualification without calling the model",
    )
    parser.add_argument("--run-id", help="safe semantic-* identifier")
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--system-root", type=Path, default=DEFAULT_SYSTEM_ROOT)
    parser.add_argument(
        "--source-cache-dir",
        type=Path,
        help="verified source snapshot cache; defaults to <system-root>/source-cache",
    )
    parser.add_argument("--repository-url", default=DEFAULT_REPOSITORY_URL)
    parser.add_argument("--revision", default=DEFAULT_REVISION)
    parser.add_argument("--expected-commit", default=EXPECTED_OTEL_COMMIT)
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument("--scan-config", type=Path, default=DEFAULT_SCAN_CONFIG)
    parser.add_argument("--episode-config", type=Path, default=DEFAULT_EPISODE_CONFIG)
    parser.add_argument("--kubeconfig", type=Path, help="optional kubectl kubeconfig")
    parser.add_argument("--kubeconfig-ref", default=DEFAULT_KUBECONFIG_REF)
    parser.add_argument("--codegraph-command", default=os.environ.get("RESBENCH_CODEGRAPH_COMMAND", "codegraph"))
    parser.add_argument("--crane-command", default=os.environ.get("RESBENCH_CRANE_COMMAND", "crane"))
    parser.add_argument(
        "--blade-command",
        default=os.environ.get(
            "RESBENCH_CHAOSBLADE_COMMAND",
            "/data/mj/resbench-tools/chaosblade-1.8.0-linux_amd64/blade",
        ),
    )
    return parser


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_id() -> str:
    now = datetime.now(timezone.utc)
    digest = hashlib.sha256(now.isoformat().encode()).hexdigest()[:8]
    return f"semantic-{now:%Y%m%dt%H%M%sz}-{digest}"


def _json_line(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_yaml(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _safe_resolve(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved == Path("/") or resolved == Path.home().resolve():
        raise PipelineError(f"unsafe broad path: {resolved}")
    return resolved


def _kubectl_base(kubeconfig: Path | None) -> list[str]:
    argv = ["kubectl"]
    if kubeconfig is not None:
        argv.extend(["--kubeconfig", str(kubeconfig.expanduser().resolve())])
    return argv


def _run_checked(argv: Sequence[str], *, cwd: Path | None = None, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(argv),
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise PipelineError(f"{Path(argv[0]).name} failed with exit {completed.returncode}: {completed.stderr[:1200]}")
    return completed


def _source_cache_paths(cache_dir: Path, expected_commit: str) -> tuple[Path, Path]:
    stem = f"otel-demo-2.2.0-{expected_commit}"
    return cache_dir / f"{stem}.tar.gz", cache_dir / f"{stem}.manifest.json"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_source_cache_bundle(
    source: Path,
    cache_dir: Path,
    *,
    repository_url: str,
    revision: str,
    expected_commit: str,
) -> tuple[Path, Path]:
    snapshot = detect_git_snapshot(
        source,
        expected_version=revision,
        expected_commit=expected_commit,
    )
    if snapshot.status != "exact":
        raise PipelineError(
            f"source cache input mismatch: expected {expected_commit}, observed {snapshot.observed_commit}"
        )
    cache_dir = _safe_resolve(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive_path, manifest_path = _source_cache_paths(cache_dir, expected_commit)
    with tempfile.NamedTemporaryFile(
        prefix="otel-demo-source-",
        suffix=".tar.gz",
        dir=cache_dir,
        delete=False,
    ) as temporary:
        temporary_archive = Path(temporary.name)

    def archive_filter(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
        parts = PurePosixPath(member.name).parts
        if any(part in {".git", ".codegraph"} for part in parts):
            return None
        if parts and parts[-1] == ".source-revision":
            return None
        member.uid = 0
        member.gid = 0
        member.uname = ""
        member.gname = ""
        return member

    try:
        with tarfile.open(temporary_archive, "w:gz") as archive:
            archive.add(source, arcname=SOURCE_CACHE_ROOT, filter=archive_filter)
            marker = (
                f"repository_url={repository_url}\n"
                f"revision={revision}\n"
                f"commit={expected_commit}\n"
            ).encode()
            marker_info = tarfile.TarInfo(f"{SOURCE_CACHE_ROOT}/.source-revision")
            marker_info.size = len(marker)
            marker_info.mode = 0o644
            archive.addfile(marker_info, io.BytesIO(marker))
        archive_sha256 = _sha256_file(temporary_archive)
        os.replace(temporary_archive, archive_path)
    finally:
        temporary_archive.unlink(missing_ok=True)
    temporary_manifest = manifest_path.with_suffix(".json.tmp")
    _write_json(
        temporary_manifest,
        {
            "schema_version": SOURCE_CACHE_SCHEMA,
            "repository_url": repository_url,
            "revision": revision,
            "commit": expected_commit,
            "archive": archive_path.name,
            "archive_sha256": archive_sha256,
            "archive_bytes": archive_path.stat().st_size,
            "created_at": _utc_now(),
        },
    )
    os.replace(temporary_manifest, manifest_path)
    return archive_path, manifest_path


def _materialize_source_workspace(
    repository_url: str,
    revision: str,
    expected_commit: str,
    run_dir: Path,
    cache_dir: Path,
) -> tuple[Path, str]:
    archive_path, manifest_path = _source_cache_paths(cache_dir, expected_commit)
    if archive_path.exists() or manifest_path.exists():
        if not archive_path.is_file() or not manifest_path.is_file():
            raise PipelineError("source cache archive and manifest must both exist")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = {
            "schema_version": SOURCE_CACHE_SCHEMA,
            "repository_url": repository_url,
            "revision": revision,
            "commit": expected_commit,
            "archive": archive_path.name,
        }
        for key, value in expected.items():
            if manifest.get(key) != value:
                raise PipelineError(f"source cache manifest mismatch for {key}")
        if manifest.get("archive_sha256") != _sha256_file(archive_path):
            raise PipelineError("source cache archive SHA256 mismatch")
        extract_root = run_dir / "workspace/cache-extracted"
        extract_root.mkdir(parents=True, exist_ok=False)
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                parts = PurePosixPath(member.name).parts
                if (
                    not parts
                    or parts[0] != SOURCE_CACHE_ROOT
                    or PurePosixPath(member.name).is_absolute()
                    or ".." in parts
                    or member.issym()
                    or member.islnk()
                ):
                    raise PipelineError(f"unsafe source cache member: {member.name}")
            archive.extractall(extract_root, filter="data")
        extracted = extract_root / SOURCE_CACHE_ROOT
        workspace = run_dir / "workspace/otel-demo-2.2.0"
        if not extracted.is_dir() or workspace.exists():
            raise PipelineError("source cache did not contain the expected repository root")
        extracted.rename(workspace)
        extract_root.rmdir()
        snapshot = detect_git_snapshot(
            workspace,
            expected_version=revision,
            expected_commit=expected_commit,
        )
        if snapshot.status != "exact":
            raise PipelineError("materialized source cache identity mismatch")
        return workspace, "verified_cache"
    workspace = _clone_source_workspace(
        repository_url,
        revision,
        expected_commit,
        run_dir,
    )
    _write_source_cache_bundle(
        workspace,
        cache_dir,
        repository_url=repository_url,
        revision=revision,
        expected_commit=expected_commit,
    )
    return workspace, "git_clone"


def _clone_source_workspace(
    repository_url: str,
    revision: str,
    expected_commit: str,
    run_dir: Path,
) -> Path:
    parsed = urlparse(repository_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise PipelineError("repository URL must be an HTTPS Git URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise PipelineError("repository URL must not contain credentials, query, or fragment")
    if not parsed.path.endswith(".git"):
        raise PipelineError("repository URL must end with .git")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", revision):
        raise PipelineError("invalid Git revision")
    if not re.fullmatch(r"[a-f0-9]{40}", expected_commit):
        raise PipelineError("expected commit must be a full SHA-1")
    workspace = run_dir / "workspace/otel-demo-2.2.0"
    workspace.parent.mkdir(parents=True, exist_ok=True)
    try:
        from dulwich import porcelain
        from dulwich.index import build_index_from_tree
        from dulwich.objects import Tag
        from dulwich.repo import Repo
    except ImportError as exc:  # pragma: no cover - dependency is runtime-bound.
        raise PipelineError("dulwich is required for HTTPS Git source materialization") from exc
    try:
        porcelain.clone(
            repository_url,
            str(workspace),
            checkout=False,
            errstream=io.BytesIO(),
        )
        repo = Repo(str(workspace))
        commit_id = _resolve_dulwich_revision(repo, revision, Tag)
        if commit_id != expected_commit:
            raise PipelineError(
                f"revision {revision} resolved to {commit_id}, expected {expected_commit}"
            )
        commit = repo[commit_id.encode("ascii")]
        repo.refs[b"HEAD"] = commit.id
        build_index_from_tree(
            repo.path,
            repo.index_path(),
            repo.object_store,
            commit.tree,
        )
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError(f"HTTPS Git clone/checkout failed: {type(exc).__name__}: {exc}") from exc
    (workspace / ".source-revision").write_text(
        f"repository_url={repository_url}\nrevision={revision}\ncommit={expected_commit}\n",
        encoding="utf-8",
    )
    return workspace


def _resolve_dulwich_revision(repo: Any, revision: str, tag_type: type[Any]) -> str:
    raw = revision.encode("utf-8")
    candidates = [
        raw,
        b"refs/tags/" + raw,
        b"refs/heads/" + raw,
        b"refs/remotes/origin/" + raw,
    ]
    oid: bytes | None = None
    if re.fullmatch(r"[a-f0-9]{40}", revision):
        oid = raw
    for candidate in candidates:
        if oid is not None:
            break
        try:
            oid = repo.refs[candidate]
        except KeyError:
            continue
    if oid is None:
        raise PipelineError(f"revision not found in cloned repository: {revision}")
    obj = repo[oid]
    while isinstance(obj, tag_type):
        _, peeled = obj.object
        oid = peeled
        obj = repo[oid]
    if getattr(obj, "type_name", None) != b"commit":
        raise PipelineError(f"revision does not resolve to a commit: {revision}")
    return obj.id.decode("ascii")


def _crane_reference(
    command: str,
    image: str,
    *,
    insecure: bool,
    known_digest: str | None = None,
) -> ImageReference:
    digest = known_digest or _run_checked(
        [
            command,
            "digest",
            "--platform",
            "linux/amd64",
            *(["--insecure"] if insecure else []),
            image,
        ],
        timeout=120,
    ).stdout.strip()
    manifest = json.loads(
        _run_checked(
            [command, "manifest", "--platform", "linux/amd64", *( ["--insecure"] if insecure else [] ), image],
            timeout=120,
        ).stdout
    )
    config = json.loads(
        _run_checked(
            [command, "config", *( ["--insecure"] if insecure else [] ), image],
            timeout=120,
        ).stdout
    )
    runtime = config.get("config") if isinstance(config.get("config"), dict) else {}
    return ImageReference(
        image=image,
        digest=digest,
        config_digest=(manifest.get("config") or {}).get("digest"),
        layers=[str(item.get("digest")) for item in manifest.get("layers", []) if item.get("digest")],
        entrypoint=[str(item) for item in runtime.get("Entrypoint") or []],
        command=[str(item) for item in runtime.get("Cmd") or []],
        source_revision=(runtime.get("Labels") or {}).get("org.opencontainers.image.revision"),
    )


def _source_runtime_manifest(
    source: Path,
    scanner: LiveKubernetesScanner,
    crane_command: str,
    expected_commit: str,
):
    snapshot = detect_git_snapshot(
        source,
        expected_version="2.2.0",
        expected_commit=expected_commit,
    )
    if snapshot.status != "exact":
        raise PipelineError(
            f"OTel Demo source identity mismatch: expected {expected_commit}, observed {snapshot.observed_commit}"
        )
    bindings = live_bindings_from_kubernetes_resources(scanner.resources)
    official: dict[str, ImageReference] = {}

    def enrich(binding: Any) -> tuple[Any, ImageReference | None]:
        if "/observe/otel-demo:2.2.0-" not in binding.image:
            return binding, None
        tag = binding.image.rsplit(":", 1)[-1]
        try:
            known_digest = (
                binding.image_id.rsplit("@", 1)[-1]
                if binding.image_id and "@" in binding.image_id
                else None
            )
            live_ref = _crane_reference(
                crane_command,
                binding.image,
                insecure=True,
                known_digest=known_digest,
            )
            official_ref = _crane_reference(
                crane_command,
                f"ghcr.io/open-telemetry/demo:{tag}",
                insecure=False,
            )
        except (OSError, PipelineError, json.JSONDecodeError):
            return binding, None
        binding.config_digest = live_ref.config_digest
        binding.layers = live_ref.layers
        binding.entrypoint = live_ref.entrypoint
        binding.command = live_ref.command
        binding.source_revision = live_ref.source_revision
        return binding, official_ref

    with ThreadPoolExecutor(max_workers=4) as executor:
        for binding, official_ref in executor.map(enrich, bindings):
            if official_ref is not None:
                official[binding.container] = official_ref
    return build_source_runtime_manifest(snapshot, bindings, official)


def _qualify_chaosblade(
    blade_command: str,
    kubeconfig: Path | None,
    output: Path,
) -> dict[str, Any]:
    version_output = _run_checked([blade_command, "version"], timeout=30).stdout
    version_match = re.search(r"Version:\s*([^\s]+)", version_output)
    if not version_match:
        raise PipelineError("unable to determine ChaosBlade version")
    version = version_match.group(1)
    _run_checked(
        [*_kubectl_base(kubeconfig), "get", "crd", "chaosblades.chaosblade.io", "-o", "name"],
        timeout=30,
    )
    daemonset = json.loads(
        _run_checked(
            [
                *_kubectl_base(kubeconfig),
                "-n",
                "default",
                "get",
                "daemonset",
                "chaosblade-tool",
                "-o",
                "json",
            ],
            timeout=30,
        ).stdout
    )
    status = daemonset.get("status") or {}
    desired = int(status.get("desiredNumberScheduled") or 0)
    ready = int(status.get("numberReady") or 0)
    operator_ready = desired > 0 and ready == desired
    command_kinds = {
        "network-delay": ["pod-network", "delay"],
        "network-loss": ["pod-network", "loss"],
        "cpu-load": ["pod-cpu", "fullload"],
        "memory-load": ["pod-mem", "load"],
        "pod-delete": ["pod-pod", "delete"],
        "pod-fail": ["pod-pod", "fail"],
    }
    capabilities = []
    verified_at = _utc_now()
    for fault_type, command_parts in command_kinds.items():
        help_result = subprocess.run(
            [blade_command, "create", "k8s", *command_parts, "--help"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        verified = help_result.returncode == 0 and operator_ready
        capabilities.append(
            {
                "fault_type": fault_type,
                "actuator": "ChaosBlade",
                "status": "verified" if verified else "unverified",
                "command_kind": "k8s " + " ".join(command_parts),
                "blade_version": version,
                "verification_source": (
                    "live blade command help, chaosblades.chaosblade.io CRD, "
                    "and chaosblade-tool DaemonSet readiness"
                ),
                "verified_at": verified_at if verified else None,
                "reason": "" if verified else "CLI help or operator readiness preflight failed",
            }
        )
    manifest = {
        "schema_version": "chaosblade-capability-manifest.v1",
        "registry_version": f"chaosblade-{version}-live-{verified_at}",
        "generated_at": verified_at,
        "namespace": "resbench-system",
        "capabilities": capabilities,
    }
    _write_yaml(output, manifest)
    return manifest


def _ready_pod_aliases(pod: dict[str, Any]) -> Iterable[str]:
    metadata = pod.get("metadata", {})
    spec = pod.get("spec", {})
    name = str(metadata.get("name", ""))
    labels = metadata.get("labels", {}) if isinstance(metadata.get("labels"), dict) else {}
    owner_refs = metadata.get("ownerReferences", []) if isinstance(metadata.get("ownerReferences"), list) else []
    containers = spec.get("containers", []) if isinstance(spec.get("containers"), list) else []
    candidates = [
        name,
        labels.get("app.kubernetes.io/name"),
        labels.get("app.kubernetes.io/component"),
        labels.get("app"),
        labels.get("component"),
        labels.get("service"),
    ]
    for owner in owner_refs:
        if isinstance(owner, dict):
            candidates.append(owner.get("name"))
    for container in containers:
        if isinstance(container, dict):
            candidates.append(container.get("name"))
    if name:
        candidates.append(re.sub(r"-[a-f0-9]{8,10}-[a-z0-9]{5}$", "", name))
    seen: set[str] = set()
    for raw in candidates:
        if not raw:
            continue
        alias = str(raw).strip().lower()
        if not alias or alias in seen:
            continue
        seen.add(alias)
        yield alias


def _pod_ready(pod: dict[str, Any]) -> bool:
    conditions = pod.get("status", {}).get("conditions", [])
    if not isinstance(conditions, list):
        return False
    return any(item.get("type") == "Ready" and item.get("status") == "True" for item in conditions if isinstance(item, dict))


def _runtime_bindings(
    namespace: str,
    kubeconfig: Path | None,
    source_runtime: Any,
) -> list[dict[str, Any]]:
    completed = _run_checked([*_kubectl_base(kubeconfig), "-n", namespace, "get", "pods", "-o", "json"], timeout=60)
    pod_list = json.loads(completed.stdout)
    items = pod_list.get("items", []) if isinstance(pod_list, dict) else []
    bindings: list[dict[str, Any]] = []
    used_components: set[tuple[str, str]] = set()
    bound_at = _utc_now()
    runtime_by_pod: dict[str, list[Any]] = {}
    for item in source_runtime.runtime:
        if item.pod_name:
            runtime_by_pod.setdefault(item.pod_name, []).append(item)
    for pod in items:
        if not isinstance(pod, dict) or not _pod_ready(pod):
            continue
        metadata = pod.get("metadata", {})
        pod_name = str(metadata.get("name", ""))
        pod_uid = str(metadata.get("uid", ""))
        if not pod_name or not pod_uid:
            continue
        for alias in _ready_pod_aliases(pod):
            key = (namespace, alias)
            if key in used_components:
                continue
            used_components.add(key)
            comparisons = runtime_by_pod.get(pod_name, [])
            comparison = next(
                (item for item in comparisons if item.container.lower() == alias),
                comparisons[0] if comparisons else None,
            )
            expected_identity = None
            if comparison and comparison.official_reference:
                expected_identity = comparison.official_reference.digest
            bindings.append(
                {
                    "status": "live",
                    "namespace": namespace,
                    "component": alias,
                    "pod_name": pod_name,
                    "pod_uid": pod_uid,
                    "bound_at": bound_at,
                    "image_identity": (
                        comparison.image_id.rsplit("@", 1)[-1]
                        if comparison and comparison.image_id
                        else None
                    ),
                    "expected_image_identity": expected_identity,
                    "runtime_image_drift": bool(
                        comparison
                        and comparison.status == "official_source_with_runtime_drift"
                    ),
                    "execution_qualified": bool(
                        comparison
                        and comparison.status == "exact"
                    ),
                    "binding_expiry": [
                        "The Pod UID changes or the Pod is deleted.",
                        "The Pod is no longer Ready.",
                        "The source-runtime manifest or live Kubernetes export changes.",
                    ],
                }
            )
    if not bindings:
        raise PipelineError(f"no Ready Pods found in namespace {namespace}")
    return bindings


def _snapshot_id(*paths: Path) -> str:
    digest = hashlib.sha256()
    for path in paths:
        if path.is_file():
            digest.update(path.read_bytes())
    return "snapshot-" + digest.hexdigest()[:16]


def _build_semantic_config(
    args: argparse.Namespace,
    run_dir: Path,
    workspace: Path,
) -> Path:
    raw = yaml.safe_load(args.scan_config.read_text(encoding="utf-8"))
    config_root = args.scan_config.expanduser().resolve().parent
    raw["codebase"]["path"] = str(workspace)
    raw["codebase"]["source_identity"] = (
        f"otel-demo-{args.revision}@{args.expected_commit}"
    )
    raw["codegraph"]["command"] = args.codegraph_command
    raw["kubernetes"]["mode"] = "live"
    raw["kubernetes"]["namespace"] = args.namespace
    raw["kubernetes"]["authoritative_for_namespace"] = False
    raw["kubernetes"]["kubeconfig_path"] = (
        str(args.kubeconfig.expanduser().resolve()) if args.kubeconfig else None
    )
    raw["kubernetes"]["include_configmap_data"] = True
    raw["kubernetes"]["discover_custom_resources"] = True
    raw["kubernetes"]["sources"] = []
    raw["templates_path"] = str((config_root / raw["templates_path"]).resolve())
    raw["prompts_root"] = str((config_root / raw["prompts_root"]).resolve())
    raw["output_dir"] = str(run_dir / "semantic-scan")
    raw["agents"]["model_config_path"] = str((config_root / raw["agents"]["model_config_path"]).resolve())
    raw["agents"]["recursion_limit"] = max(int(raw["agents"].get("recursion_limit", 48)), 320)
    raw["agents"]["agent_timeout_seconds"] = max(int(raw["agents"].get("agent_timeout_seconds", 300)), 1800)
    raw["agents"]["tool_call_limit"] = 100
    raw["agents"]["model_call_limit"] = 120
    raw["planning_mode"] = "model"
    raw["agents"]["context_budget"] = {
        "coordinator_chars": 30000,
        "subagent_chars": 40000,
        "verifier_chars": 30000,
        "max_tool_result_chars": 8000,
    }
    path = run_dir / "configs/semantic-scan.runtime.yaml"
    _write_yaml(path, raw)
    return path


def _build_episode_config(
    args: argparse.Namespace,
    run_dir: Path,
    bindings: list[dict[str, Any]],
    snapshot_id: str,
    capabilities_path: Path,
) -> Path:
    raw = yaml.safe_load(args.episode_config.read_text(encoding="utf-8"))
    config_root = args.episode_config.expanduser().resolve().parent
    raw["application"] = "otel-demo"
    raw["snapshot_id"] = snapshot_id
    raw["kubeconfig_ref"] = args.kubeconfig_ref
    raw["fault_profiles_path"] = str((config_root / raw["fault_profiles_path"]).resolve())
    raw["chaosblade_capabilities_path"] = str(capabilities_path)
    raw["public_prompt_path"] = str((config_root / raw["public_prompt_path"]).resolve())
    raw["output_dir"] = str(run_dir / "episodes")
    raw["runtime_bindings"] = bindings
    raw["mcp_servers"] = ["k8s_ro", "telemetry_ro", "source_ro"]
    raw["mcp_tools"] = [
        "k8s_get_resource",
        "k8s_list_resources",
        "k8s_list_events",
        "telemetry_prom_metric_range",
        "telemetry_jaeger_find_traces",
        "telemetry_loki_logs_range",
        "source_search",
        "source_read_file",
    ]
    raw["codegraph_entrypoints"] = sorted({item["component"] for item in bindings})[:50] or ["otel-demo"]
    raw["fixed_slo"] = ["success_rate >= 0.95", "p95_latency_ms <= 10000"]
    path = run_dir / "configs/episode-generation.runtime.yaml"
    _write_yaml(path, raw)
    return path


def _subprocess_json(
    argv: Sequence[str],
    *,
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    timeout: int,
    stream_stderr: bool = False,
) -> dict[str, Any]:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            text=True,
            stdout=stdout,
            stderr=subprocess.PIPE,
            bufsize=1,
        )

        def pump_stderr() -> None:
            assert process.stderr is not None
            for line in process.stderr:
                stderr.write(line)
                stderr.flush()
                if stream_stderr:
                    sys.stderr.write(line)
                    sys.stderr.flush()

        pump = threading.Thread(target=pump_stderr, daemon=True)
        pump.start()
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            raise PipelineError(
                f"{Path(argv[1]).name if len(argv) > 1 else argv[0]} exceeded {timeout} seconds"
            ) from exc
        finally:
            pump.join(timeout=10)
    raw = stdout_path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {"status": "failed", "error": raw[-2000:]}
    if returncode != 0:
        raise PipelineError(f"{Path(argv[1]).name if len(argv) > 1 else argv[0]} failed with exit {returncode}: {parsed.get('error', '')}")
    return parsed


def dry_run_report(args: argparse.Namespace, run_id: str) -> dict[str, Any]:
    return {
        "schema_version": "resbench-semantic-pipeline.v1",
        "mode": "dry-run",
        "status": "not_executed",
        "run_id": run_id,
        "planned_paths": {
            "system_root": str(args.system_root),
            "source_cache_dir": str(
                args.source_cache_dir or args.system_root / "source-cache"
            ),
            "repository_url": args.repository_url,
            "revision": args.revision,
            "expected_commit": args.expected_commit,
            "run_root": str(args.run_root / run_id),
        },
        "planned_steps": [
            "reuse a verified source cache or clone the allowlisted repository once",
            "verify the immutable commit before CodeGraph indexing",
            "read live Kubernetes resources directly from the API server",
            "compare live image identities with official OTel Demo references",
            "derive current Ready Pod UID runtime bindings",
            "write run-scoped scan and Episode configs",
            "run semantic scan agents with CodeGraph command available to agents",
            "retain non-ChaosBlade candidates and generate Episodes only for supported faults",
        ],
        "mutation_requires": "--execute",
        "qualification_only": args.qualification_only,
    }


def execute(args: argparse.Namespace, run_id: str) -> dict[str, Any]:
    run_dir = _safe_resolve(args.run_root / run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    _json_line({"event": "pipeline_started", "run_id": run_id, "run_dir": str(run_dir), "at": _utc_now()})

    source_cache_dir = _safe_resolve(
        args.source_cache_dir or args.system_root / "source-cache"
    )
    workspace, source_acquisition = _materialize_source_workspace(
        args.repository_url,
        args.revision,
        args.expected_commit,
        run_dir,
        source_cache_dir,
    )
    _json_line(
        {
            "event": "source_materialized",
            "run_id": run_id,
            "acquisition": source_acquisition,
            "cache_dir": str(source_cache_dir),
            "at": _utc_now(),
        }
    )

    live_scanner = LiveKubernetesScanner(
        args.namespace,
        kubeconfig=(str(args.kubeconfig.expanduser().resolve()) if args.kubeconfig else None),
        authoritative_for_namespace=False,
    )
    live_manifest = live_scanner.scan()
    k8s_snapshot = run_dir / "kubernetes/k8s-live-snapshot.json"
    _write_json(
        k8s_snapshot,
        {
            "manifest": live_manifest.model_dump(mode="json"),
            "completeness": live_scanner.completeness,
            "resources": live_scanner.resources,
        },
    )
    _json_line(
        {
            "event": "kubernetes_snapshot_completed",
            "run_id": run_id,
            "resources": live_manifest.resource_count,
            "at": _utc_now(),
        }
    )
    source_runtime = _source_runtime_manifest(
        workspace,
        live_scanner,
        args.crane_command,
        args.expected_commit,
    )
    source_manifest = run_dir / "source/source-runtime-manifest.json"
    _write_json(source_manifest, source_runtime.model_dump(mode="json"))
    _json_line(
        {
            "event": "source_runtime_completed",
            "run_id": run_id,
            "counts": source_runtime.counts,
            "at": _utc_now(),
        }
    )
    bindings = _runtime_bindings(args.namespace, args.kubeconfig, source_runtime)
    bindings_path = run_dir / "runtime/runtime-bindings.json"
    _write_json(bindings_path, {"bindings": bindings})

    capabilities_path = run_dir / "chaosblade/capabilities.yaml"
    capability_manifest = _qualify_chaosblade(
        args.blade_command,
        args.kubeconfig,
        capabilities_path,
    )
    _json_line({"event": "chaosblade_qualified", "run_id": run_id, "at": _utc_now()})

    snapshot_id = _snapshot_id(source_manifest, k8s_snapshot, bindings_path)
    semantic_config = _build_semantic_config(args, run_dir, workspace)
    episode_config = _build_episode_config(
        args,
        run_dir,
        bindings,
        snapshot_id,
        capabilities_path,
    )
    runtime_scan_config = load_semantic_scan_config(semantic_config)
    codegraph_manifest = CodeGraphDriver(
        runtime_scan_config.codebase,
        runtime_scan_config.codegraph,
    ).ensure_index()
    codegraph_manifest_path = run_dir / "codegraph/codegraph-manifest.json"
    _write_json(codegraph_manifest_path, codegraph_manifest.model_dump(mode="json"))
    _json_line(
        {
            "event": "codegraph_index_completed",
            "run_id": run_id,
            "files": codegraph_manifest.file_count,
            "nodes": codegraph_manifest.node_count,
            "edges": codegraph_manifest.edge_count,
            "at": _utc_now(),
        }
    )

    if args.qualification_only:
        report = {
            "schema_version": "resbench-semantic-pipeline.v1",
            "mode": "execute",
            "status": "qualified",
            "run_id": run_id,
            "generated_at": _utc_now(),
            "snapshot_id": snapshot_id,
            "artifacts": {
                "run_dir": str(run_dir),
                "source_manifest": str(source_manifest),
                "kubernetes_live_snapshot": str(k8s_snapshot),
                "codegraph_workspace": str(workspace),
                "codegraph_manifest": str(codegraph_manifest_path),
                "runtime_bindings": str(bindings_path),
                "semantic_config": str(semantic_config),
                "episode_config": str(episode_config),
                "chaosblade_capabilities": str(capabilities_path),
            },
            "source_runtime": {
                "source_status": source_runtime.source.status,
                "acquisition": source_acquisition,
                "counts": source_runtime.counts,
            },
            "kubernetes": {
                "resource_count": live_manifest.resource_count,
                "completeness": live_scanner.completeness,
            },
            "runtime_binding_count": len(bindings),
            "codegraph": {
                "files": codegraph_manifest.file_count,
                "nodes": codegraph_manifest.node_count,
                "edges": codegraph_manifest.edge_count,
                "languages": codegraph_manifest.languages,
            },
            "chaosblade": {
                "verified_fault_types": [
                    item["fault_type"]
                    for item in capability_manifest["capabilities"]
                    if item["status"] == "verified"
                ]
            },
            "model_called": False,
            "episodes_generated": False,
        }
        _write_json(run_dir / "pipeline-report.json", report)
        _json_line(
            {
                "event": "pipeline_qualification_completed",
                "run_id": run_id,
                "at": _utc_now(),
            }
        )
        return report

    scan_stdout = run_dir / "logs/semantic-scan.stdout.json"
    scan_stderr = run_dir / "logs/semantic-scan.events.jsonl"
    _json_line({"event": "semantic_scan_started", "run_id": run_id, "at": _utc_now()})
    scan_result = _subprocess_json(
        [sys.executable, "scripts/run_semantic_scan.py", "--config", str(semantic_config), "--run-id", run_id],
        cwd=REPO_ROOT,
        stdout_path=scan_stdout,
        stderr_path=scan_stderr,
        timeout=7200,
        stream_stderr=True,
    )
    _json_line({"event": "semantic_scan_completed", "run_id": run_id, "at": _utc_now()})
    scan_report = run_dir / "semantic-scan" / run_id / "semantic-scan-report.json"

    episode_stdout = run_dir / "logs/episode-generation.stdout.json"
    episode_stderr = run_dir / "logs/episode-generation.stderr.txt"
    _json_line({"event": "episode_generation_started", "run_id": run_id, "at": _utc_now()})
    episode_result = _subprocess_json(
        [sys.executable, "scripts/generate_episodes.py", "--config", str(episode_config), "--scan-report", str(scan_report)],
        cwd=REPO_ROOT,
        stdout_path=episode_stdout,
        stderr_path=episode_stderr,
        timeout=1200,
    )
    _json_line({"event": "episode_generation_completed", "run_id": run_id, "at": _utc_now()})

    report = {
        "schema_version": "resbench-semantic-pipeline.v1",
        "mode": "execute",
        "status": "completed",
        "run_id": run_id,
        "generated_at": _utc_now(),
        "snapshot_id": snapshot_id,
        "artifacts": {
            "run_dir": str(run_dir),
            "source_manifest": str(source_manifest),
            "kubernetes_live_snapshot": str(k8s_snapshot),
            "codegraph_workspace": str(workspace),
            "codegraph_manifest": str(codegraph_manifest_path),
            "runtime_bindings": str(bindings_path),
            "semantic_config": str(semantic_config),
            "episode_config": str(episode_config),
            "chaosblade_capabilities": str(capabilities_path),
            "scan_report": str(scan_report),
            "episode_report": str(run_dir / "episodes/episode-generation-report.json"),
        },
        "source_runtime": {
            "source_status": source_runtime.source.status,
            "acquisition": source_acquisition,
            "counts": source_runtime.counts,
        },
        "kubernetes": {
            "resource_count": live_manifest.resource_count,
            "completeness": live_scanner.completeness,
        },
        "runtime_binding_count": len(bindings),
        "codegraph": {
            "files": codegraph_manifest.file_count,
            "nodes": codegraph_manifest.node_count,
            "edges": codegraph_manifest.edge_count,
            "languages": codegraph_manifest.languages,
        },
        "chaosblade": {
            "verified_fault_types": [
                item["fault_type"]
                for item in capability_manifest["capabilities"]
                if item["status"] == "verified"
            ]
        },
        "scan": scan_result,
        "episodes": episode_result,
    }
    _write_json(run_dir / "pipeline-report.json", report)
    _json_line({"event": "pipeline_completed", "run_id": run_id, "episode_count": episode_result.get("generated_count", 0), "at": _utc_now()})
    return report


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_id = args.run_id or _run_id()
    if not RUN_ID_RE.fullmatch(run_id):
        print(json.dumps({"status": "failed", "error": "--run-id must match semantic-*"}, ensure_ascii=False), flush=True)
        return 2
    try:
        report = execute(args, run_id) if args.execute else dry_run_report(args, run_id)
    except Exception as exc:  # noqa: BLE001 - CLI boundary redacts env/secrets.
        report = {"schema_version": "resbench-semantic-pipeline.v1", "status": "failed", "run_id": run_id, "error_type": type(exc).__name__, "error": str(exc)[:2000]}
        target = args.run_root / run_id / "pipeline-report.json"
        if args.execute and target.parent.exists():
            _write_json(target, report)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
