#!/usr/bin/env python3
"""Deploy, activate, standby, or delete one benchmark application.

The command is dry-run by default. Cluster mutations require ``--execute`` and
an explicit kubeconfig. Runtime credentials and environment-specific endpoints
are rendered from environment variables or a mode-600 env file and are never
written to the repository or structured report.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Protocol

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
MARKER_NAMESPACE = "otel-demo"
MARKER_NAME = "resbench-active-system"
STANDBY_ANNOTATION = "resiliencebenchmark.io/standby-replicas"
SUPPORTED_APPLICATIONS = ("train-ticket", "sock-shop", "otel-demo")
LIVE_NAMESPACES = {name: name for name in SUPPORTED_APPLICATIONS}
PROTECTED_NAMESPACES = frozenset({"observability", "ischaos", "resiliencebenchmark-system", "resilience-benchmark-system"})
PLACEHOLDER_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
SAFE_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")


class DeployError(RuntimeError):
    """Expected deployment error safe to print without runtime values."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(self, argv: list[str], *, stdin: str | None = None, timeout: int = 300) -> CommandResult: ...


class SubprocessRunner:
    def run(self, argv: list[str], *, stdin: str | None = None, timeout: int = 300) -> CommandResult:
        try:
            completed = subprocess.run(
                argv,
                input=stdin,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise DeployError("application command timed out") from exc
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def run_checked(
    runner: CommandRunner,
    argv: list[str],
    *,
    stdin: str | None = None,
    timeout: int = 300,
) -> str:
    result = runner.run(argv, stdin=stdin, timeout=timeout)
    if result.returncode != 0:
        command = " ".join(_redacted_argv(argv))
        raise DeployError(f"command failed: {command}: {_safe_error(result.stderr or result.stdout)}")
    return result.stdout


def _redacted_argv(argv: list[str]) -> list[str]:
    redacted: list[str] = []
    for item in argv:
        if "password" in item.lower() or "token" in item.lower() or "secret" in item.lower():
            redacted.append("<redacted-argument>")
        else:
            redacted.append(item)
    return redacted


def _safe_error(value: str) -> str:
    text = " ".join(value.strip().split())
    text = re.sub(r"(?i)(password|token|secret|api[_-]?key)\s*[:=]\s*\S+", r"\1=<redacted>", text)
    return text[:1000] or "no diagnostic output"


def validate_name(value: str, field: str) -> str:
    if not SAFE_NAME_RE.fullmatch(value):
        raise DeployError(f"{field} must be a concrete Kubernetes DNS name")
    return value


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise DeployError(f"cannot parse deployment config: {path.relative_to(REPO_ROOT)}") from exc
    if not isinstance(data, dict):
        raise DeployError(f"deployment config must be a mapping: {path.relative_to(REPO_ROOT)}")
    return data


def application_config(application: str) -> dict[str, Any]:
    return load_yaml(REPO_ROOT / "environment" / "applications" / f"{application}.yaml")


def deployment_bundle(application: str) -> tuple[Path, dict[str, Any]] | None:
    path = REPO_ROOT / "environment" / "kubernetes" / application / "deployment.yaml"
    if not path.is_file():
        return None
    return path, load_yaml(path)


def runtime_environment(env: Mapping[str, str], env_file: Path | None) -> dict[str, str]:
    values = dict(env)
    if env_file is None:
        return values
    path = env_file.expanduser().resolve()
    if not path.is_file():
        raise DeployError("runtime env file must be an existing regular file")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise DeployError("runtime env file must not be accessible by group or other users")
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise DeployError(f"runtime env file line {number} must be KEY=VALUE")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise DeployError(f"runtime env file line {number} has an invalid key")
        if any(char in value for char in "\r\n\x00"):
            raise DeployError(f"runtime env file line {number} contains a control character")
        values[key] = value
    return values


def render_text(text: str, values: Mapping[str, str]) -> str:
    missing: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        value = values.get(key)
        if value is None or value == "":
            missing.add(key)
            return match.group(0)
        if any(char in value for char in "\r\n\x00"):
            raise DeployError(f"runtime value {key} contains a control character")
        return value

    rendered = PLACEHOLDER_RE.sub(replace, text)
    if missing:
        raise DeployError(f"missing runtime value(s): {', '.join(sorted(missing))}")
    return rendered


def _rewrite_namespace_string(value: str, source: str, target: str) -> str:
    if source == target:
        return value
    return value.replace(f".{source}.svc", f".{target}.svc").replace(
        f"/namespaces/{source}/", f"/namespaces/{target}/"
    )


def rewrite_tree(value: Any, source_namespace: str, target_namespace: str) -> Any:
    if isinstance(value, dict):
        return {key: rewrite_tree(item, source_namespace, target_namespace) for key, item in value.items()}
    if isinstance(value, list):
        return [rewrite_tree(item, source_namespace, target_namespace) for item in value]
    if isinstance(value, str):
        return _rewrite_namespace_string(value, source_namespace, target_namespace)
    return value


def render_manifest(
    path: Path,
    values: Mapping[str, str],
    *,
    source_namespace: str,
    target_namespace: str,
) -> dict[str, Any]:
    rendered = render_text(path.read_text(encoding="utf-8"), values)
    document = yaml.safe_load(rendered)
    if not isinstance(document, dict):
        raise DeployError("rendered manifest must be a Kubernetes object")
    if document.get("kind") == "List":
        items = document.get("items")
    else:
        items = [document]
        document = {"apiVersion": "v1", "kind": "List", "items": items}
    if not isinstance(items, list) or not items:
        raise DeployError("rendered manifest list is empty")
    for item in items:
        if not isinstance(item, dict) or item.get("kind") == "Secret":
            raise DeployError("application manifests must not contain Secret objects")
        metadata = item.setdefault("metadata", {})
        metadata["namespace"] = target_namespace
        if item.get("kind") == "Service" and source_namespace != target_namespace:
            ports = item.get("spec", {}).get("ports", []) or []
            for port in ports:
                port.pop("nodePort", None)
            if item.get("spec", {}).get("type") == "NodePort":
                item["spec"]["type"] = "ClusterIP"
                item["spec"].pop("externalTrafficPolicy", None)
                item["spec"].pop("allocateLoadBalancerNodePorts", None)
    return rewrite_tree(document, source_namespace, target_namespace)


def render_values(path: Path, values: Mapping[str, str], source_namespace: str, target_namespace: str) -> dict[str, Any]:
    rendered = render_text(path.read_text(encoding="utf-8"), values)
    document = yaml.safe_load(rendered)
    if not isinstance(document, dict):
        raise DeployError("rendered Helm values must be a mapping")
    document = rewrite_tree(document, source_namespace, target_namespace)
    if source_namespace != target_namespace:
        service = document.get("components", {}).get("frontend-proxy", {}).get("service")
        if isinstance(service, dict) and service.get("type") == "NodePort":
            service["type"] = "ClusterIP"
            service.pop("nodePort", None)
        collector = document.get("opentelemetry-collector")
        if isinstance(collector, dict):
            cluster_role = collector.setdefault("clusterRole", {})
            scoped_name = f"otel-collector-{target_namespace}"
            cluster_role["name"] = scoped_name
            cluster_role.setdefault("clusterRoleBinding", {})["name"] = scoped_name
    return document


def kube_base(kubeconfig: Path) -> list[str]:
    return ["kubectl", "--kubeconfig", str(kubeconfig), "--request-timeout=30s"]


def namespace_exists(runner: CommandRunner, kubeconfig: Path, namespace: str) -> bool:
    result = runner.run(kube_base(kubeconfig) + ["get", "namespace", namespace, "-o", "name"], timeout=30)
    return result.returncode == 0


def namespace_inventory(runner: CommandRunner, kubeconfig: Path, namespace: str) -> list[dict[str, str]]:
    if not namespace_exists(runner, kubeconfig, namespace):
        return []
    output = run_checked(
        runner,
        kube_base(kubeconfig)
        + ["get", "deployments,statefulsets,services,persistentvolumeclaims,configmaps,secrets,jobs", "-n", namespace, "-o", "json"],
        timeout=60,
    )
    payload = json.loads(output)
    return sorted(
        ({"kind": item.get("kind", "Unknown"), "name": item.get("metadata", {}).get("name", "unknown")} for item in payload.get("items", [])),
        key=lambda item: (item["kind"], item["name"]),
    )


def protected_resources(application: str) -> list[str]:
    config = application_config(application)
    return list(config.get("spec", {}).get("resetContract", {}).get("protectedResources", []))


def assert_delete_boundary(application: str, namespace: str) -> None:
    if namespace in PROTECTED_NAMESPACES:
        raise DeployError(f"refusing to delete protected namespace {namespace}")
    live = LIVE_NAMESPACES[application]
    if namespace != live and not namespace.startswith(("rb-", "resbench-", "tmp-")):
        raise DeployError("non-default delete targets must use an rb-, resbench-, or tmp- namespace")


def assert_inventory_not_protected(
    application: str,
    namespace: str,
    inventory: list[dict[str, str]],
    *,
    temporary_owned: bool = False,
) -> None:
    protected = set(protected_resources(application))
    if "persistent-volume-claims" in protected:
        pvcs = sorted(item["name"] for item in inventory if item["kind"] == "PersistentVolumeClaim")
        if pvcs and not (namespace != LIVE_NAMESPACES[application] and temporary_owned):
            raise DeployError(
                "refusing namespace deletion because resetContract protects persistent-volume-claims: "
                + ", ".join(pvcs)
            )


def namespace_is_temporary_owned(runner: CommandRunner, kubeconfig: Path, namespace: str) -> bool:
    result = runner.run(kube_base(kubeconfig) + ["get", "namespace", namespace, "-o", "json"], timeout=30)
    if result.returncode != 0:
        return False
    labels = json.loads(result.stdout).get("metadata", {}).get("labels") or {}
    return labels.get("resiliencebenchmark.io/temporary") == "true"


def create_namespace(runner: CommandRunner, kubeconfig: Path, application: str, namespace: str) -> None:
    labels = {"resiliencebenchmark.io/application": application}
    if namespace != LIVE_NAMESPACES[application]:
        labels["resiliencebenchmark.io/temporary"] = "true"
    manifest = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": namespace, "labels": labels},
    }
    run_checked(runner, kube_base(kubeconfig) + ["apply", "-f", "-"], stdin=yaml.safe_dump(manifest, sort_keys=False))


