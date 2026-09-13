"""Request and state models for the Fleet API.

Every trial item ends up as one ``LxRunRequest`` on one Controller, so the
fields mirror that contract rather than inventing a second vocabulary. The
Controller stays the authority on what is valid; Fleet only rejects what it
can decide on its own (self-consistency, uniqueness, which replica).
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .guard import PREFIX_RE, assert_prefix


HarnessName = Literal["codex", "claude-code", "deepseek-harness", "bladeai"]
TestKind = Literal["Lx", "Dx", "Px"]
AutonomyLevel = Literal["L0", "L1", "L2", "L3", "L4"]
PromptSource = Literal["canonical", "manual"]

DX_CASES = ("D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8")
PX_CASES = ("P1", "P2")
CAPABILITY_LOSS_CASES = ("D7", "D8")
DEFAULT_DURATION_SECONDS = 300


class FleetModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SlotPhase(str, Enum):
    PROVISIONING = "Provisioning"
    READY = "Ready"
    BUSY = "Busy"
    FAILED = "Failed"
    DRAINING = "Draining"


class ItemState(str, Enum):
    QUEUED = "Queued"
    ASSIGNED = "Assigned"
    RUNNING = "Running"
    DONE = "Done"
    FAILED = "Failed"
    INVALID = "Invalid"


class FaultSlots(FleetModel):
    """The hidden execution contract every prompt variant is rendered from."""

    target: str = Field(default="cart", min_length=1, max_length=80, pattern=r"^[a-z0-9-]+$")
    fault_type: str = Field(default="cpu_load", min_length=1, max_length=80)
    fault_params: dict[str, Any] = Field(default_factory=lambda: {"cpu_percent": 80})
    duration_seconds: int = Field(default=DEFAULT_DURATION_SECONDS, ge=1, le=1200)


class ResourceRequests(FleetModel):
    cpu: str = "1"
    memory: str = "2Gi"


class ResourceLimits(FleetModel):
    cpu: str = "4"
    memory: str = "8Gi"


class SlotResources(FleetModel):
    requests: ResourceRequests = Field(default_factory=ResourceRequests)
    limits: ResourceLimits = Field(default_factory=ResourceLimits)
    storage: str = "8Gi"
    storage_class: str = "openebs-hostpath"


class FleetConfig(FleetModel):
    """Human-set fleet shape. This is the "how many Controllers" entry point."""

    schema_version: Literal["fleet-config.v1"] = "fleet-config.v1"
    replicas: int = Field(default=5, ge=1, le=999)
    namespace_prefix: str = Field(default="otel-demo", pattern=PREFIX_RE.pattern)
    control_namespace: str = "resiliencebenchmark-system"
    controller_image: str = Field(min_length=1)
    agent_image: str = Field(min_length=1)
    litellm_image: str = Field(min_length=1)
    # Replica slots can mount a paced routing table of their own, leaving the
    # single-system Controller's gateway settings untouched.
    litellm_config_map: str = "litellm-config"
    sut_values_profile: str = "replica"
    sut_application: str = "otel-demo"
    gateway_url: str = "http://127.0.0.1:4000/v1"
    coroot_project_id: str = ""
    coroot_allow_anonymous_read: bool = True
    resources: SlotResources = Field(default_factory=SlotResources)
    node_spread: bool = True
    nodes: tuple[str, ...] = ()
    # Addresses of the Kubernetes API server, for the replica NetworkPolicy.
    # The provisioner reads them from the cluster when this is empty.
    api_server_endpoints: tuple[str, ...] = ()
    source_head: str = "unknown"
    max_concurrency: int = Field(default=5, ge=1, le=999)

    @field_validator("namespace_prefix")
    @classmethod
    def validate_prefix(cls, value: str) -> str:
        return assert_prefix(value)

    @model_validator(mode="after")
    def validate_concurrency(self) -> "FleetConfig":
        if self.max_concurrency > self.replicas:
            raise ValueError("max_concurrency cannot exceed replicas")
        return self


class SlotRequest(FleetModel):
    index: int = Field(ge=1, le=999)


class BatchDefaults(FleetModel):
    model: str = "qwen3.8-max"
    llm_tag: str | None = None
    duration_seconds: int = Field(default=DEFAULT_DURATION_SECONDS, ge=1, le=1200)
    prompt_source: PromptSource = "canonical"
    repetitions: int = Field(default=1, ge=1, le=20)
    slots: FaultSlots = Field(default_factory=FaultSlots)
    note: str | None = Field(default=None, max_length=500)


class BatchItem(FleetModel):
    item_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    namespace: str | None = None
    test_kind: TestKind
    autonomy_level: AutonomyLevel
    case: str = Field(min_length=1, max_length=8)
    tool_substitution_variant: Literal["A", "B"] | None = None
    harness: HarnessName
    model: str | None = None
    llm_tag: str | None = None
    duration_seconds: int | None = Field(default=None, ge=1, le=1200)
    prompt: str | None = Field(default=None, max_length=12000)
    prompt_source: PromptSource | None = None
    repetition: int = Field(default=1, ge=1, le=20)
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_self_consistency(self) -> "BatchItem":
        """The label a human reads must agree with what actually runs."""
        case = self.case
        if self.test_kind == "Lx":
            if case != "C0":
                raise ValueError("test_kind Lx runs case C0")
        elif self.test_kind == "Dx":
            if case not in DX_CASES:
                raise ValueError(f"test_kind Dx needs a case in {', '.join(DX_CASES)}")
            if self.autonomy_level != "L0":
                raise ValueError("Dx disturbs the trial, so it pairs with the complete L0 prompt")
        elif self.test_kind == "Px":
            if case not in PX_CASES:
                raise ValueError(f"test_kind Px needs a case in {', '.join(PX_CASES)}")
            if self.autonomy_level != "L0":
                raise ValueError("Px varies the prompt, so it pairs with the complete L0 prompt")
        if case in CAPABILITY_LOSS_CASES and self.tool_substitution_variant is None:
            raise ValueError(f"case {case} requires tool_substitution_variant A or B")
        if case not in CAPABILITY_LOSS_CASES and self.tool_substitution_variant is not None:
            raise ValueError("tool_substitution_variant is only valid for D7/D8")
        if self.prompt_source == "manual" and not (self.prompt or "").strip():
            raise ValueError("prompt_source manual requires a prompt")
        if self.prompt is not None and self.prompt_source == "canonical":
            raise ValueError("a supplied prompt is prompt_source manual, not canonical")
        return self

    @property
    def cell(self) -> tuple[str, str, str, str, int]:
        """(case, level, harness, model, repetition) uniqueness key."""
        return (self.case, self.autonomy_level, self.harness, self.model or "", self.repetition)


class BatchRequest(FleetModel):
    schema_version: Literal["fleet-batch.v1"] = "fleet-batch.v1"
    batch_id: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    cluster: str = Field(default="unspecified", max_length=80)
    max_concurrency: int | None = Field(default=None, ge=1, le=999)
    platform_retry_limit: int = Field(default=2, ge=0, le=10)
    defaults: BatchDefaults = Field(default_factory=BatchDefaults)
    items: list[BatchItem] = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_items(self) -> "BatchRequest":
        seen_ids: set[str] = set()
        seen_cells: set[tuple[str, str, str, str, int]] = set()
        for item in self.items:
            if item.item_id in seen_ids:
                raise ValueError(f"duplicate item_id: {item.item_id}")
            seen_ids.add(item.item_id)
            model = item.model or self.defaults.model
            cell = (item.case, item.autonomy_level, item.harness, model, item.repetition)
            if cell in seen_cells:
                raise ValueError(
                    "duplicate (case, autonomy_level, harness, model, repetition): "
                    + ", ".join(str(part) for part in cell)
                )
            seen_cells.add(cell)
        return self

    def resolved(self, item: BatchItem) -> dict[str, Any]:
        """The item with batch defaults filled in."""
        prompt_source = item.prompt_source or ("manual" if item.prompt else self.defaults.prompt_source)
        model = item.model or self.defaults.model
        return {
            "item_id": item.item_id,
            "namespace": item.namespace,
            "test_kind": item.test_kind,
            "autonomy_level": item.autonomy_level,
            "case": item.case,
            "tool_substitution_variant": item.tool_substitution_variant,
            "harness": item.harness,
            "model": model,
            "llm_tag": item.llm_tag or self.defaults.llm_tag or model,
            "duration_seconds": item.duration_seconds or self.defaults.duration_seconds,
            "prompt": item.prompt,
            "prompt_source": prompt_source,
            "repetition": item.repetition,
            "note": item.note or self.defaults.note,
            "slots": self.defaults.slots.model_dump(mode="json"),
        }


class StopRequest(FleetModel):
    reason: str = Field(default="operator stop requested", max_length=500)
