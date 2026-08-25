"""Model configuration data models."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class CredentialRef(BaseModel):
    """Credential reference configuration."""

    base_url: str = Field(alias="base_url")
    api_key: str = Field(alias="api_key")
    auth_scheme: str = Field(alias="auth_scheme")


class CapabilityProbe(BaseModel):
    """Capability probe configuration."""

    enabled: Optional[bool] = None
    timeout_seconds: Optional[int] = Field(None, alias="timeout_seconds")
    isolation: Optional[str] = None
    prompt_ref: Optional[str] = Field(None, alias="prompt_ref")
    transport_checks_implemented: Optional[List[str]] = Field(
        None, alias="transport_checks_implemented"
    )
    behavioral_checks_required_before_matrix_freeze: Optional[List[str]] = Field(
        None, alias="behavioral_checks_required_before_matrix_freeze"
    )
    recorded_fields: Optional[List[str]] = Field(None, alias="recorded_fields")
    transport_acceptance: Optional[Dict[str, Any]] = Field(
        None, alias="transport_acceptance"
    )
    behavioral_acceptance_pending: Optional[Dict[str, Any]] = Field(
        None, alias="behavioral_acceptance_pending"
    )
    current_oauth_catalog: Optional[str] = Field(None, alias="current_oauth_catalog")
    transport_checks_required: Optional[List[str]] = Field(
        None, alias="transport_checks_required"
    )


class ModelConfig(BaseModel):
    """Model configuration."""

    id: str
    upstream_model: str = Field(alias="upstream_model")
    display_name: str = Field(alias="display_name")
    protocol_candidates: List[str] = Field(alias="protocol_candidates")
    credential_ref: Optional[str] = Field(None, alias="credential_ref")
    authentication_modes: Optional[List[str]] = Field(
        None, alias="authentication_modes"
    )
    capability_probe: Optional[CapabilityProbe] = Field(
        None, alias="capability_probe"
    )


class ModelsRegistry(BaseModel):
    """Models registry configuration."""

    version: str
    description: str
    credential_refs: Dict[str, CredentialRef] = Field(alias="credential_refs")
    defaults: Dict[str, Any]
    models: List[ModelConfig]

    class Config:
        populate_by_name = True