def required_secret_contract(application: str) -> list[dict[str, Any]]:
    if application != "train-ticket":
        return []
    data = load_yaml(REPO_ROOT / "environment/kubernetes/train-ticket/required-secrets.yaml")
    return list(data.get("spec", {}).get("secrets", []))


def copy_runtime_secrets(
    runner: CommandRunner,
    kubeconfig: Path,
    source_namespace: str,
    target_namespace: str,
    contract: list[dict[str, Any]],
) -> None:
    if source_namespace == target_namespace:
        return
    items: list[dict[str, Any]] = []
    for entry in contract:
        name = str(entry["name"])
        raw = run_checked(runner, kube_base(kubeconfig) + ["get", "secret", name, "-n", source_namespace, "-o", "json"])
        secret = json.loads(raw)
        metadata = secret.get("metadata", {})
        secret["metadata"] = {"name": metadata.get("name"), "namespace": target_namespace}
        for key in ("status",):
            secret.pop(key, None)
        items.append(secret)
    payload = yaml.safe_dump({"apiVersion": "v1", "kind": "List", "items": items}, sort_keys=False)
    run_checked(runner, kube_base(kubeconfig) + ["apply", "-f", "-"], stdin=payload, timeout=120)


def verify_required_secrets(
    runner: CommandRunner,
    kubeconfig: Path,
    namespace: str,
    contract: list[dict[str, Any]],
) -> None:
    for entry in contract:
        name = str(entry["name"])
        output = run_checked(runner, kube_base(kubeconfig) + ["get", "secret", name, "-n", namespace, "-o", "json"])
        secret = json.loads(output)
        keys = set((secret.get("data") or {}).keys())
        required = set(entry.get("requiredKeys", []))
        if not required.issubset(keys):
            raise DeployError(f"runtime Secret {name} is missing required keys")


