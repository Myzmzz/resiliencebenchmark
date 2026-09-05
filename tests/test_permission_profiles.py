from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml
from pydantic import ValidationError

from stage2_service.contracts import McpServerName, PermissionProfile


REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILE_ROOT = REPO_ROOT / "harness/permission-profiles"
PROFILE_SCHEMA = json.loads(
    (REPO_ROOT / "harness/schemas/permission-profile.schema.json").read_text(
        encoding="utf-8"
    )
)


def load_profiles() -> dict[str, PermissionProfile]:
    profiles: dict[str, PermissionProfile] = {}
    for path in sorted(PROFILE_ROOT.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        jsonschema.validate(raw, PROFILE_SCHEMA)
        profile = PermissionProfile.model_validate(raw)
        profiles[profile.profile_id] = profile
    return profiles


def test_static_permission_profiles_are_valid_and_unique():
    profiles = load_profiles()

    assert set(profiles) == {
        "p0-full-authorized",
        "p1-no-telemetry-mcp",
        "p2-no-k8s-mcp",
        "p3-read-only",
        "p4-no-native-k8s-metrics",
        "p5-no-mcp",
    }
    assert profiles["p0-full-authorized"].mcp_servers == tuple(McpServerName)
    assert McpServerName.TELEMETRY_RO not in profiles["p1-no-telemetry-mcp"].mcp_servers
    assert McpServerName.K8S_RO not in profiles["p2-no-k8s-mcp"].mcp_servers
    assert profiles["p3-read-only"].bladeai_native.chaosblade_execute is False
    assert profiles["p4-no-native-k8s-metrics"].bladeai_native.kubernetes_metrics is False
    assert profiles["p5-no-mcp"].mcp_servers == ()


def test_permission_profile_rejects_duplicate_or_unknown_mcp_servers():
    with pytest.raises(ValidationError, match="mcp_servers must be unique"):
        PermissionProfile(mcp_servers=(McpServerName.K8S_RO, McpServerName.K8S_RO))

    with pytest.raises(ValidationError):
        PermissionProfile.model_validate(
            {
                "profile_id": "invalid-server",
                "mcp_servers": ["not_a_server"],
            }
        )
