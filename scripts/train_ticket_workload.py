#!/usr/bin/env python3
"""Render and manage controller-owned Train-Ticket workload jobs.

The command is dry-run by default. Mutating Kubernetes actions require
``--execute``, an explicit kubeconfig, and a namespace that is present in the
runtime fixture allowlist.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import yaml


DEFAULT_PROFILE_PATH = Path("environment/workloads/train-ticket/profiles.yaml")
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,62}$")
K8S_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
IMAGE_DIGEST_RE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
IMAGE_DIGEST_PLACEHOLDER_RE = re.compile(r"^.+@\{\{[A-Z0-9_]*DIGEST[A-Z0-9_]*\}\}$")
SAFE_PROFILE_IDS = {"baseline", "search", "login", "order"}


class WorkloadError(Exception):
    """Raised when a workload plan is unsafe or invalid."""


class CommandRunner(Protocol):
    def run(self, argv: list[str], *, stdin: str | None = None) -> str:
        """Run a command and return stdout."""


class SubprocessRunner:
    def run(self, argv: list[str], *, stdin: str | None = None) -> str:
        completed = subprocess.run(
            argv,
            input=stdin,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if completed.returncode != 0:
            raise WorkloadError(
                f"command failed: {' '.join(argv)}: {completed.stderr.strip() or completed.stdout.strip()}"
            )
        return completed.stdout.strip()


@dataclass(frozen=True)
class RuntimeFixture:
    base_url: str
    base_url_ref: str
    username_env: str | None
    password_env: str | None
    secret_name: str | None
    username_key: str | None
    password_key: str | None
    allowed_hosts: tuple[str, ...]
    allowed_namespaces: tuple[str, ...]
    pvc_claim: str
    scenario: dict[str, str]


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise WorkloadError(f"{path} top-level document must be a mapping")
    return data


def load_profile(profile_path: Path, profile_id: str) -> dict[str, Any]:
    data = load_yaml(profile_path)
    spec = data.get("spec")
    if not isinstance(spec, dict):
        raise WorkloadError("profile file missing spec")
    generator = spec.get("generator")
    if not isinstance(generator, dict):
        raise WorkloadError("profile file missing spec.generator")
    profiles = spec.get("profiles")
    if not isinstance(profiles, list):
        raise WorkloadError("profile file missing spec.profiles")
    matches = [item for item in profiles if isinstance(item, dict) and item.get("id") == profile_id]
    if len(matches) != 1:
        raise WorkloadError(f"unknown workload profile: {profile_id}")
    profile = dict(matches[0])
    profile["generator"] = generator
    validate_profile(profile)
    return profile


def validate_profile(profile: dict[str, Any]) -> None:
    profile_id = profile.get("id")
    if profile_id not in SAFE_PROFILE_IDS:
        raise WorkloadError(f"profile id is not controller-approved: {profile_id}")
    for key in ("targetFlowQps", "concurrency", "durationSeconds", "steps", "abortThresholds", "generator"):
        if key not in profile:
            raise WorkloadError(f"profile {profile_id} missing {key}")
    if not isinstance(profile["concurrency"], int) or not 1 <= profile["concurrency"] <= 200:
        raise WorkloadError("concurrency must be between 1 and 200")
    if not isinstance(profile["durationSeconds"], int) or not 60 <= profile["durationSeconds"] <= 21600:
        raise WorkloadError("durationSeconds must be between 60 and 21600")
    if not isinstance(profile["targetFlowQps"], (int, float)) or not 0 < float(profile["targetFlowQps"]) <= 100:
        raise WorkloadError("targetFlowQps must be between 0 and 100")
    if not isinstance(profile["steps"], list) or not profile["steps"]:
        raise WorkloadError("steps must be a non-empty list")
    thresholds = profile["abortThresholds"]
    if not isinstance(thresholds, dict):
        raise WorkloadError("abortThresholds must be a mapping")
    for key in ("maxErrorRate", "maxP95LatencyMs", "maxConsecutiveFailures"):
        if key not in thresholds:
            raise WorkloadError(f"abortThresholds missing {key}")
    image = str(profile["generator"].get("image", ""))
    if not (IMAGE_DIGEST_RE.match(image) or IMAGE_DIGEST_PLACEHOLDER_RE.match(image)):
        raise WorkloadError("generator image must be pinned by sha256 digest or digest placeholder")
    if profile_id == "baseline":
        seed = profile.get("randomSeed")
        if not isinstance(seed, int) or isinstance(seed, bool) or seed <= 0:
            raise WorkloadError("baseline randomSeed must be a positive integer")
        mix = profile.get("trafficMix")
        if not isinstance(mix, list) or not mix:
            raise WorkloadError("baseline trafficMix must be a non-empty list")
        flows = [item.get("flow") for item in mix if isinstance(item, dict)]
        if set(flows) != {"search", "login", "order"} or len(flows) != 3:
            raise WorkloadError("baseline trafficMix must define search, login, and order exactly once")
        weights = [item.get("weightPercent") for item in mix if isinstance(item, dict)]
        if any(not isinstance(weight, int) or isinstance(weight, bool) or weight <= 0 for weight in weights):
            raise WorkloadError("baseline trafficMix weights must be positive integers")
        if sum(weights) != 100:
            raise WorkloadError("baseline trafficMix weights must sum to 100")


def load_fixture(fixture_path: Path) -> RuntimeFixture:
    data = load_yaml(fixture_path)
    target = require_mapping(data, "target")
    credentials = require_mapping(data, "credentials")
    cluster = require_mapping(data, "cluster")
    scenario = require_mapping(data, "scenario")

    base_url = require_string(target, "base_url")
    base_url_ref = str(target.get("base_url_ref") or "runtime://train-ticket/base-url")
    allowed_hosts = parse_allowed_hosts(target)
    validate_base_url(base_url, allowed_hosts)
    username_env = optional_env_name(credentials.get("username_env"), "username_env")
    password_env = optional_env_name(credentials.get("password_env"), "password_env")
    secret_ref = credentials.get("kubernetes_secret_ref")
    secret_name = username_key = password_key = None
    if secret_ref is not None:
        if not isinstance(secret_ref, dict):
            raise WorkloadError("credentials.kubernetes_secret_ref must be a mapping")
        secret_name = require_k8s_name(secret_ref, "name")
        username_key = require_k8s_name(secret_ref, "username_key")
        password_key = require_k8s_name(secret_ref, "password_key")

    forbidden_literal_keys = {"username", "password", "token", "api_key", "secret"}
    leaked = sorted(key for key in forbidden_literal_keys if key in credentials)
    if leaked:
        raise WorkloadError(f"literal credential fields are not allowed: {', '.join(leaked)}")
    if not ((username_env and password_env) or (secret_name and username_key and password_key)):
        raise WorkloadError("fixture must provide env credential refs or a Kubernetes Secret ref")

    allowed_raw = cluster.get("allowed_namespaces")
    if not isinstance(allowed_raw, list) or not allowed_raw:
        raise WorkloadError("cluster.allowed_namespaces must be a non-empty list")
    allowed = tuple(require_k8s_name({"value": item}, "value") for item in allowed_raw)
    artifacts = require_mapping(data, "artifacts")
    pvc_claim = require_k8s_name(artifacts, "pvc_claim")
    scenario_values = {
        "from_station": require_string(scenario, "from_station"),
        "to_station": require_string(scenario, "to_station"),
        "travel_date": require_string(scenario, "travel_date"),
    }
    return RuntimeFixture(
        base_url=base_url.rstrip("/"),
        base_url_ref=base_url_ref,
        username_env=username_env,
        password_env=password_env,
        secret_name=secret_name,
        username_key=username_key,
        password_key=password_key,
        allowed_hosts=allowed_hosts,
        allowed_namespaces=allowed,
        pvc_claim=pvc_claim,
        scenario=scenario_values,
    )


def require_mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise WorkloadError(f"fixture missing {key}")
    return value


def require_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WorkloadError(f"{key} must be a non-empty string")
    return value.strip()


def require_k8s_name(data: dict[str, Any], key: str) -> str:
    value = require_string(data, key)
    if not K8S_NAME_RE.match(value):
        raise WorkloadError(f"{key} must be a safe Kubernetes name")
    return value


def parse_allowed_hosts(target: dict[str, Any]) -> tuple[str, ...]:
    raw = target.get("allowed_hosts")
    if not isinstance(raw, list) or not raw:
        raise WorkloadError("target.allowed_hosts must be a non-empty list")
    hosts = tuple(normalize_allowed_host(item) for item in raw)
    if len(set(hosts)) != len(hosts):
        raise WorkloadError("target.allowed_hosts contains duplicate hosts")
    return hosts


def normalize_allowed_host(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkloadError("allowed host must be a non-empty string")
    host = value.strip().lower().rstrip(".")
    if "/" in host or "@" in host or ":" in host:
        raise WorkloadError("allowed host must be a hostname without scheme, userinfo, port, or path")
    labels = host.split(".")
    if not all(re.match(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", label) for label in labels):
        raise WorkloadError(f"allowed host is not a valid DNS hostname: {value}")
    return host


def validate_base_url(base_url: str, allowed_hosts: tuple[str, ...]) -> None:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"}:
        raise WorkloadError("target.base_url must use http or https")
    if not parsed.netloc or not parsed.hostname:
        raise WorkloadError("target.base_url must include a hostname")
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise WorkloadError("target.base_url must not contain userinfo")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise WorkloadError("target.base_url must not contain a path, parameters, query, or fragment")
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname not in allowed_hosts:
        raise WorkloadError("target.base_url hostname is not in target.allowed_hosts")


def optional_env_name(value: Any, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.match(r"^[A-Z][A-Z0-9_]{2,80}$", value):
        raise WorkloadError(f"{key} must be an environment variable name")
    return value


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_RE.match(run_id):
        raise WorkloadError("run_id must match ^[a-z0-9][a-z0-9-]{2,62}$")
    return run_id


def assert_namespace_allowed(namespace: str, fixture: RuntimeFixture) -> None:
    if namespace not in fixture.allowed_namespaces:
        raise WorkloadError(f"namespace {namespace} is not in fixture allowlist")


def assert_kubeconfig(path: Path | None) -> Path:
    if path is None:
        raise WorkloadError("real Kubernetes actions require --kubeconfig")
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise WorkloadError(f"kubeconfig does not exist: {resolved}")
    return resolved


def object_name(run_id: str) -> str:
    suffix = run_id[:40].rstrip("-")
    return f"tt-workload-{suffix}"


def common_labels(run_id: str) -> dict[str, str]:
    return {
        "app.kubernetes.io/name": "train-ticket-workload",
        "app.kubernetes.io/part-of": "resiliencebenchmark",
        "resiliencebenchmark.io/managed-by": "controller",
        "resiliencebenchmark.io/owner": "benchmark-controller",
        "resiliencebenchmark.io/workload": "train-ticket",
        "resiliencebenchmark.io/run-id": run_id,
    }


def workload_config(profile: dict[str, Any], fixture: RuntimeFixture) -> dict[str, Any]:
    config = {
        "profileId": profile["id"],
        "targetUrlRef": fixture.base_url_ref,
        "scenario": fixture.scenario,
        "steps": profile["steps"],
        "targetFlowQps": profile["targetFlowQps"],
        "concurrency": profile["concurrency"],
        "durationSeconds": profile["durationSeconds"],
        "abortThresholds": profile["abortThresholds"],
        "cleanupCreatedOrders": bool(profile.get("cleanupCreatedOrders", True)),
    }
    if profile["id"] == "baseline":
        config["randomSeed"] = profile["randomSeed"]
        config["trafficMix"] = profile["trafficMix"]
    return config


def profile_result_artifact(profile: dict[str, Any]) -> str:
    return str(profile.get("resultArtifact") or profile["generator"]["resultArtifact"])


def render_manifest(run_id: str, namespace: str, profile: dict[str, Any], fixture: RuntimeFixture) -> list[dict[str, Any]]:
    labels = common_labels(run_id)
    name = object_name(run_id)
    config = workload_config(profile, fixture)
    result_artifact = profile_result_artifact(profile)
    env = [
        {"name": "TRAIN_TICKET_BASE_URL", "value": fixture.base_url},
        {"name": "TRAIN_TICKET_BASE_URL_REF", "value": fixture.base_url_ref},
        {"name": "TRAIN_TICKET_ALLOWED_HOSTS", "value": ",".join(fixture.allowed_hosts)},
        {"name": "WORKLOAD_CONFIG_PATH", "value": "/etc/train-ticket-workload/workload.json"},
        {"name": "RESULT_ARTIFACT", "value": result_artifact},
    ]
    if fixture.secret_name:
        env.extend(
            [
                {
                    "name": "TRAIN_TICKET_USERNAME",
                    "valueFrom": {
                        "secretKeyRef": {"name": fixture.secret_name, "key": fixture.username_key},
                    },
                },
                {
                    "name": "TRAIN_TICKET_PASSWORD",
                    "valueFrom": {
                        "secretKeyRef": {"name": fixture.secret_name, "key": fixture.password_key},
                    },
                },
            ]
        )
    else:
        env.extend(
            [
                {"name": "TRAIN_TICKET_USERNAME_ENV", "value": fixture.username_env},
                {"name": "TRAIN_TICKET_PASSWORD_ENV", "value": fixture.password_env},
            ]
        )

    config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "data": {"workload.json": json.dumps(config, sort_keys=True, separators=(",", ":"))},
    }
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": labels,
            "annotations": {
                "resiliencebenchmark.io/result-artifact": result_artifact,
                "resiliencebenchmark.io/result-pvc": fixture.pvc_claim,
                "resiliencebenchmark.io/target-url-ref": fixture.base_url_ref,
            },
        },
        "spec": {
            "backoffLimit": 0,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "workload-generator",
                            "image": profile["generator"]["image"],
                            "imagePullPolicy": "IfNotPresent",
                            "command": profile["generator"]["command"],
                            "env": env,
                            "volumeMounts": [
                                {
                                    "name": "workload-config",
                                    "mountPath": "/etc/train-ticket-workload",
                                    "readOnly": True,
                                },
                                {"name": "results", "mountPath": "/results"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "workload-config", "configMap": {"name": name}},
                        {"name": "results", "persistentVolumeClaim": {"claimName": fixture.pvc_claim}},
                    ],
                },
            },
        },
    }
    return [config_map, job]


def render_plan(run_id: str, namespace: str, profile: dict[str, Any], fixture: RuntimeFixture) -> dict[str, Any]:
    manifest = render_manifest(run_id, namespace, profile, fixture)
    config = workload_config(profile, fixture)
    return {
        "runId": run_id,
        "namespace": namespace,
        "owner": "benchmark-controller",
        "profile": profile["id"],
        "targetUrlRef": fixture.base_url_ref,
        "targetUrlSha256": hashlib.sha256(fixture.base_url.encode("utf-8")).hexdigest(),
        "targetFlowQps": profile["targetFlowQps"],
        "concurrency": profile["concurrency"],
        "durationSeconds": profile["durationSeconds"],
        "abortThresholds": profile["abortThresholds"],
        "resultArtifact": profile_result_artifact(profile),
        "artifactRef": f"pvc://{namespace}/{fixture.pvc_claim}{profile_result_artifact(profile)}",
        "artifactPvcClaim": fixture.pvc_claim,
        "credentialMode": "kubernetesSecretRef" if fixture.secret_name else "environmentRef",
        "workloadConfig": config,
        "randomSeed": profile.get("randomSeed"),
        "trafficMix": profile.get("trafficMix", []),
        "objects": [
            {"kind": item["kind"], "name": item["metadata"]["name"], "namespace": item["metadata"]["namespace"]}
            for item in manifest
        ],
        "manifest": manifest,
    }


def ensure_executable_image(profile: dict[str, Any]) -> None:
    image = str(profile["generator"].get("image", ""))
    if not IMAGE_DIGEST_RE.match(image):
        raise WorkloadError("real start requires a resolved image pinned as name@sha256:digest")


def apply_image_override(profile: dict[str, Any], image: str | None) -> None:
    if image is None:
        return
    resolved = image.strip()
    if not IMAGE_DIGEST_RE.match(resolved):
        raise WorkloadError("--image must be a concrete image reference pinned by sha256 digest")
    profile["generator"]["image"] = resolved


def manifest_yaml(manifest: list[dict[str, Any]]) -> str:
    return "---\n".join(yaml.safe_dump(item, sort_keys=True) for item in manifest)


def kubectl_base(kubeconfig: Path, namespace: str) -> list[str]:
    return ["kubectl", "--kubeconfig", str(kubeconfig), "--namespace", namespace]


def apply_manifest(kubeconfig: Path, namespace: str, manifest: list[dict[str, Any]], runner: CommandRunner) -> str:
    return runner.run(kubectl_base(kubeconfig, namespace) + ["apply", "-f", "-"], stdin=manifest_yaml(manifest))


def get_status(kubeconfig: Path, namespace: str, run_id: str, runner: CommandRunner) -> dict[str, Any]:
    selector = f"resiliencebenchmark.io/workload=train-ticket,resiliencebenchmark.io/run-id={run_id}"
    raw = runner.run(kubectl_base(kubeconfig, namespace) + ["get", "jobs,pods", "-l", selector, "-o", "json"])
    data = json.loads(raw or "{}")
    return {
        "runId": run_id,
        "namespace": namespace,
        "selector": selector,
        "items": [
            {
                "kind": item.get("kind"),
                "name": item.get("metadata", {}).get("name"),
                "phase": item.get("status", {}).get("phase"),
                "succeeded": item.get("status", {}).get("succeeded"),
                "failed": item.get("status", {}).get("failed"),
                "active": item.get("status", {}).get("active"),
            }
            for item in data.get("items", [])
        ],
    }


def stop_run(kubeconfig: Path, namespace: str, run_id: str, runner: CommandRunner) -> str:
    selector = f"resiliencebenchmark.io/workload=train-ticket,resiliencebenchmark.io/run-id={run_id}"
    return runner.run(
        kubectl_base(kubeconfig, namespace) + ["delete", "job,configmap", "-l", selector, "--ignore-not-found=true"]
    )


def write_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare controller-owned Train-Ticket workload jobs.")
    parser.add_argument("command", choices=("render", "validate", "start", "status", "stop"))
    parser.add_argument("--profile-file", type=Path, default=DEFAULT_PROFILE_PATH)
    parser.add_argument("--profile", default="order", choices=sorted(SAFE_PROFILE_IDS))
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--run-id", default="tt-baseline")
    parser.add_argument("--namespace", default="train-ticket")
    parser.add_argument("--image", help="Runtime workload image as repository:tag@sha256:digest")
    parser.add_argument("--duration-seconds", type=int, help="Bounded runtime override for qualification smoke runs")
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument("--execute", action="store_true", help="Perform the Kubernetes action. Default is dry-run.")
    return parser


def run(argv: list[str] | None = None, runner: CommandRunner | None = None) -> int:
    args = build_parser().parse_args(argv)
    runner = runner or SubprocessRunner()
    try:
        run_id = validate_run_id(args.run_id)
        fixture = load_fixture(args.fixture)
        assert_namespace_allowed(args.namespace, fixture)
        profile = load_profile(args.profile_file, args.profile)
        if args.duration_seconds is not None:
            if not 60 <= args.duration_seconds <= 21_600:
                raise WorkloadError("--duration-seconds must be between 60 and 21600")
            profile["durationSeconds"] = args.duration_seconds
        apply_image_override(profile, args.image)
        plan = render_plan(run_id, args.namespace, profile, fixture)

        if args.command == "validate":
            sys.stdout.write(write_json({"valid": True, "plan": summarized_plan(plan)}))
            return 0
        if args.command == "render":
            sys.stdout.write(write_json(plan))
            return 0
        if args.command == "start":
            if not args.execute:
                sys.stdout.write(write_json({"dryRun": True, "action": "start", "plan": summarized_plan(plan)}))
                return 0
            if not fixture.secret_name:
                raise WorkloadError("real Kubernetes start requires credentials.kubernetes_secret_ref")
            ensure_executable_image(profile)
            kubeconfig = assert_kubeconfig(args.kubeconfig)
            output = apply_manifest(kubeconfig, args.namespace, plan["manifest"], runner)
            sys.stdout.write(write_json({"dryRun": False, "action": "start", "kubectl": output, "plan": summarized_plan(plan)}))
            return 0
        if args.command == "status":
            if not args.execute:
                sys.stdout.write(write_json({"dryRun": True, "action": "status", "plan": summarized_plan(plan)}))
                return 0
            kubeconfig = assert_kubeconfig(args.kubeconfig)
            sys.stdout.write(write_json(get_status(kubeconfig, args.namespace, run_id, runner)))
            return 0
        if args.command == "stop":
            if not args.execute:
                sys.stdout.write(write_json({"dryRun": True, "action": "stop", "plan": summarized_plan(plan)}))
                return 0
            kubeconfig = assert_kubeconfig(args.kubeconfig)
            output = stop_run(kubeconfig, args.namespace, run_id, runner)
            sys.stdout.write(write_json({"dryRun": False, "action": "stop", "kubectl": output, "runId": run_id}))
            return 0
    except WorkloadError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2
    raise AssertionError(f"unhandled command: {args.command}")


def summarized_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "runId": plan["runId"],
        "namespace": plan["namespace"],
        "owner": plan["owner"],
        "profile": plan["profile"],
        "targetUrlRef": plan["targetUrlRef"],
        "targetFlowQps": plan["targetFlowQps"],
        "concurrency": plan["concurrency"],
        "durationSeconds": plan["durationSeconds"],
        "abortThresholds": plan["abortThresholds"],
        "resultArtifact": plan["resultArtifact"],
        "artifactRef": plan["artifactRef"],
        "artifactPvcClaim": plan["artifactPvcClaim"],
        "credentialMode": plan["credentialMode"],
        "randomSeed": plan.get("randomSeed"),
        "trafficMix": plan.get("trafficMix", []),
        "objects": plan["objects"],
    }


if __name__ == "__main__":
    raise SystemExit(run())