def helm_upgrade(
    runner: CommandRunner,
    *,
    release: str,
    chart: str,
    namespace: str,
    values: dict[str, Any],
    timeout_seconds: int,
    version: str | None = None,
    force_conflicts: bool = False,
    wait: bool = True,
) -> None:
    argv = ["helm", "upgrade", "--install", release, chart, "--namespace", namespace, "--create-namespace", "--values", "-", "--timeout", f"{timeout_seconds}s"]
    if wait:
        argv.append("--wait")
    if version:
        argv.extend(["--version", version])
    if force_conflicts:
        argv.extend(["--server-side=true", "--force-conflicts"])
    run_checked(runner, argv, stdin=yaml.safe_dump(values, sort_keys=False), timeout=timeout_seconds + 60)


def apply_post_install_patch(
    runner: CommandRunner,
    kubeconfig: Path,
    namespace: str,
    patch: dict[str, Any],
    timeout_seconds: int,
) -> None:
    containers = copy.deepcopy(patch.get("containers", []))
    init_containers = copy.deepcopy(patch.get("initContainers", []))
    pod_spec: dict[str, Any] = {}
    if containers:
        pod_spec["containers"] = containers
    if init_containers:
        pod_spec["initContainers"] = init_containers
    payload = {"spec": {"template": {"spec": pod_spec}}}
    kind = str(patch["kind"]).lower()
    name = str(patch["name"])
    raw = run_checked(
        runner,
        kube_base(kubeconfig) + ["get", f"{kind}/{name}", "-n", namespace, "-o", "json"],
        timeout=30,
    )
    current = json.loads(raw)
    current_pod_spec = current.get("spec", {}).get("template", {}).get("spec", {})

    def differs(group: str, desired: list[dict[str, Any]]) -> bool:
        current_by_name = {item.get("name"): item for item in current_pod_spec.get(group, []) or []}
        for item in desired:
            active = current_by_name.get(item.get("name"), {})
            for field in ("command", "args"):
                if field in item and active.get(field) != item.get(field):
                    return True
        return False

    patch_needed = differs("containers", containers) or differs("initContainers", init_containers)
    if patch_needed:
        run_checked(
            runner,
            kube_base(kubeconfig)
            + ["patch", f"{kind}/{name}", "-n", namespace, "--type=strategic", "-p", json.dumps(payload, separators=(",", ":"))],
            timeout=60,
        )
        if kind == "statefulset":
            selector_labels = current.get("spec", {}).get("selector", {}).get("matchLabels") or {}
            if not selector_labels:
                raise DeployError(f"statefulset/{name} has no exact selector for post-install Pod replacement")
            selector = ",".join(f"{key}={value}" for key, value in sorted(selector_labels.items()))
            run_checked(
                runner,
                kube_base(kubeconfig) + ["delete", "pods", "-n", namespace, "-l", selector, "--wait=false"],
                timeout=60,
            )
    resource = "deployment" if kind == "deployment" else "statefulset"
    run_checked(
        runner,
        kube_base(kubeconfig) + ["rollout", "status", f"{resource}/{name}", "-n", namespace, f"--timeout={timeout_seconds}s"],
        timeout=timeout_seconds + 30,
    )


