#!/usr/bin/env python3
"""Preparation-time validation for the resilience benchmark repository.

This script is intentionally a thin entrypoint. It validates repository
contracts, checks for unsafe material in agent-visible files, and can run a
read-only cluster qualification pass. It never injects faults, applies
manifests, deletes resources, or reads Kubernetes Secrets.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml


class TaggedSafeLoader(yaml.SafeLoader):
    """Parse custom YAML tags as inert data without executing constructors."""


def _construct_inert_tag(loader: TaggedSafeLoader, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    raise TypeError(f"unsupported YAML node: {type(node).__name__}")


TaggedSafeLoader.add_constructor(None, _construct_inert_tag)


REPO_REQUIRED_FILES = (
    "README.md",
    "tasks/README.md",
    "environment/README.md",
    "harness/README.md",
    "controller/README.md",
    "evaluator/README.md",
    "scripts/README.md",
)

CONFIG_SUFFIXES = {".yaml", ".yml", ".json"}
SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    "artifacts",
    "runs",
    "tmp",
    "ground-truth-private",
}
POLICY_SCAN_SKIP_DIRS = {"tests"}
AGENT_VISIBLE_DIRS = {
    "tasks",
    "environment",
    "harness",
    "controller",
    "resilience_agent",
    "scripts",
}
SAFE_PRIVATE_CONTRACT_FILES = {
    "tasks/schemas/ground-truth.schema.json",
}
PRIVATE_NETS = tuple(ipaddress.ip_network(cidr) for cidr in (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
    "255.255.255.255/32",
))

SECRET_TOKEN_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{20,}")
SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password)\b[ \t]*[:=][ \t]*['\"]?([^'\"\s#]+)"
)
PRIVATE_KEY_PATTERN = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")
PUBLIC_IPV4_PATTERN = re.compile(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b")
USER_ABS_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9_])/(?:Users|home)/[A-Za-z0-9._-]+/")
GROUND_TRUTH_KEY_PATTERN = re.compile(r"(?i)\bground[_-]?truth\b|\bhidden[_-]?truth\b")


@dataclass
class Issue:
    severity: str
    location: str
    message: str

    def line(self) -> str:
        return f"{self.severity}: {self.location}: {self.message}"


def repo_root_from_arg(path: str | None) -> Path:
    root = Path(path or ".").resolve()
    if not root.exists():
        raise SystemExit(f"repo root does not exist: {root}")
    return root


def iter_repo_files(root: Path) -> Iterable[Path]:
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            yield Path(current) / name


def relpath(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def is_agent_visible(rel: str) -> bool:
    return rel.split("/", 1)[0] in AGENT_VISIBLE_DIRS


def should_policy_scan(rel: str) -> bool:
    return rel.split("/", 1)[0] not in POLICY_SCAN_SKIP_DIRS


def contains_secret_material(text: str) -> bool:
    if SECRET_TOKEN_PATTERN.search(text) or PRIVATE_KEY_PATTERN.search(text):
        return True
    for match in SECRET_ASSIGNMENT_PATTERN.finditer(text):
        value = match.group(2).strip()
        if not value:
            continue
        normalized = value.strip("'\"").lower()
        if normalized.startswith(("env:", "${", "$", "<")):
            continue
        if normalized.startswith(("env.get(", "os.getenv(", "os.environ", "process.env")):
            continue
        if re.fullmatch(r"str(?:[,)]|$)", normalized):
            continue
        if re.match(r"^[a-z_][a-z0-9_.]*\(", normalized):
            continue
        if re.fullmatch(r"[a-z_][a-z0-9_]*[,)]", normalized):
            continue
        if normalized in {"changeme", "placeholder", "example", "unset", "none", "null"}:
            continue
        if normalized.startswith(("example_", "your_", "replace_")):
            continue
        return True
    return False


def is_public_ipv4(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return ip.version == 4 and not any(ip in net for net in PRIVATE_NETS)


def parse_config_file(path: Path) -> tuple[Any | None, str | None]:
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            return json.loads(text), None
        return yaml.load(text, Loader=TaggedSafeLoader), None
    except Exception as exc:  # noqa: BLE001 - exact parser errors vary.
        return None, str(exc)


def validate_structured_contract(rel: str, data: Any) -> list[Issue]:
    issues: list[Issue] = []
    if data is None:
        return issues
    if isinstance(data, list) and rel.endswith(".cordis.patch.yml"):
        return issues
    if not isinstance(data, dict):
        issues.append(Issue("ERROR", rel, "top-level document must be a mapping"))
        return issues

    kind = data.get("kind")
    required_by_kind = {
        "ApplicationTarget": (
            "apiVersion",
            "kind",
            "metadata.name",
            "spec.namespace",
            "spec.sourceRef",
            "spec.observability",
        ),
        "EpisodeSpec": (
            "apiVersion",
            "kind",
            "metadata.name",
            "spec.application",
            "spec.visibleInputs",
            "spec.safety",
            "spec.budget",
            "spec.oracleRef",
        ),
        "HarnessSpec": (
            "apiVersion",
            "kind",
            "metadata.name",
            "spec.agent",
            "spec.models",
            "spec.tools",
        ),
        "McpEndpointSet": (
            "apiVersion",
            "kind",
            "metadata.name",
            "spec.servers",
        ),
    }
    required = required_by_kind.get(str(kind))
    if not required:
        return issues

    for dotted in required:
        if lookup_dotted(data, dotted) is None:
            issues.append(Issue("ERROR", rel, f"missing required field {dotted} for {kind}"))
    return issues


def lookup_dotted(data: dict[str, Any], dotted: str) -> Any:
    current: Any = data
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def validate_ground_truth_keys(rel: str, data: Any) -> list[Issue]:
    if not is_agent_visible(rel):
        return []
    issues: list[Issue] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                if GROUND_TRUTH_KEY_PATTERN.search(str(key)):
                    issues.append(Issue("ERROR", rel, f"ground truth key is agent-visible at {child_path}"))
                walk(child, child_path)
        elif isinstance(value, list):
            for idx, child in enumerate(value):
                walk(child, f"{path}[{idx}]")

    walk(data, "")
    return issues


def validate_static_repo(root: Path, strict: bool = False) -> list[Issue]:
    issues: list[Issue] = []

    for required in REPO_REQUIRED_FILES:
        if not (root / required).is_file():
            issues.append(Issue("ERROR", required, "required repository scaffold file is missing"))

    config_count = 0
    for path in iter_repo_files(root):
        rel = relpath(root, path)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        if not should_policy_scan(rel):
            continue

        if contains_secret_material(text):
            issues.append(Issue("ERROR", rel, "possible secret material found"))

        if USER_ABS_PATH_PATTERN.search(text):
            issues.append(Issue("ERROR", rel, "user absolute path found"))

        for candidate in PUBLIC_IPV4_PATTERN.findall(text):
            if is_public_ipv4(candidate):
                issues.append(Issue("ERROR", rel, f"public IPv4 address found: {candidate}"))

        if (
            is_agent_visible(rel)
            and GROUND_TRUTH_KEY_PATTERN.search(rel)
            and rel not in SAFE_PRIVATE_CONTRACT_FILES
        ):
            issues.append(Issue("ERROR", rel, "ground truth file path is agent-visible"))

        if path.suffix.lower() in CONFIG_SUFFIXES:
            config_count += 1
            data, error = parse_config_file(path)
            if error:
                issues.append(Issue("ERROR", rel, f"cannot parse {path.suffix} config: {error}"))
                continue
            issues.extend(validate_structured_contract(rel, data))
            if rel not in SAFE_PRIVATE_CONTRACT_FILES:
                issues.extend(validate_ground_truth_keys(rel, data))

    if config_count == 0:
        issues.append(Issue("WARN", ".", "no YAML/JSON configuration files found yet"))

    if strict:
        issues = [Issue("ERROR" if i.severity == "WARN" else i.severity, i.location, i.message) for i in issues]
    return issues


def run_kubectl(kubeconfig: Path, args: list[str]) -> Any:
    if not args or args[0] != "get":
        raise RuntimeError("refusing kubectl invocation outside the read-only get allowlist")
    cmd = ["kubectl", "--kubeconfig", str(kubeconfig), *args]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("kubectl read-only qualification timed out after 30 seconds") from exc
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"kubectl failed: {' '.join(args)}")
    if "-o" in args and "json" in args:
        return json.loads(proc.stdout)
    return proc.stdout


def qualify_cluster(args: argparse.Namespace) -> int:
    kubeconfig = Path(args.kubeconfig).expanduser().resolve()
    if not kubeconfig.is_file():
        print(f"ERROR: --kubeconfig does not exist or is not a file: {kubeconfig}", file=sys.stderr)
        return 2

    report: dict[str, Any] = {
        "mode": "read-only",
        "kubeconfigSource": "explicit-runtime-file",
        "targets": list(args.namespace),
        "observabilityNamespaces": list(args.observability_namespace),
        "checks": {},
        "issues": [],
    }

    try:
        nodes = run_kubectl(kubeconfig, ["get", "nodes", "-o", "json"])
        report["checks"]["nodes"] = summarize_nodes(nodes)
    except Exception as exc:  # noqa: BLE001
        report["issues"].append({"severity": "ERROR", "check": "nodes", "message": str(exc)})

    for namespace in args.namespace:
        report["checks"].setdefault("namespaces", {})[namespace] = qualify_namespace(kubeconfig, namespace)

    report["checks"]["observability"] = {}
    for namespace in args.observability_namespace:
        report["checks"]["observability"][namespace] = qualify_observability(kubeconfig, namespace)

    report["checks"]["chaosblade"] = qualify_chaosblade(kubeconfig)

    collected_issues = collect_cluster_issues(report)
    issue_count = sum(1 for item in collected_issues if item.get("severity") == "ERROR")
    warning_count = sum(1 for item in collected_issues if item.get("severity") == "WARN")
    report["qualification"] = {
        "passed": issue_count == 0,
        "errorCount": issue_count,
        "warningCount": warning_count,
    }
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"wrote read-only qualification report: {output_path}")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if issue_count else 0


def summarize_nodes(nodes: dict[str, Any]) -> dict[str, Any]:
    items = nodes.get("items", [])
    ready = 0
    names: list[str] = []
    for node in items:
        names.append(node.get("metadata", {}).get("name", "unknown"))
        conditions = node.get("status", {}).get("conditions", [])
        if any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions):
            ready += 1
    return {"count": len(items), "ready": ready, "names": names}


def qualify_namespace(kubeconfig: Path, namespace: str) -> dict[str, Any]:
    result: dict[str, Any] = {"exists": False, "deployments": [], "loadGenerators": [], "issues": []}
    try:
        run_kubectl(kubeconfig, ["get", "namespace", namespace, "-o", "json"])
        result["exists"] = True
    except Exception as exc:  # noqa: BLE001
        result["issues"].append({"severity": "ERROR", "message": str(exc)})
        return result

    try:
        deployments = run_kubectl(kubeconfig, ["get", "deployments", "-n", namespace, "-o", "json"])
        for item in deployments.get("items", []):
            name = item.get("metadata", {}).get("name", "unknown")
            spec_replicas = int(item.get("spec", {}).get("replicas") or 0)
            ready = int(item.get("status", {}).get("readyReplicas") or 0)
            available = int(item.get("status", {}).get("availableReplicas") or 0)
            summary = {"name": name, "replicas": spec_replicas, "ready": ready, "available": available}
            result["deployments"].append(summary)
            if spec_replicas > 0 and ready < spec_replicas:
                result["issues"].append({
                    "severity": "ERROR",
                    "message": f"deployment {name} ready {ready}/{spec_replicas}",
                })
            if "load" in name.lower() and "generat" in name.lower():
                result["loadGenerators"].append(summary)
                if spec_replicas == 0:
                    result["issues"].append({"severity": "WARN", "message": f"load generator {name} has 0 replicas"})
    except Exception as exc:  # noqa: BLE001
        result["issues"].append({"severity": "ERROR", "message": str(exc)})

    if not result["loadGenerators"]:
        result["issues"].append({"severity": "WARN", "message": "no load generator deployment detected"})
    if not result["deployments"]:
        result["issues"].append({"severity": "ERROR", "message": "target namespace has no deployments"})
    elif sum(item["replicas"] for item in result["deployments"]) == 0:
        result["issues"].append({"severity": "ERROR", "message": "all target deployments are scaled to zero"})
    return result


def qualify_observability(kubeconfig: Path, namespace: str) -> dict[str, Any]:
    result: dict[str, Any] = {"exists": False, "services": [], "workloads": [], "issues": []}
    try:
        run_kubectl(kubeconfig, ["get", "namespace", namespace, "-o", "json"])
        result["exists"] = True
    except Exception as exc:  # noqa: BLE001
        result["issues"].append({"severity": "WARN", "message": str(exc)})
        return result

    try:
        services = run_kubectl(kubeconfig, ["get", "services", "-n", namespace, "-o", "json"])
        for svc in services.get("items", []):
            name = svc.get("metadata", {}).get("name", "unknown")
            ports = [port.get("port") for port in svc.get("spec", {}).get("ports", [])]
            result["services"].append({"name": name, "ports": ports})
    except Exception as exc:  # noqa: BLE001
        result["issues"].append({"severity": "WARN", "message": str(exc)})
    try:
        workloads = run_kubectl(
            kubeconfig,
            ["get", "deployments,daemonsets,statefulsets", "-n", namespace, "-o", "json"],
        )
        for item in workloads.get("items", []):
            kind = item.get("kind", "Unknown")
            name = item.get("metadata", {}).get("name", "unknown")
            spec = item.get("spec", {})
            status = item.get("status", {})
            if kind == "DaemonSet":
                desired = int(status.get("desiredNumberScheduled") or 0)
                ready = int(status.get("numberReady") or 0)
            else:
                desired = int(spec.get("replicas") or 0)
                ready = int(status.get("readyReplicas") or 0)
            summary = {"kind": kind, "name": name, "desired": desired, "ready": ready}
            result["workloads"].append(summary)
            if desired > 0 and ready < desired:
                result["issues"].append(
                    {"severity": "ERROR", "message": f"{kind} {name} ready {ready}/{desired}"}
                )
    except Exception as exc:  # noqa: BLE001
        result["issues"].append({"severity": "WARN", "message": str(exc)})
    return result


def qualify_chaosblade(kubeconfig: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"installed": False, "count": 0, "phases": {}, "issues": []}
    for resource in ("chaosblades.chaosblade.io", "chaosblade"):
        try:
            blades = run_kubectl(kubeconfig, ["get", resource, "-A", "-o", "json"])
            result["installed"] = True
            for item in blades.get("items", []):
                result["count"] += 1
                phase = item.get("status", {}).get("phase") or item.get("status", {}).get("state") or "Unknown"
                result["phases"][phase] = result["phases"].get(phase, 0) + 1
            unsafe_count = sum(
                count
                for phase, count in result["phases"].items()
                if str(phase).lower() not in {"destroyed", "completed", "finished"}
            )
            if unsafe_count:
                result["issues"].append(
                    {
                        "severity": "ERROR",
                        "message": f"{unsafe_count} pre-existing nonterminal ChaosBlade resources require ownership and residual-state reconciliation",
                    }
                )
            return result
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
    result["issues"].append({"severity": "WARN", "message": f"ChaosBlade CRs not readable: {last_error}"})
    return result


def collect_cluster_issues(report: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = list(report.get("issues", []))
    namespaces = report.get("checks", {}).get("namespaces", {})
    for namespace, details in namespaces.items():
        for item in details.get("issues", []):
            issues.append({"namespace": namespace, **item})
    observability = report.get("checks", {}).get("observability", {})
    for namespace, details in observability.items():
        for item in details.get("issues", []):
            issues.append({"namespace": namespace, **item})
    for item in report.get("checks", {}).get("chaosblade", {}).get("issues", []):
        issues.append({"check": "chaosblade", **item})
    return issues


def print_issues(issues: list[Issue]) -> None:
    for issue in issues:
        print(issue.line())


def command_validate_repo(args: argparse.Namespace) -> int:
    issues = validate_static_repo(repo_root_from_arg(args.repo), strict=args.strict)
    print_issues(issues)
    errors = [issue for issue in issues if issue.severity == "ERROR"]
    if errors:
        print(f"repository validation failed with {len(errors)} error(s)", file=sys.stderr)
        return 1
    print("repository validation passed")
    return 0


def command_dry_run(args: argparse.Namespace) -> int:
    issues = validate_static_repo(repo_root_from_arg(args.repo), strict=args.strict)
    print_issues(issues)
    if any(issue.severity == "ERROR" for issue in issues):
        print("dry-run blocked by repository validation errors", file=sys.stderr)
        return 1
    plan = {
        "mode": "dry-run",
        "willNotRun": ["kubectl apply", "kubectl delete", "kubectl patch", "kubectl exec", "fault injection"],
        "nextReadOnlyChecks": [
            "validate repository contracts",
            "qualify target namespaces and deployments",
            "qualify observability services",
            "inventory ChaosBlade custom resources",
        ],
    }
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BenchmarkFactory preparation validator")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate-repo", help="validate repository scaffold and safe visible files")
    validate.add_argument("--repo", default=".", help="repository root, defaults to current directory")
    validate.add_argument("--strict", action="store_true", help="treat warnings as errors")
    validate.set_defaults(func=command_validate_repo)

    dry_run = sub.add_parser("dry-run", help="print non-mutating preparation plan after repo validation")
    dry_run.add_argument("--repo", default=".", help="repository root, defaults to current directory")
    dry_run.add_argument("--strict", action="store_true", help="treat warnings as errors")
    dry_run.set_defaults(func=command_dry_run)

    qualify = sub.add_parser("qualify-cluster", help="run read-only Kubernetes qualification checks")
    qualify.add_argument("--kubeconfig", required=True, help="explicit kubeconfig path; no default is used")
    qualify.add_argument("--namespace", action="append", required=True, help="target application namespace")
    qualify.add_argument(
        "--observability-namespace",
        action="append",
        default=[],
        help="namespace expected to contain Prometheus/Jaeger/Loki/OTel services",
    )
    qualify.add_argument("--output", help="optional JSON report path, e.g. artifacts/qualification/cluster.json")
    qualify.set_defaults(func=qualify_cluster)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
