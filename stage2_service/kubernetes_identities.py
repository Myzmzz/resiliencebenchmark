"""Controller-private Kubernetes identities for fault creation and cleanup.

The Pod's projected, rotating credential authenticates the trusted Controller.
Kubernetes authorizes delegation to two fixed ServiceAccounts; their own RBAC
then authorizes each request. Neither kubeconfig belongs in the Agent mount.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import urlsplit

import yaml


CONTROLLER_SERVICE_ACCOUNT = "resbench-stage2-controller"
EXECUTOR_SERVICE_ACCOUNT = "resbench-stage2-executor"
FINALIZER_SERVICE_ACCOUNT = "resbench-stage2-finalizer"
_NAMESPACE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class KubernetesIdentityError(RuntimeError):
    """A deployment-owned credential cannot establish the required identities."""


@dataclass(frozen=True)
class ExecutionIdentities:
    executor_kubeconfig: Path
    finalizer_kubeconfig: Path
    executor_user: str
    finalizer_user: str


def prepare_execution_identities(
    controller_kubeconfig: Path, output_dir: Path, *, control_namespace: str,
) -> ExecutionIdentities:
    """Write two fixed impersonating configurations without copying tokens.

    This does not grant RBAC or claim live authorization; the API server must
    allow both delegation and the operation. Missing configuration is fatal.
    """
    if not _NAMESPACE.fullmatch(control_namespace):
        raise KubernetesIdentityError("control namespace is invalid")
    _assert_private_file(controller_kubeconfig)
    try:
        document = yaml.safe_load(controller_kubeconfig.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError()
        clusters, users, contexts = (document[key] for key in ("clusters", "users", "contexts"))
        if any(not isinstance(rows, list) or len(rows) != 1 for rows in (clusters, users, contexts)):
            raise ValueError()
        context = contexts[0]["context"]
        if (document["current-context"] != contexts[0]["name"]
                or context["user"] != users[0]["name"] or context["cluster"] != clusters[0]["name"]):
            raise ValueError()
        auth = users[0]["user"]
        if set(auth) != {"tokenFile"}:
            raise ValueError()
        token_path = Path(auth["tokenFile"])
        if not token_path.is_absolute() or not token_path.is_file():
            raise ValueError()
        cluster = clusters[0]["cluster"]
        endpoint = urlsplit(cluster["server"])
        if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password
                or endpoint.query or endpoint.fragment or cluster.get("insecure-skip-tls-verify")
                or cluster.get("proxy-url")
                or not (cluster.get("certificate-authority-data") or cluster.get("certificate-authority"))):
            raise ValueError()
    except (KeyError, TypeError, ValueError, OSError, yaml.YAMLError) as exc:
        raise KubernetesIdentityError("Controller kubeconfig must use one verified cluster and projected tokenFile") from exc

    if (not output_dir.is_absolute() or output_dir.is_symlink()
            or output_dir.resolve() in {Path("/"), Path.home().resolve()}):
        raise KubernetesIdentityError("identity directory must be an absolute non-symlink path")
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = output_dir.stat()
    if metadata.st_uid != os.geteuid() or not stat.S_ISDIR(metadata.st_mode):
        raise KubernetesIdentityError("identity directory is not Controller-owned")
    output_dir.chmod(0o700)
    paths = []
    identities = []
    for role, account in (("executor", EXECUTOR_SERVICE_ACCOUNT), ("finalizer", FINALIZER_SERVICE_ACCOUNT)):
        identity = f"system:serviceaccount:{control_namespace}:{account}"
        configured = deepcopy(document)
        configured["users"][0]["user"]["as"] = identity
        destination = output_dir / f"{role}.kubeconfig"
        if destination.exists() or destination.is_symlink():
            _assert_private_file(destination)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{role}-", dir=output_dir)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                yaml.safe_dump(configured, handle, sort_keys=False)
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
        paths.append(destination)
        identities.append(identity)
    return ExecutionIdentities(paths[0], paths[1], identities[0], identities[1])


def _assert_private_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise KubernetesIdentityError("Controller identity file is unavailable") from exc
    if (not path.is_absolute() or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077):
        raise KubernetesIdentityError("Controller identity file must be private, regular and owned by Controller")