def apply_train_ticket(
    runner: CommandRunner,
    kubeconfig: Path,
    namespace: str,
    runtime: Mapping[str, str],
    secret_source_namespace: str | None,
    timeout_seconds: int,
) -> None:
    base = REPO_ROOT / "environment/kubernetes/train-ticket"
    bundle = load_yaml(base / "deployment.yaml")["spec"]
    contract = required_secret_contract("train-ticket")
    external_contract = [item for item in contract if not str(item.get("provisionedBy", "")).startswith("helm:")]
    if secret_source_namespace:
        copy_runtime_secrets(runner, kubeconfig, secret_source_namespace, namespace, external_contract)
    verify_required_secrets(runner, kubeconfig, namespace, external_contract)
    releases = {item["release"]: item for item in bundle["helmReleases"]}
    post_install_patches = bundle.get("postInstallPatches", {})
    for release in ("nacosdb", "tsdb", "rabbitmq", "nacos"):
        item = releases[release]
        chart_path = base / item["chart"]["package"]
        digest = hashlib.sha256(chart_path.read_bytes()).hexdigest()
        if digest != item["chart"]["sha256"]:
            raise DeployError(f"vendored chart digest mismatch for {release}")
        values = render_values(base / item["values"], runtime, "train-ticket", namespace)
        helm_upgrade(
            runner,
            release=release,
            chart=str(chart_path),
            namespace=namespace,
            values=values,
            timeout_seconds=timeout_seconds,
            wait=False,
        )
        patch = post_install_patches.get(release)
        if patch:
            apply_post_install_patch(runner, kubeconfig, namespace, patch, timeout_seconds)
    verify_required_secrets(runner, kubeconfig, namespace, contract)
    manifest = render_manifest(base / bundle["staticManifest"], runtime, source_namespace="train-ticket", target_namespace=namespace)
    run_checked(runner, kube_base(kubeconfig) + ["apply", "-f", "-"], stdin=yaml.safe_dump(manifest, sort_keys=False), timeout=180)


