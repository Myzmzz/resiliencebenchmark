"""Namespace guards for every operation that can destroy something.

Kubernetes RBAC cannot restrict a verb to namespaces matching a prefix, so the
prefix gate lives here and every destructive path goes through it.
"""

from __future__ import annotations

import re


class FleetGuardError(ValueError):
    """A requested namespace is outside what this Fleet may ever touch."""


# Never operable, whatever the configuration says. ``otel-demo`` is on the list
# on purpose: the full system under test shares a prefix with every replica and
# must never be provisioned, reset or deleted by the Fleet.
PROTECTED_NAMESPACES = frozenset(
    {
        "default",
        "kube-system",
        "kube-public",
        "kube-node-lease",
        "observability",
        "coroot",
        "chaos-mesh",
        "chaos-testing",
        "openebs",
        "sregym",
        "ischaos",
        "otel-demo",
        "train-ticket",
        "sock-shop",
        "resiliencebenchmark-system",
        "resilience-benchmark-system",
    }
)

NAMESPACE_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
PREFIX_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,40}[a-z0-9])?$")


def slot_id(index: int) -> str:
    """``s01`` for slot 1. Two digits below 100, three above."""
    if not isinstance(index, int) or isinstance(index, bool) or not 1 <= index <= 999:
        raise FleetGuardError("slot index must be between 1 and 999")
    return f"s{index:02d}"


def replica_namespace(prefix: str, index: int) -> str:
    """``otel-demo-03`` for prefix ``otel-demo`` and slot 3."""
    assert_prefix(prefix)
    if not isinstance(index, int) or isinstance(index, bool) or not 1 <= index <= 999:
        raise FleetGuardError("replica index must be between 1 and 999")
    return f"{prefix}-{index:02d}"


def assert_prefix(prefix: str) -> str:
    if not isinstance(prefix, str) or not PREFIX_RE.fullmatch(prefix):
        raise FleetGuardError(f"namespace prefix is not a valid name: {prefix!r}")
    return prefix


def replica_index(prefix: str, namespace: str) -> int | None:
    """The slot number of a replica namespace, or ``None`` if it is not one.

    Exact match on ``<prefix>-<digits>`` only. ``otel-demo`` is a prefix of
    ``otel-demo-01``, so a ``startswith`` test here would let the Fleet operate
    on the full system under test.
    """
    assert_prefix(prefix)
    if not isinstance(namespace, str):
        return None
    match = re.fullmatch(re.escape(prefix) + r"-(?P<index>[0-9]{1,3})", namespace)
    if match is None:
        return None
    index = int(match.group("index"))
    return index if 1 <= index <= 999 else None


def assert_operable_namespace(prefix: str, namespace: str) -> str:
    """Allow only a numbered replica namespace of this Fleet's own prefix."""
    if namespace in PROTECTED_NAMESPACES:
        raise FleetGuardError(f"refusing to operate on protected namespace {namespace}")
    if not isinstance(namespace, str) or not NAMESPACE_RE.fullmatch(namespace):
        raise FleetGuardError(f"not a valid Kubernetes namespace: {namespace!r}")
    if replica_index(prefix, namespace) is None:
        raise FleetGuardError(
            f"namespace {namespace} is not a numbered replica of prefix {prefix}; "
            "the Fleet only operates on its own replicas"
        )
    return namespace


def assert_confirmed(namespace: str, confirm: str | None) -> str:
    """A destructive call must repeat the namespace it is about to destroy."""
    if confirm != namespace:
        raise FleetGuardError(
            "destructive operations require confirm=<namespace>; "
            f"expected {namespace!r}"
        )
    return namespace


def assert_destroyable(prefix: str, namespace: str, confirm: str | None) -> str:
    assert_operable_namespace(prefix, namespace)
    return assert_confirmed(namespace, confirm)
