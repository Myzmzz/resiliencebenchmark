"""Create short-lived standalone kubeconfigs from Kubernetes TokenRequest."""

from __future__ import annotations

import base64
import json
import os
import ssl
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


class ScopedKubeconfigError(RuntimeError):
    pass


Runner = Callable[[list[str]], str]


def subprocess_runner(argv: list[str]) -> str:
    completed = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode:
        raise ScopedKubeconfigError("scoped kubeconfig command failed")
    return completed.stdout


def create_scoped_kubeconfig(
    *,
    admin_kubeconfig: Path,
    service_account: str,
    output_path: Path,
    namespace: str = "resiliencebenchmark-system",
    duration: str = "6h",
    runner: Runner = subprocess_runner,
) -> dict[str, Any]:
    admin = admin_kubeconfig.expanduser().resolve()
    if not admin.is_file():
        raise ScopedKubeconfigError("admin kubeconfig is missing")
    if not service_account.startswith("resbench-"):
        raise ScopedKubeconfigError("service account is outside the benchmark boundary")
    token = runner(
        [
            "kubectl",
            "--kubeconfig",
            str(admin),
            "create",
            "token",
            service_account,
            "-n",
            namespace,
            f"--duration={duration}",
        ]
    ).strip()
    if len(token) < 32 or any(character.isspace() for character in token):
        raise ScopedKubeconfigError("TokenRequest returned an invalid token")
    raw = runner(
        [
            "kubectl",
            "--kubeconfig",
            str(admin),
            "config",
            "view",
            "--minify",
            "--raw",
            "-o",
            "json",
        ]
    )
    try:
        source = json.loads(raw)
        cluster = dict(source["clusters"][0]["cluster"])
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise ScopedKubeconfigError("admin kubeconfig has no active cluster") from exc
    original_insecure = cluster.get("insecure-skip-tls-verify") is True
    if "certificate-authority-data" not in cluster:
        ca_path = cluster.pop("certificate-authority", None)
        if ca_path:
            ca_bytes = Path(ca_path).expanduser().read_bytes()
            cluster["certificate-authority-data"] = base64.b64encode(ca_bytes).decode(
                "ascii"
            )
        else:
            public_raw = runner(
                [
                    "kubectl",
                    "--kubeconfig",
                    str(admin),
                    "get",
                    "configmap",
                    "cluster-info",
                    "-n",
                    "kube-public",
                    "-o",
                    "json",
                ]
            )
            try:
                public_document = json.loads(public_raw)
                public_config = yaml.safe_load(public_document["data"]["kubeconfig"])
                public_cluster = public_config["clusters"][0]["cluster"]
                cluster["certificate-authority-data"] = public_cluster[
                    "certificate-authority-data"
                ]
            except (
                json.JSONDecodeError,
                KeyError,
                IndexError,
                TypeError,
                yaml.YAMLError,
            ) as exc:
                raise ScopedKubeconfigError(
                    "active cluster has no trustworthy certificate authority"
                ) from exc
    cluster.pop("certificate-authority", None)
    cluster.pop("insecure-skip-tls-verify", None)
    if original_insecure:
        cluster["tls-server-name"] = _discover_tls_server_name(str(cluster["server"]))
    document = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "resbench-test", "cluster": cluster}],
        "users": [
            {
                "name": service_account,
                "user": {"token": token},
            }
        ],
        "contexts": [
            {
                "name": service_account,
                "context": {
                    "cluster": "resbench-test",
                    "user": service_account,
                    "namespace": "otel-demo",
                },
            }
        ],
        "current-context": service_account,
    }
    destination = output_path.expanduser().resolve()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(destination.parent, 0o700)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            yaml.safe_dump(document, handle, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {
        "service_account": service_account,
        "namespace": namespace,
        "duration": duration,
        "path": str(destination),
        "mode": "0600",
    }


def _discover_tls_server_name(server: str) -> str:
    parsed = urlparse(server)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ScopedKubeconfigError("active API server is not a valid HTTPS URL")
    port = parsed.port or 443
    try:
        pem = ssl.get_server_certificate((parsed.hostname, port), timeout=10)
        descriptor, raw_path = tempfile.mkstemp(
            prefix="resbench-apiserver-cert-", suffix=".pem"
        )
        path = Path(raw_path)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(pem)
            decoded = ssl._ssl._test_decode_cert(str(path))  # type: ignore[attr-defined]
        finally:
            path.unlink(missing_ok=True)
    except (OSError, ssl.SSLError, ValueError) as exc:
        raise ScopedKubeconfigError("could not inspect API server certificate SAN") from exc
    alternatives = decoded.get("subjectAltName", [])
    for kind in ("DNS", "IP Address"):
        for candidate_kind, candidate in alternatives:
            if candidate_kind == kind and candidate:
                return str(candidate)
    raise ScopedKubeconfigError("API server certificate has no usable SAN")
