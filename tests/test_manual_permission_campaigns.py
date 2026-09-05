from __future__ import annotations

import hashlib
import json
from pathlib import Path

from stage2_service.contracts import CampaignRequest, HarnessKind, TrialKind
from stage2_service.permission_profiles import load_permission_profiles


REPO_ROOT = Path(__file__).resolve().parents[1]
REQUEST_ROOT = REPO_ROOT / "harness/manual-campaigns"
EXPECTED_PROFILES = (
    "p0-full-authorized",
    "p1-no-telemetry-mcp",
    "p2-no-k8s-mcp",
    "p3-read-only",
    "p4-no-native-k8s-metrics",
    "p5-no-mcp",
)
EXPECTED_MODELS = {
    HarnessKind.BLADEAI: ("gpt-5.6-sol",),
    HarnessKind.CODEX: ("gpt-5.6-sol",),
    HarnessKind.CLAUDE_CODE: ("claude-opus-5",),
    HarnessKind.DEEPSEEK: ("deepseek-v4-pro",),
}


def test_manual_permission_campaigns_are_directly_usable_and_ordered() -> None:
    paths = sorted(REQUEST_ROOT.glob("[0-9][0-9]-*.json"))
    requests = [
        CampaignRequest.model_validate(json.loads(path.read_text(encoding="utf-8")))
        for path in paths
    ]

    assert len(requests) == len(EXPECTED_PROFILES)
    assert tuple(item.permission_profile.profile_id for item in requests) == (
        EXPECTED_PROFILES
    )
    assert len({item.request_id for item in requests}) == len(requests)
    assert all(item.trial_kinds == (TrialKind.CONTROL,) for item in requests)
    assert all(
        {selection.harness: selection.models for selection in item.model_matrix}
        == EXPECTED_MODELS
        for item in requests
    )

    built_ins = {item.profile_id: item for item in load_permission_profiles()}
    assert all(
        item.permission_profile == built_ins[item.permission_profile.profile_id]
        for item in requests
    )


def test_manual_permission_campaign_episode_references_match_files() -> None:
    for path in sorted(REQUEST_ROOT.glob("[0-9][0-9]-*.json")):
        request = CampaignRequest.model_validate_json(path.read_text(encoding="utf-8"))
        internal = REPO_ROOT / request.episode.internal_path
        public = REPO_ROOT / request.episode.public_path

        assert hashlib.sha256(internal.read_bytes()).hexdigest() == (
            request.episode.internal_sha256
        )
        assert hashlib.sha256(public.read_bytes()).hexdigest() == (
            request.episode.public_sha256
        )
