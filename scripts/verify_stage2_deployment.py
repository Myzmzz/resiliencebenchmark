#!/usr/bin/env python3
"""Check the three settings a Stage-2 redeploy is known to silently lose.

The new-environment operations manual and the Dx remediation notes both say the
same thing after every image change: go and check three things by hand. They are
checked by hand because none of them is in the rendered manifests --
``fsGroupChangePolicy`` is in no repository file at all, and the two Coroot
variables carry a per-cluster value that the repository copy gets wrong.

Losing any of them is silent at deploy time and only shows up later: without
``fsGroupChangePolicy: OnRootMismatch`` the kubelet re-chowns the evidence volume
on every mount, the Controller's private files become group-readable, and the
next run either hangs in QUEUED or fails with ``KubernetesIdentityError``.

This turns that checklist into something a deploy step can run. It reads only;
it never writes to the cluster.

    python scripts/verify_stage2_deployment.py --coroot-project p1nar0hw
    python scripts/verify_stage2_deployment.py --manifest rendered.yaml --coroot-project p1nar0hw
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


DEFAULT_NAMESPACE = "resiliencebenchmark-system"
DEFAULT_DEPLOYMENT = "resbench-stage2-integration"
REQUIRED_CONTAINERS = ("litellm", "stage2", "agent-runtime")
PRIVATE_ROOT = "/var/lib/resbench-stage2/integration"
EXPECTED_FS_GROUP = 10001
EXPECTED_FS_GROUP_CHANGE_POLICY = "OnRootMismatch"
COROOT_PROJECT_ENV = "RESBENCH_COROOT_PROJECT_ID"
COROOT_ANONYMOUS_ENV = "RESBENCH_COROOT_ALLOW_ANONYMOUS_READ"


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


def check_fs_group(spec: Mapping[str, Any]) -> list[Check]:
    """The setting that is in no repository manifest and breaks runs when lost."""
    context = spec.get("securityContext")
    if not isinstance(context, Mapping):
        return [
            Check("fsGroupChangePolicy", False, "Pod has no securityContext at all"),
            Check("fsGroup", False, "Pod has no securityContext at all"),
        ]
    policy = context.get("fsGroupChangePolicy")
    group = context.get("fsGroup")
    return [
        Check(
            "fsGroupChangePolicy",
            policy == EXPECTED_FS_GROUP_CHANGE_POLICY,
            f"is {policy!r}, expected {EXPECTED_FS_GROUP_CHANGE_POLICY!r}"
            if policy != EXPECTED_FS_GROUP_CHANGE_POLICY
            else f"{EXPECTED_FS_GROUP_CHANGE_POLICY} (private files survive a remount)",
        ),
        Check(
            "fsGroup",
            group == EXPECTED_FS_GROUP,
            f"is {group!r}, expected {EXPECTED_FS_GROUP}"
            if group != EXPECTED_FS_GROUP
            else str(EXPECTED_FS_GROUP),
        ),
    ]


def check_coroot_environment(
    spec: Mapping[str, Any], *, expected_project: str | None
) -> list[Check]:
    """Both variables must be present, and the project id is per-cluster."""
    container = _container(spec, "stage2")
    if container is None:
        return [Check("coroot-env", False, "the stage2 container is not in this Pod")]
    env = {
        str(item.get("name")): item.get("value")
        for item in container.get("env", [])
        if isinstance(item, Mapping) and item.get("name")
    }
    checks: list[Check] = []

    project = env.get(COROOT_PROJECT_ENV)
    if project is None:
        checks.append(Check(COROOT_PROJECT_ENV, False, "not set"))
    elif expected_project is None:
        checks.append(
            Check(
                COROOT_PROJECT_ENV,
                True,
                f"{project} (not compared; pass --coroot-project to pin it)",
            )
        )
    else:
        checks.append(
            Check(
                COROOT_PROJECT_ENV,
                project == expected_project,
                f"is {project!r}, expected {expected_project!r} for this cluster"
                if project != expected_project
                else str(project),
            )
        )

    anonymous = env.get(COROOT_ANONYMOUS_ENV)
    checks.append(
        Check(
            COROOT_ANONYMOUS_ENV,
            str(anonymous).lower() == "true",
            f"is {anonymous!r}, expected \"true\""
            if str(anonymous).lower() != "true"
            else "true",
        )
    )
    return checks


def check_containers(spec: Mapping[str, Any]) -> list[Check]:
    present = {str(item.get("name")) for item in spec.get("containers", []) if isinstance(item, Mapping)}
    missing = [name for name in REQUIRED_CONTAINERS if name not in present]
    return [
        Check(
            "containers",
            not missing,
            "missing " + ", ".join(missing) if missing else ", ".join(REQUIRED_CONTAINERS),
        )
    ]


def check_node_placement(spec: Mapping[str, Any], *, required: bool) -> list[Check]:
    """A cluster where only some nodes carry the AppArmor profile needs this."""
    selector = spec.get("nodeSelector")
    has = isinstance(selector, Mapping) and bool(selector)
    if not required:
        return [
            Check(
                "nodeSelector",
                True,
                json.dumps(selector, sort_keys=True) if has else "none (not required)",
            )
        ]
    return [
        Check(
            "nodeSelector",
            has,
            json.dumps(selector, sort_keys=True)
            if has
            else "absent, but this cluster pins the workload to an AppArmor-enabled node",
        )
    ]


def check_private_file_modes(listing: str) -> list[Check]:
    """Reject any private file that group or other can read or write.

    ``listing`` is the output of ``find <private root> -type f -printf '%m %p\\n'``
    or the ``ls -l`` equivalent produced inside the Controller container.
    """
    offenders: list[str] = []
    checked = 0
    for line in listing.splitlines():
        line = line.strip()
        if not line:
            continue
        mode, _, path = line.partition(" ")
        if not mode.isdigit() or not path:
            continue
        checked += 1
        if int(mode, 8) & 0o077:
            offenders.append(f"{path} ({mode})")
    if not checked:
        return [Check("private-file-modes", False, "no files were listed; the check did not run")]
    return [
        Check(
            "private-file-modes",
            not offenders,
            "group/other-accessible: " + ", ".join(offenders[:5])
            if offenders
            else f"{checked} files, none group- or other-accessible",
        )
    ]


def _container(spec: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    for item in spec.get("containers", []):
        if isinstance(item, Mapping) and item.get("name") == name:
            return item
    return None


def pod_spec_from_deployment(document: Mapping[str, Any]) -> Mapping[str, Any]:
    if document.get("kind") == "Pod":
        spec = document.get("spec")
        return spec if isinstance(spec, Mapping) else {}
    spec = (((document.get("spec") or {}).get("template") or {}).get("spec")) or {}
    return spec if isinstance(spec, Mapping) else {}


def run_checks(
    document: Mapping[str, Any],
    *,
    expected_project: str | None,
    require_node_selector: bool,
    private_listing: str | None,
) -> list[Check]:
    spec = pod_spec_from_deployment(document)
    checks = [
        *check_containers(spec),
        *check_fs_group(spec),
        *check_coroot_environment(spec, expected_project=expected_project),
        *check_node_placement(spec, required=require_node_selector),
    ]
    if private_listing is not None:
        checks.extend(check_private_file_modes(private_listing))
    return checks


def _kubectl_json(argv: Sequence[str]) -> Mapping[str, Any]:
    completed = subprocess.run(
        list(argv), check=False, capture_output=True, text=True, shell=False
    )
    if completed.returncode:
        raise RuntimeError(
            f"kubectl failed: {(completed.stderr or completed.stdout).strip()[:300]}"
        )
    return json.loads(completed.stdout)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, help="read a rendered manifest instead of the cluster")
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument("--deployment", default=DEFAULT_DEPLOYMENT)
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument(
        "--coroot-project",
        help="the Coroot project id this cluster uses; the repository copy is another cluster's",
    )
    parser.add_argument(
        "--require-node-selector",
        action="store_true",
        help="fail when the workload is not pinned (clusters where only some nodes carry the AppArmor profile)",
    )
    parser.add_argument(
        "--private-listing",
        type=Path,
        help="a 'mode path' listing of the Controller private root, captured inside the container",
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.manifest is not None:
        import yaml

        document = yaml.safe_load(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(document, Mapping):
            print("manifest is not a mapping", file=sys.stderr)
            return 2
    else:
        command = ["kubectl"]
        if args.kubeconfig:
            command += ["--kubeconfig", str(args.kubeconfig)]
        command += ["-n", args.namespace, "get", "deploy", args.deployment, "-o", "json"]
        try:
            document = _kubectl_json(command)
        except (RuntimeError, ValueError) as exc:
            print(f"could not read the deployment: {exc}", file=sys.stderr)
            return 2

    listing = (
        args.private_listing.read_text(encoding="utf-8")
        if args.private_listing is not None
        else None
    )
    checks = run_checks(
        document,
        expected_project=args.coroot_project,
        require_node_selector=args.require_node_selector,
        private_listing=listing,
    )
    failed = [check for check in checks if not check.passed]

    if args.json:
        print(
            json.dumps(
                {
                    "passed": not failed,
                    "checks": [check.as_dict() for check in checks],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for check in checks:
            print(f"{'ok  ' if check.passed else 'FAIL'}  {check.name}: {check.detail}")
        if listing is None:
            print(
                "note  private-file-modes: not checked; capture a listing inside the "
                f"stage2 container under {PRIVATE_ROOT} and pass --private-listing"
            )
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