def apply_otel_demo(
    runner: CommandRunner,
    kubeconfig: Path,
    namespace: str,
    runtime: Mapping[str, str],
    timeout_seconds: int,
) -> None:
    base = REPO_ROOT / "environment/kubernetes/otel-demo"
    bundle = load_yaml(base / "deployment.yaml")["spec"]
    item = bundle["helmReleases"][0]
    chart = item["chart"]
    chart_file_raw = str(runtime.get("OTEL_DEMO_CHART_FILE") or "")
    if chart_file_raw:
        chart_file = Path(chart_file_raw).expanduser().resolve()
        if (
            not chart_file.is_absolute()
            or not chart_file.is_file()
            or chart_file.is_symlink()
            or chart_file.name != f"opentelemetry-demo-{chart['version']}.tgz"
        ):
            raise DeployError("OTEL_DEMO_CHART_FILE must reference the pinned chart archive")
        chart_reference = str(chart_file)
    else:
        run_checked(runner, ["helm", "repo", "add", chart["repositoryName"], chart["repositoryUrl"], "--force-update"], timeout=120)
        chart_reference = f"{chart['repositoryName']}/{chart['name']}"
    values = render_values(base / item["values"], runtime, "otel-demo", namespace)
    helm_upgrade(
        runner,
        release=item["release"],
        chart=chart_reference,
        namespace=namespace,
        values=values,
        timeout_seconds=timeout_seconds,
        version=str(chart["version"]),
        force_conflicts=namespace == "otel-demo",
    )
    for name in intentionally_standby("otel-demo"):
        run_checked(
            runner,
            kube_base(kubeconfig)
            + ["annotate", f"deployment/{name}", "-n", namespace, f"{STANDBY_ANNOTATION}=1", "--overwrite"],
        )
        run_checked(
            runner,
            kube_base(kubeconfig) + ["scale", f"deployment/{name}", "-n", namespace, "--replicas=0"],
        )
    manifest = render_manifest(base / bundle["supplementalManifest"], runtime, source_namespace="otel-demo", target_namespace=namespace)
    run_checked(runner, kube_base(kubeconfig) + ["apply", "-f", "-"], stdin=yaml.safe_dump(manifest, sort_keys=False), timeout=120)


