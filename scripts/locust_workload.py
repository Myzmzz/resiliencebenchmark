#!/usr/bin/env python3
"""Render and manage deterministic Sock Shop and OTel Demo Locust Jobs."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Protocol
from urllib.parse import urlparse

import yaml


PROFILE_PATH = Path("environment/workloads/deterministic-profiles.yaml")
WORKLOAD_ROOT = Path("environment/workloads")
SUPPORTED_APPLICATIONS = {"sock-shop", "otel-demo"}
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,62}$")
IMAGE_RE = re.compile(r"^.+:[^/@]+@sha256:[0-9a-f]{64}$")
IMAGE_PLACEHOLDER_RE = re.compile(r"^.+:\{\{[A-Z0-9_]+\}\}@\{\{[A-Z0-9_]*DIGEST[A-Z0-9_]*\}\}$")
HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")


class WorkloadError(ValueError):
    """The workload plan is incomplete or unsafe."""


class Runner(Protocol):
    def run(self, argv: list[str], *, stdin: str | None = None) -> str:
        """Run a fixed command."""


class SubprocessRunner:
    def run(self, argv: list[str], *, stdin: str | None = None) -> str:
        completed = subprocess.run(argv, input=stdin, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if completed.returncode:
            raise WorkloadError(completed.stderr.strip() or "workload kubectl command failed")
        return completed.stdout.strip()


@dataclass(frozen=True)
class Fixture:
    base_url: str
    base_url_ref: str
    allowed_hosts: tuple[str, ...]
    allowed_namespaces: tuple[str, ...]
    pvc_claim: str
    secret_name: str | None
    username_key: str | None
    password_key: str | None


def load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise WorkloadError(f"{path} must contain a mapping")
    return data


def mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkloadError(f"{field} must be a mapping")
    return value


def load_application(application: str, profile_path: Path = PROFILE_PATH) -> tuple[dict[str, Any], dict[str, Any]]:
    if application not in SUPPORTED_APPLICATIONS:
        raise WorkloadError("application must be sock-shop or otel-demo")
    data = load_yaml(profile_path)
    spec = mapping(data.get("spec"), "spec")
    defaults = mapping(spec.get("defaults"), "spec.defaults")
    matches = [item for item in spec.get("applications", []) if isinstance(item, dict) and item.get("id") == application]
    if len(matches) != 1:
        raise WorkloadError("deterministic profile must contain the selected application exactly once")
    return defaults, matches[0]


def load_fixture(path: Path, application: str) -> Fixture:
    data = load_yaml(path)
    target = mapping(data.get("target"), "target")
    parsed = urlparse(str(target.get("base_url") or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise WorkloadError("target.base_url must be an http(s) URL without userinfo")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise WorkloadError("target.base_url must not contain path, query, or fragment")
    hosts = target.get("allowed_hosts")
    if not isinstance(hosts, list) or not hosts or any(not isinstance(host, str) or not HOST_RE.fullmatch(host) for host in hosts):
        raise WorkloadError("target.allowed_hosts must contain safe hostnames")
    normalized = tuple(host.lower().rstrip(".") for host in hosts)
    if len(normalized) != len(set(normalized)) or parsed.hostname.lower().rstrip(".") not in normalized:
        raise WorkloadError("target hostname must appear exactly once in target.allowed_hosts")
    cluster = mapping(data.get("cluster"), "cluster")
    namespaces = cluster.get("allowed_namespaces")
    if namespaces != [application]:
        raise WorkloadError("fixture must allow only the selected application namespace")
    artifacts = mapping(data.get("artifacts"), "artifacts")
    pvc = str(artifacts.get("pvc_claim") or "")
    if not RUN_ID_RE.fullmatch(pvc):
        raise WorkloadError("artifacts.pvc_claim must be a safe Kubernetes name")
    credentials = data.get("credentials")
    secret_name = username_key = password_key = None
    if credentials is not None:
        secret = mapping(mapping(credentials, "credentials").get("kubernetes_secret_ref"), "credentials.kubernetes_secret_ref")
        secret_name = str(secret.get("name") or "")
        username_key = str(secret.get("username_key") or "")
        password_key = str(secret.get("password_key") or "")
        if not all(RUN_ID_RE.fullmatch(value) for value in (secret_name, username_key, password_key)):
            raise WorkloadError("credential references must be safe Kubernetes names")
    if application == "sock-shop" and not secret_name:
        raise WorkloadError("Sock Shop fixture requires a benchmark user Secret reference")
    return Fixture(
        base_url=str(target["base_url"]).rstrip("/"),
        base_url_ref=str(target.get("base_url_ref") or ""),
        allowed_hosts=normalized,
        allowed_namespaces=(application,),
        pvc_claim=pvc,
        secret_name=secret_name,
        username_key=username_key,
        password_key=password_key,
    )


def validate_image(image: str) -> str:
    if not (IMAGE_RE.fullmatch(image) or IMAGE_PLACEHOLDER_RE.fullmatch(image)):
        raise WorkloadError("workload image must be repository:tag@sha256 or the declared digest placeholder")
    return image


def code_files(application: str) -> dict[str, str]:
    files = {
        "locustfile.py": (WORKLOAD_ROOT / application / "locustfile.py").read_text(encoding="utf-8"),
        "deterministic.py": (WORKLOAD_ROOT / "common" / "deterministic.py").read_text(encoding="utf-8"),
        "locust_runner.py": (WORKLOAD_ROOT / "common" / "locust_runner.py").read_text(encoding="utf-8"),
    }
    if sum(len(value.encode("utf-8")) for value in files.values()) > 700_000:
        raise WorkloadError("workload code exceeds the ConfigMap size budget")
    return files


def object_name(application: str, run_id: str) -> str:
    if not RUN_ID_RE.fullmatch(run_id):
        raise WorkloadError("run_id must be a safe lowercase Kubernetes name")
    return f"rb-load-{application[:12]}-{run_id[:32]}".rstrip("-")


def render_plan(application: str, run_id: str, defaults: dict[str, Any], app: dict[str, Any], fixture: Fixture, image: str, baseline_throughput: float | None) -> dict[str, Any]:
    name = object_name(application, run_id)
    namespace = application
    executor = mapping(app.get("executor"), "executor")
    load = mapping(app.get("load"), "load")
    slo = mapping(app.get("entrySlo"), "entrySlo")
    seed = int(mapping(app.get("determinism"), "determinism")["randomSeed"])
    mix = app.get("trafficMix")
    result_prefix = f"/results/{application}-baseline"
    labels = {
        "app.kubernetes.io/name": "resiliencebenchmark-load-generator",
        "app.kubernetes.io/part-of": "resiliencebenchmark",
        "resiliencebenchmark.io/managed-by": "controller",
        "resiliencebenchmark.io/workload": application,
        "resiliencebenchmark.io/run-id": run_id,
    }
    env = [
        {"name": "RESBENCH_APPLICATION", "value": application},
        {"name": "RESBENCH_RUN_ID", "value": run_id},
        {"name": "RESBENCH_RANDOM_SEED", "value": str(seed)},
        {"name": "RESBENCH_TRAFFIC_MIX_JSON", "value": json.dumps(mix, separators=(",", ":"))},
        {"name": "RESBENCH_LOCUSTFILE", "value": "/etc/resbench-workload/locustfile.py"},
        {"name": "RESBENCH_TARGET_URL", "value": fixture.base_url},
        {"name": "RESBENCH_USERS", "value": str(load["users"])},
        {"name": "RESBENCH_SPAWN_RATE", "value": str(load["spawnRatePerSecond"])},
        {"name": "RESBENCH_DURATION_SECONDS", "value": str(defaults["durationSeconds"])},
        {"name": "RESBENCH_WARMUP_SECONDS", "value": str(defaults["warmupSeconds"])},
        {"name": "RESBENCH_EVALUATION_WINDOW_SECONDS", "value": str(defaults["evaluationWindowSeconds"])},
        {"name": "RESBENCH_CSV_PREFIX", "value": result_prefix},
        {"name": "RESBENCH_SUMMARY_PATH", "value": f"/results/{application}-summary.json"},
        {"name": "RESBENCH_MINIMUM_SUCCESS_RATE", "value": str(slo["minimumSuccessRate"])},
        {"name": "RESBENCH_MAXIMUM_ERROR_RATE", "value": str(slo["maximumErrorRate"])},
        {"name": "RESBENCH_MAXIMUM_P95_LATENCY_MS", "value": str(slo["p95LatencyMs"])},
        {"name": "RESBENCH_MINIMUM_THROUGHPUT_RATIO", "value": str(slo["minimumThroughputRatio"])},
        {"name": "RESBENCH_MINIMUM_SAMPLES", "value": str(defaults["minimumSamples"])},
        {"name": "RESBENCH_ALLOWED_HOSTS", "value": ",".join(fixture.allowed_hosts)},
        {"name": "PYTHONPATH", "value": "/etc/resbench-workload"},
    ]
    if baseline_throughput is not None:
        if baseline_throughput <= 0:
            raise WorkloadError("baseline throughput must be positive")
        env.append({"name": "RESBENCH_BASELINE_THROUGHPUT_RPS", "value": str(baseline_throughput)})
    if fixture.secret_name:
        env.extend(
            [
                {"name": "SOCK_SHOP_USERNAME", "valueFrom": {"secretKeyRef": {"name": fixture.secret_name, "key": fixture.username_key}}},
                {"name": "SOCK_SHOP_PASSWORD", "valueFrom": {"secretKeyRef": {"name": fixture.secret_name, "key": fixture.password_key}}},
            ]
        )
    config_map = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": namespace, "labels": labels}, "data": code_files(application)}
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "workload-generator",
                            "image": validate_image(image),
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["python", "/etc/resbench-workload/locust_runner.py"],
                            "env": env,
                            "volumeMounts": [
                                {"name": "code", "mountPath": "/etc/resbench-workload", "readOnly": True},
                                {"name": "results", "mountPath": "/results"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "code", "configMap": {"name": name, "defaultMode": 0o444}},
                        {"name": "results", "persistentVolumeClaim": {"claimName": fixture.pvc_claim}},
                    ],
                },
            },
        },
    }
    return {
        "application": application,
        "runId": run_id,
        "namespace": namespace,
        "targetUrlRef": fixture.base_url_ref,
        "randomSeed": seed,
        "trafficMix": mix,
        "load": load,
        "durationSeconds": defaults["durationSeconds"],
        "entrySlo": slo,
        "resultArtifact": executor["resultArtifact"],
        "objects": [{"kind": "ConfigMap", "name": name}, {"kind": "Job", "name": name}],
        "manifest": [config_map, job],
    }


def manifest_yaml(items: list[dict[str, Any]]) -> str:
    return "---\n".join(yaml.safe_dump(item, sort_keys=True) for item in items)


def kubectl(kubeconfig: Path, namespace: str) -> list[str]:
    if not kubeconfig.is_absolute() or not kubeconfig.is_file():
        raise WorkloadError("execute requires an explicit existing absolute kubeconfig")
    return ["kubectl", "--kubeconfig", str(kubeconfig), "-n", namespace]


def run(argv: list[str] | None = None, runner: Runner | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("render", "validate", "start", "status", "stop"))
    parser.add_argument("--application", choices=sorted(SUPPORTED_APPLICATIONS), required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--profile-file", type=Path, default=PROFILE_PATH)
    parser.add_argument("--run-id", default="baseline")
    parser.add_argument("--image")
    parser.add_argument("--baseline-throughput-rps", type=float)
    parser.add_argument("--duration-seconds", type=int)
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    active = runner or SubprocessRunner()
    try:
        defaults, app = load_application(args.application, args.profile_file)
        defaults = dict(defaults)
        if args.duration_seconds is not None:
            if not 60 <= args.duration_seconds <= 21_600:
                raise WorkloadError("--duration-seconds must be between 60 and 21600")
            defaults["durationSeconds"] = args.duration_seconds
        fixture = load_fixture(args.fixture, args.application)
        image = args.image or str(mapping(app.get("executor"), "executor").get("image") or "")
        plan = render_plan(args.application, args.run_id, defaults, app, fixture, image, args.baseline_throughput_rps)
        if args.command == "validate":
            print(json.dumps({"valid": True, "plan": {key: value for key, value in plan.items() if key != "manifest"}}, indent=2))
            return 0
        if args.command == "render":
            print(json.dumps(plan, indent=2))
            return 0
        if not args.execute:
            print(json.dumps({"dryRun": True, "action": args.command, "plan": {key: value for key, value in plan.items() if key != "manifest"}}, indent=2))
            return 0
        base = kubectl(args.kubeconfig, args.application)
        selector = f"resiliencebenchmark.io/workload={args.application},resiliencebenchmark.io/run-id={args.run_id}"
        if args.command == "start":
            if not IMAGE_RE.fullmatch(image):
                raise WorkloadError("real start requires --image as repository:tag@sha256:digest")
            output = active.run([*base, "apply", "-f", "-"], stdin=manifest_yaml(plan["manifest"]))
        elif args.command == "status":
            output = active.run([*base, "get", "jobs,pods", "-l", selector, "-o", "json"])
        else:
            output = active.run([*base, "delete", "job,configmap", "-l", selector, "--ignore-not-found=true"])
        print(json.dumps({"dryRun": False, "action": args.command, "output": output, "runId": args.run_id}))
        return 0
    except (OSError, yaml.YAMLError, WorkloadError, KeyError, TypeError, ValueError) as exc:
        print(f"locust_workload: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(run())
