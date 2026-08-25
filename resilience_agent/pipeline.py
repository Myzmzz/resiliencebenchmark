"""Orchestration for our two internal resilience-analysis capabilities."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import load_document, sanitize_context, validate_document, write_json
from .defect_identification import identify_defects
from .episode_design import design_episodes


PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent
TEMPLATE_ROOT = PACKAGE_ROOT / "templates"
CANDIDATE_SCHEMA = PACKAGE_ROOT / "schemas" / "candidate-defects.schema.json"
EPISODE_SCHEMA = PACKAGE_ROOT / "schemas" / "episode-designs.schema.json"


def run_pipeline(
    project_root: Path,
    *,
    system_context: dict[str, Any] | None = None,
    output_dir: Path | None = None,
    catalog_path: Path | None = None,
    rules_path: Path | None = None,
    episode_templates_path: Path | None = None,
    min_confidence: float = 0.65,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Identify candidates, validate the handoff, then design Episodes."""
    candidates = identify_defects(
        project_root,
        catalog_path or REPO_ROOT / "tasks" / "catalog" / "resilience-defect-classes.v0.1.yaml",
        rules_path or TEMPLATE_ROOT / "defect-matchers.v0.1.yaml",
        system_context,
    )
    validate_document(candidates, CANDIDATE_SCHEMA)
    episodes = design_episodes(
        candidates,
        episode_templates_path or TEMPLATE_ROOT / "episode-design-templates.v0.1.yaml",
        min_confidence=min_confidence,
    )
    validate_document(episodes, EPISODE_SCHEMA)
    if output_dir is not None:
        write_json(output_dir / "candidate-defects.json", candidates)
        write_json(output_dir / "episode-designs.json", episodes)
    return candidates, episodes


def load_context(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    value = load_document(path)
    if not isinstance(value, dict):
        raise ValueError("system context must be a JSON/YAML object")
    return sanitize_context(value)