def apply_sock_shop(
    runner: CommandRunner,
    kubeconfig: Path,
    namespace: str,
    runtime: Mapping[str, str],
) -> None:
    from scripts import render_sock_shop

    base = REPO_ROOT / "environment/kubernetes/sock-shop"
    config_path = base / "render-config.yaml"
    image_map_path = base / "harbor-image-map.json"
    rendered_map = render_text(image_map_path.read_text(encoding="utf-8"), runtime)
    with tempfile.TemporaryDirectory(prefix="resbench-sock-shop-map-") as temp_dir:
        runtime_map = Path(temp_dir) / "image-map.json"
        runtime_map.write_text(rendered_map, encoding="utf-8")
        rendered = render_sock_shop.render_manifest(config_path, image_map_path=runtime_map)
    docs = [doc for doc in yaml.safe_load_all(rendered) if isinstance(doc, dict)]
    docs = [doc for doc in docs if doc.get("kind") != "Namespace"]
    document = {"apiVersion": "v1", "kind": "List", "items": docs}
    document = rewrite_tree(document, "sock-shop", namespace)
    for item in document["items"]:
        item.setdefault("metadata", {})["namespace"] = namespace
        if item.get("kind") == "Service" and namespace != "sock-shop":
            for port in item.get("spec", {}).get("ports", []) or []:
                port.pop("nodePort", None)
            if item.get("spec", {}).get("type") == "NodePort":
                item["spec"]["type"] = "ClusterIP"
    run_checked(runner, kube_base(kubeconfig) + ["apply", "-f", "-"], stdin=yaml.safe_dump(document, sort_keys=False), timeout=180)


def controllers(runner: CommandRunner, kubeconfig: Path, namespace: str) -> list[dict[str, Any]]:
    output = run_checked(runner, kube_base(kubeconfig) + ["get", "deployments,statefulsets", "-n", namespace, "-o", "json"])
    return list(json.loads(output).get("items", []))


def intentionally_standby(application: str) -> set[str]:
    bundle = deployment_bundle(application)
    if not bundle:
        return set()
    return set(bundle[1].get("spec", {}).get("readiness", {}).get("intentionallyStandby", []))


def standby_application(runner: CommandRunner, kubeconfig: Path, namespace: str) -> dict[str, int]:
    changed = 0
    preserved = 0
    for item in controllers(runner, kubeconfig, namespace):
        kind = item["kind"].lower()
        name = item["metadata"]["name"]
        replicas = int(item.get("spec", {}).get("replicas") or 0)
        annotations = item.get("metadata", {}).get("annotations") or {}
        if replicas > 0:
            run_checked(runner, kube_base(kubeconfig) + ["annotate", f"{kind}/{name}", "-n", namespace, f"{STANDBY_ANNOTATION}={replicas}", "--overwrite"])
            run_checked(runner, kube_base(kubeconfig) + ["scale", f"{kind}/{name}", "-n", namespace, "--replicas=0"])
            changed += 1
        elif STANDBY_ANNOTATION in annotations:
            preserved += 1
    return {"scaledToZero": changed, "alreadyStandby": preserved}


def activate_application(
    runner: CommandRunner,
    kubeconfig: Path,
    application: str,
    namespace: str,
    timeout_seconds: int,
) -> dict[str, int]:
    restored = 0
    kept_zero = 0
    intentional = intentionally_standby(application)
    items = controllers(runner, kubeconfig, namespace)
    for item in items:
        kind = item["kind"].lower()
        name = item["metadata"]["name"]
        current = int(item.get("spec", {}).get("replicas") or 0)
        if name in intentional:
            if current != 0:
                run_checked(runner, kube_base(kubeconfig) + ["scale", f"{kind}/{name}", "-n", namespace, "--replicas=0"])
            kept_zero += 1
            continue
        annotations = item.get("metadata", {}).get("annotations") or {}
        raw = annotations.get(STANDBY_ANNOTATION)
        if raw is None:
            if current <= 0:
                raise DeployError(f"controller {kind}/{name} has no standby replica annotation")
            continue
        replicas = int(raw)
        if replicas <= 0:
            raise DeployError(f"controller {kind}/{name} has an invalid standby replica annotation")
        run_checked(runner, kube_base(kubeconfig) + ["scale", f"{kind}/{name}", "-n", namespace, f"--replicas={replicas}"])
        restored += 1
    wait_ready(runner, kubeconfig, namespace, timeout_seconds, intentional)
    return {"restored": restored, "intentionallyZero": kept_zero}


