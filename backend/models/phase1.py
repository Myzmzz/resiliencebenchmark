"""第一阶段核心功能测试工作台的数据合同。"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Phase1Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class FeatureInputField(Phase1Model):
    name: str
    label: str
    value_type: Literal[
        "string", "integer", "boolean", "string_array", "object", "object_array"
    ] = Field(alias="valueType")
    required: bool = True
    description: str
    allowed_values: list[str] = Field(default_factory=list, alias="allowedValues")


class CoreFeature(Phase1Model):
    id: str
    group: Literal[
        "source-codegraph", "templates-defects", "kubernetes", "evidence-contracts"
    ]
    title: str
    description: str
    evidence_output: str = Field(alias="evidenceOutput")
    mutates_local_state: bool = Field(alias="mutatesLocalState")
    may_exceed_five_minutes: bool = Field(alias="mayExceedFiveMinutes")
    input_fields: list[FeatureInputField] = Field(alias="inputFields")
    example_inputs: dict[str, Any] = Field(alias="exampleInputs")


class CoreFeatureCatalog(Phase1Model):
    schema_version: Literal["phase1-core-feature-catalog.v1"] = Field(
        default="phase1-core-feature-catalog.v1", alias="schemaVersion"
    )
    features: list[CoreFeature]


class PreviewRequest(Phase1Model):
    inputs: dict[str, Any]


class CoreTestPreview(Phase1Model):
    preview_id: str = Field(alias="previewId")
    feature_id: str = Field(alias="featureId")
    created_at: datetime = Field(alias="createdAt")
    expires_at: datetime = Field(alias="expiresAt")
    normalized_inputs: dict[str, Any] = Field(alias="normalizedInputs")
    operations: list[str]
    warnings: list[str]
    confirmation_text: str = Field(alias="confirmationText")
    input_digest: str = Field(alias="inputDigest")


class StartRunRequest(Phase1Model):
    confirmed: bool
    input_digest: str = Field(alias="inputDigest")


class CoreTestLog(Phase1Model):
    sequence: int
    occurred_at: datetime = Field(alias="occurredAt")
    level: Literal["info", "warning", "error"]
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class CoreTestRun(Phase1Model):
    run_id: str = Field(alias="runId")
    feature_id: str = Field(alias="featureId")
    status: Literal["queued", "running", "passed", "failed"]
    created_at: datetime = Field(alias="createdAt")
    started_at: datetime | None = Field(default=None, alias="startedAt")
    finished_at: datetime | None = Field(default=None, alias="finishedAt")
    duration_seconds: float | None = Field(default=None, alias="durationSeconds")
    inputs: dict[str, Any]
    logs: list[CoreTestLog]
    result: Any | None = None
    error: str | None = None

