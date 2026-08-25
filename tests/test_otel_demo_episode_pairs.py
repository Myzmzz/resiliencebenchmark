from __future__ import annotations

from pathlib import Path

import yaml

from resilience_agent.semantic_scan.episode_contracts import (
    InternalEpisode,
    PublicEpisodeTask,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
EPISODE_ROOT = REPO_ROOT / "tasks/episodes/otel-demo"
PRIVATE_PUBLIC_KEYS = {
    "cleanup_command",
    "cleanup_handle",
    "command_template",
    "confidence",
    "defect_basis",
    "evidence_id",
    "result_integrity",
    "oracle",
    "pod_uid",
    "residual_hypotheses",
}


def _roots() -> list[Path]:
    return sorted(path for path in EPISODE_ROOT.glob("EPI-*") if path.is_dir())


def _load(root: Path) -> tuple[InternalEpisode, PublicEpisodeTask, dict, dict]:
    internal_raw = yaml.safe_load((root / "episode-internal.yaml").read_text(encoding="utf-8"))
    public_raw = yaml.safe_load((root / "episode-public.yaml").read_text(encoding="utf-8"))
    return (
        InternalEpisode.model_validate(internal_raw),
        PublicEpisodeTask.model_validate(public_raw),
        internal_raw,
        public_raw,
    )


def test_three_otel_demo_episode_pairs_use_canonical_contracts() -> None:
    roots = _roots()
    assert [root.name for root in roots] == [
        "EPI-OTEL-CART-DEADLINE-001",
        "EPI-OTEL-CURRENCY-INSTANCE-LOSS-003",
        "EPI-OTEL-RECOMMENDATION-FALLBACK-002",
    ]
    for root in roots:
        assert sorted(path.name for path in root.glob("*.yaml")) == [
            "episode-internal.yaml",
            "episode-public.yaml",
        ]
        internal, public, _, _ = _load(root)
        assert internal.schema_version == "resilience-episode.v2"
        assert public.schema_version == "resilience-episode-public.v1"
        assert internal.identity == public.identity


def test_runtime_bindings_match_the_internal_fault_targets() -> None:
    for root in _roots():
        internal, _, _, _ = _load(root)
        target = internal.main_fault.target
        binding = internal.runtime_binding
        assert target["namespace"] == binding.namespace
        assert target["component"] == binding.component
        assert target["pod_name"] == binding.pod_name
        assert target["pod_uid"] == binding.pod_uid
        assert internal.main_fault.fault_type in internal.defect_basis.supported_fault_types
        assert internal.main_fault.duration_seconds == 600
        assert internal.execution_policy.max_experiments == 5


def test_public_episode_does_not_contain_private_fields() -> None:
    for root in _roots():
        _, _, _, public_raw = _load(root)
        found: set[str] = set()

        def visit(value: object) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if str(key) in PRIVATE_PUBLIC_KEYS:
                        found.add(str(key))
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(public_raw)
        assert found == set()


def test_internal_episode_does_not_embed_runtime_disturbances() -> None:
    for root in _roots():
        _, _, internal_raw, _ = _load(root)
        assert "disturbances" not in internal_raw