def wait_ready(
    runner: CommandRunner,
    kubeconfig: Path,
    namespace: str,
    timeout_seconds: int,
    intentional_zero: set[str] | None = None,
) -> None:
    intentional_zero = intentional_zero or set()
    for item in controllers(runner, kubeconfig, namespace):
        name = item["metadata"]["name"]
        if name in intentional_zero or int(item.get("spec", {}).get("replicas") or 0) == 0:
            continue
        resource = "deployment" if item["kind"] == "Deployment" else "statefulset"
        run_checked(
            runner,
            kube_base(kubeconfig) + ["rollout", "status", f"{resource}/{name}", "-n", namespace, f"--timeout={timeout_seconds}s"],
            timeout=timeout_seconds + 30,
        )


def update_active_marker(runner: CommandRunner, kubeconfig: Path, active: str) -> None:
    if not namespace_exists(runner, kubeconfig, MARKER_NAMESPACE):
        return
    marker = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": MARKER_NAME,
            "namespace": MARKER_NAMESPACE,
            "labels": {"app.kubernetes.io/managed-by": "resiliencebenchmark"},
        },
        "data": {"active-system": active, "inactive-strategy": "reversible-scale-to-zero"},
    }
    run_checked(runner, kube_base(kubeconfig) + ["apply", "-f", "-"], stdin=yaml.safe_dump(marker, sort_keys=False))


def current_active_marker(runner: CommandRunner, kubeconfig: Path) -> str | None:
    result = runner.run(kube_base(kubeconfig) + ["get", "configmap", MARKER_NAME, "-n", MARKER_NAMESPACE, "-o", "json"], timeout=30)
    if result.returncode != 0:
        return None
    return str(json.loads(result.stdout).get("data", {}).get("active-system") or "") or None


def plan_report(application: str, mode: str, namespace: str, fresh: bool) -> dict[str, Any]:
    actions: list[str] = []
    if mode == "apply":
        if fresh:
            actions.extend([f"inventory namespace/{namespace}", f"delete namespace/{namespace}", f"create namespace/{namespace}"])
        else:
            actions.append(f"ensure namespace/{namespace}")
        if application == "train-ticket":
            actions.extend(["verify or copy 29 external runtime Secrets; Helm creates 3 release Secrets", "helm upgrade --install nacosdb, tsdb, rabbitmq, nacos", "kubectl apply static Train-Ticket manifests"])
        elif application == "otel-demo":
            actions.extend(["helm upgrade --install otel-demo 0.40.5", "kubectl apply workload result PVC"])
        else:
            actions.append("render and kubectl apply pinned Sock Shop manifest")
    elif mode == "standby":
        actions.extend([f"annotate current replicas in namespace/{namespace}", "scale application controllers to 0", "update active-system marker if needed"])
    elif mode == "activate":
        actions.extend(["standby other active benchmark application", f"restore replicas in namespace/{namespace}", "wait for rollout readiness", f"set active-system marker to {application}"])
    else:
        actions.extend([f"inventory namespace/{namespace}", f"delete namespace/{namespace}"])
    return {
        "schemaVersion": "resiliencebenchmark.application_deploy/v1",
        "phase": "plan",
        "application": application,
        "mode": mode,
        "namespace": namespace,
        "fresh": fresh,
        "protectedResources": protected_resources(application),
        "actions": actions,
    }


