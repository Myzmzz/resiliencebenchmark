"""Which system under test this Controller instance is bound to.

The single-system deployment hard-codes ``otel-demo`` / ``cart``.  The replica
fleet runs one Controller per replica namespace (``otel-demo-01`` ...), so the
binding is read from the environment instead.  Every value defaults to the
historical hard-coded value: a Controller started without any of these
variables behaves exactly as before.

Convention: the application id equals the application namespace, so an Lx
request keeps carrying a single ``application`` field.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass


DEFAULT_APPLICATION = "otel-demo"
DEFAULT_APPLICATION_NAMESPACE = "otel-demo"
DEFAULT_COMPONENT = "cart"
DEFAULT_CONTROL_NAMESPACE = "resiliencebenchmark-system"

# A replica namespace is the deployment bundle's name plus a numeric suffix,
# e.g. ``otel-demo-03``. The bundle (chart, values, source snapshot) is shared.
REPLICA_SUFFIX_RE = re.compile(r"^(?P<bundle>[a-z0-9][a-z0-9-]*[a-z0-9])-(?P<index>[0-9]{1,3})$")

APPLICATION_ENV = "RESBENCH_APPLICATION"
APPLICATION_NAMESPACE_ENV = "RESBENCH_APPLICATION_NAMESPACE"
COMPONENT_ENV = "RESBENCH_APPLICATION_COMPONENT"
CONTROL_NAMESPACE_ENV = "RESBENCH_CONTROL_NAMESPACE"

_NAMESPACE_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_COMPONENT_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,78}[a-z0-9])?$")


class TargetBindingError(ValueError):
    """The environment describes an invalid or inconsistent target binding."""


@dataclass(frozen=True)
class TargetBinding:
    application: str = DEFAULT_APPLICATION
    application_namespace: str = DEFAULT_APPLICATION_NAMESPACE
    component: str = DEFAULT_COMPONENT
    control_namespace: str = DEFAULT_CONTROL_NAMESPACE

    @property
    def bundle(self) -> str:
        """The deployment bundle under ``environment/kubernetes/`` for this binding.

        ``otel-demo-03`` is a replica of the ``otel-demo`` bundle; a plain
        ``otel-demo`` is its own bundle.
        """
        match = REPLICA_SUFFIX_RE.fullmatch(self.application)
        return match.group("bundle") if match else self.application

    @property
    def replica_index(self) -> int | None:
        """The replica number, or ``None`` for a non-replica binding."""
        match = REPLICA_SUFFIX_RE.fullmatch(self.application)
        return int(match.group("index")) if match else None

    @property
    def is_default(self) -> bool:
        return (
            self.application == DEFAULT_APPLICATION
            and self.application_namespace == DEFAULT_APPLICATION_NAMESPACE
            and self.component == DEFAULT_COMPONENT
            and self.control_namespace == DEFAULT_CONTROL_NAMESPACE
        )

    @property
    def supported_bindings(self) -> frozenset[tuple[str, str]]:
        return frozenset({(self.application_namespace, self.component)})

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "application": self.application,
            "application_namespace": self.application_namespace,
            "component": self.component,
            "control_namespace": self.control_namespace,
            "bundle": self.bundle,
            "replica_index": self.replica_index,
            "is_default": self.is_default,
        }

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "TargetBinding":
        values = os.environ if env is None else env
        namespace = (values.get(APPLICATION_NAMESPACE_ENV) or "").strip() or DEFAULT_APPLICATION_NAMESPACE
        application = (values.get(APPLICATION_ENV) or "").strip() or namespace
        component = (values.get(COMPONENT_ENV) or "").strip() or DEFAULT_COMPONENT
        control = (values.get(CONTROL_NAMESPACE_ENV) or "").strip() or DEFAULT_CONTROL_NAMESPACE
        for name, value in ((APPLICATION_NAMESPACE_ENV, namespace), (CONTROL_NAMESPACE_ENV, control)):
            if not _NAMESPACE_RE.fullmatch(value):
                raise TargetBindingError(f"{name} is not a valid Kubernetes namespace: {value!r}")
        if not _COMPONENT_RE.fullmatch(component):
            raise TargetBindingError(f"{COMPONENT_ENV} is not a valid component name: {component!r}")
        if application != namespace:
            raise TargetBindingError(
                f"{APPLICATION_ENV} must equal {APPLICATION_NAMESPACE_ENV} "
                f"(application ids are namespace names): {application!r} != {namespace!r}"
            )
        return cls(
            application=application,
            application_namespace=namespace,
            component=component,
            control_namespace=control,
        )


def current(env: Mapping[str, str] | None = None) -> TargetBinding:
    """The binding described by the process environment (re-read on every call)."""
    return TargetBinding.from_env(env)


def application() -> str:
    return current().application


def application_namespace() -> str:
    return current().application_namespace


def component() -> str:
    return current().component


def control_namespace() -> str:
    return current().control_namespace


def bundle() -> str:
    return current().bundle


def supported_bindings() -> frozenset[tuple[str, str]]:
    return current().supported_bindings


def is_default() -> bool:
    return current().is_default
