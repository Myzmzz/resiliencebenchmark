#!/usr/bin/env python3
"""Verify API-server identity and RBAC for Stage2; never create a fault."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from stage2_service.target_binding import current as current_target_binding
from stage2_service.kubernetes_identities import (
    CONTROLLER_SERVICE_ACCOUNT, EXECUTOR_SERVICE_ACCOUNT, FINALIZER_SERVICE_ACCOUNT,
)


def qualify_identities(
    configs: dict[str, Path], *, control_namespace: str,
    application_namespace: str | None = None, runner=subprocess.run,
) -> dict[str, Any]:
    """Only SelfSubjectReview and SelfSubjectAccessReview requests are used."""
    application_namespace = application_namespace or current_target_binding().application_namespace
    accounts = {"controller": CONTROLLER_SERVICE_ACCOUNT, "executor": EXECUTOR_SERVICE_ACCOUNT, "finalizer": FINALIZER_SERVICE_ACCOUNT}
    checks = []
    for role, account in accounts.items():
        prefix = ["kubectl", "--kubeconfig", str(configs[role])]
        try:
            response = runner([*prefix, "auth", "whoami", "-o", "json"], capture_output=True, text=True, check=False, timeout=10)
            data = json.loads(response.stdout) if response.returncode == 0 else {}
            user = data.get("status", {}).get("userInfo", {}).get("username")
        except (OSError, subprocess.TimeoutExpired, ValueError):
            user = None
        expected_user = f"system:serviceaccount:{control_namespace}:{account}"
        identified = user == expected_user
        checks.append({"role": role, "check": "authenticated_identity", "expected": expected_user,
                       "observed": user, "passed": identified})
        if not identified:
            # Do not mistake transport/authentication failure for a valid 'no'.
            continue
        for resource in ("chaosblades.chaosblade.io", "networkchaos.chaos-mesh.org", "podchaos.chaos-mesh.org", "stresschaos.chaos-mesh.org"):
            for verb in ("get", "list", "create", "delete", "patch"):
                expected = (verb in {"get", "list"}
                            or (role == "executor" and verb == "create")
                            or (role == "finalizer" and verb in {"delete", "patch"}))
                scope = ["--namespace", application_namespace] if resource.endswith("chaos-mesh.org") else []
                checks.append(_permission_check(role, prefix, verb, resource, scope, expected, runner))
        if role == "controller":
            for target, expected in ((EXECUTOR_SERVICE_ACCOUNT, True), (FINALIZER_SERVICE_ACCOUNT, True), ("unrelated-service-account", False)):
                checks.append(_permission_check(role, prefix, "impersonate", f"serviceaccounts/{target}",
                                                ["--namespace", control_namespace], expected, runner))
        else:
            for verb in ("get", "list", "patch"):
                checks.append(_permission_check(role, prefix, verb, "pods", ["--namespace", application_namespace], True, runner))
            checks.append(_permission_check(role, prefix, "get", "pods", ["--namespace", control_namespace], True, runner))
            checks.append(_permission_check(role, prefix, "patch", "pods", ["--namespace", "kube-system"], False, runner))
            for verb in ("create", "delete"):
                checks.append(_permission_check(role, prefix, verb, "networkchaos.chaos-mesh.org", ["--namespace", "kube-system"], False, runner))
    return {
        "schema_version": "stage2-kubernetes-identity-qualification.v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "status": "passed" if checks and all(item["passed"] for item in checks) else "failed",
        "checks": checks,
        "fault_mutations_performed": False,
        "scope": "identity_and_rbac_only; executor canaries and Agent isolation require separate verification",
        "trust_boundary": "Controller is the trusted authority; it delegates restricted operation identities. This does not isolate a malicious Controller from its own administrative privileges.",
    }


def _permission_check(role, prefix, verb, resource, scope, expected, runner):
    observed = None
    try:
        result = runner([*prefix, "auth", "can-i", verb, resource, *scope], capture_output=True, text=True, check=False, timeout=10)
        text = result.stdout.strip().lower()
        if result.returncode == 0 and text == "yes":
            observed = True
        elif result.returncode == 1 and text == "no":
            observed = False
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {"role": role, "check": "authorization", "verb": verb, "resource": resource,
            "scope": scope, "expected": expected, "observed": observed,
            "passed": observed is not None and observed is expected}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("controller", "executor", "finalizer"):
        parser.add_argument(f"--{name}-kubeconfig", type=Path, required=True)
    parser.add_argument("--control-namespace", default="resiliencebenchmark-system")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    configs = {role: getattr(args, f"{role}_kubeconfig").resolve() for role in ("controller", "executor", "finalizer")}
    if not all(path.is_file() for path in configs.values()):
        parser.error("all three kubeconfig files must exist")
    if args.output.exists() or args.output.is_symlink():
        parser.error("refusing to overwrite existing qualification evidence")
    result = qualify_identities(configs, control_namespace=args.control_namespace)
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"status": result["status"], "checks": len(result["checks"]), "output": str(args.output)}, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