def execute(args: argparse.Namespace, runner: CommandRunner | None = None, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    runner = runner or SubprocessRunner()
    application = args.application
    namespace = validate_name(args.namespace or LIVE_NAMESPACES[application], "namespace")
    report = plan_report(application, args.mode, namespace, args.fresh)
    report["modeExecution"] = "execute" if args.execute else "dry-run"
    if not args.execute:
        return report
    kubeconfig = args.kubeconfig.expanduser().resolve()
    if not kubeconfig.is_file():
        raise DeployError("--execute requires an explicit existing kubeconfig")
    runtime = runtime_environment(os.environ if env is None else env, args.runtime_env_file)
    if args.mode in {"delete"} or (args.mode == "apply" and args.fresh):
        assert_delete_boundary(application, namespace)
        report["deletionInventory"] = namespace_inventory(runner, kubeconfig, namespace)
        temporary_owned = namespace_is_temporary_owned(runner, kubeconfig, namespace)
        assert_inventory_not_protected(
            application,
            namespace,
            report["deletionInventory"],
            temporary_owned=temporary_owned,
        )
    if args.mode == "delete":
        if namespace_exists(runner, kubeconfig, namespace):
            run_checked(runner, kube_base(kubeconfig) + ["delete", "namespace", namespace, "--wait=true", f"--timeout={args.timeout}s"], timeout=args.timeout + 30)
        report["result"] = "deleted"
        return report
    if args.mode == "apply":
        if args.fresh and namespace_exists(runner, kubeconfig, namespace):
            run_checked(runner, kube_base(kubeconfig) + ["delete", "namespace", namespace, "--wait=true", f"--timeout={args.timeout}s"], timeout=args.timeout + 30)
        if not namespace_exists(runner, kubeconfig, namespace):
            create_namespace(runner, kubeconfig, application, namespace)
        if application == "train-ticket":
            apply_train_ticket(runner, kubeconfig, namespace, runtime, args.secret_source_namespace, args.timeout)
        elif application == "otel-demo":
            apply_otel_demo(runner, kubeconfig, namespace, runtime, args.timeout)
        else:
            apply_sock_shop(runner, kubeconfig, namespace, runtime)
        wait_ready(runner, kubeconfig, namespace, args.timeout, intentionally_standby(application))
        report["result"] = "applied-ready"
        return report
    if args.mode == "standby":
        report["standby"] = standby_application(runner, kubeconfig, namespace)
        if current_active_marker(runner, kubeconfig) == application:
            update_active_marker(runner, kubeconfig, "none")
        report["result"] = "standby"
        return report
    if namespace == LIVE_NAMESPACES[application]:
        for other in SUPPORTED_APPLICATIONS:
            if other == application:
                continue
            other_ns = LIVE_NAMESPACES[other]
            if namespace_exists(runner, kubeconfig, other_ns):
                standby_application(runner, kubeconfig, other_ns)
    report["activate"] = activate_application(runner, kubeconfig, application, namespace, args.timeout)
    update_active_marker(runner, kubeconfig, application)
    report["result"] = "active-ready"
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deploy or switch one resilience benchmark application")
    parser.add_argument("--application", choices=SUPPORTED_APPLICATIONS, required=True)
    parser.add_argument("--mode", choices=("apply", "activate", "standby", "delete"), required=True)
    parser.add_argument("--namespace", help="Override the application's live namespace")
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument("--runtime-env-file", type=Path)
    parser.add_argument("--secret-source-namespace", help="Explicit source namespace for transient Secret copying")
    parser.add_argument("--fresh", action="store_true", help="For apply only: delete and recreate the target namespace")
    parser.add_argument("--execute", action="store_true", help="Perform cluster mutations; default is dry-run")
    parser.add_argument("--timeout", type=int, default=900)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.fresh and args.mode != "apply":
        print("deploy_application: --fresh is valid only with --mode apply", file=sys.stderr)
        return 2
    if args.execute and args.kubeconfig is None:
        print("deploy_application: --execute requires --kubeconfig", file=sys.stderr)
        return 2
    if args.timeout < 30 or args.timeout > 3600:
        print("deploy_application: --timeout must be between 30 and 3600 seconds", file=sys.stderr)
        return 2
    plan = plan_report(args.application, args.mode, args.namespace or LIVE_NAMESPACES[args.application], args.fresh)
    if args.execute:
        print(json.dumps(plan, ensure_ascii=False, indent=2), flush=True)
    try:
        report = execute(args)
    except DeployError as exc:
        print(json.dumps({"schemaVersion": "resiliencebenchmark.application_deploy/v1", "phase": "failed", "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
