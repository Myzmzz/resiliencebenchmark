"""Parser for episode YAML files."""

from pathlib import Path
from typing import List

import yaml

from backend.models.episode import (
    ActionSpace,
    BudgetConfig,
    Episode,
    EpisodeApplication,
    EnvironmentSnapshot,
    ObservabilityConfig,
    SourceAccess,
    WorkloadConfig,
)


def parse_episodes(repo_path: Path) -> List[Episode]:
    """
    Parse all episode YAML files from tasks/examples/.

    Args:
        repo_path: Path to the resiliencebenchmark repository

    Returns:
        List of Episode objects
    """
    episodes_dir = repo_path / "tasks" / "examples"

    if not episodes_dir.exists():
        return []

    episodes = []

    # Search for episode YAML files recursively
    for yaml_file in episodes_dir.rglob("episode.*.yaml"):
        try:
            episode = _parse_episode_file(yaml_file)
            if episode:
                episodes.append(episode)
        except Exception as e:
            # Log error but continue processing other files
            print(f"Error parsing {yaml_file}: {e}")
            # Create error placeholder
            episodes.append(
                Episode(
                    schema_version="unknown",
                    episode_id=yaml_file.stem,
                    title=yaml_file.stem,
                    status="error",
                    application=EpisodeApplication(
                        name="unknown",
                        namespace="unknown",
                        candidate_services=[],
                        release_ref="unknown",
                    ),
                    agent_goal=f"parse_error: {str(e)}",
                    environment_snapshot=EnvironmentSnapshot(
                        snapshot_id="unknown",
                        health_prerequisites=[],
                        reset_contract=[],
                    ),
                    workload=WorkloadConfig(profile="unknown", slo=[]),
                    observability=ObservabilityConfig(
                        metrics=[], traces=[], logs=[], kubernetes=[]
                    ),
                    source_access=SourceAccess(
                        mode="unknown", allowed_paths=[], forbidden_paths=[]
                    ),
                    action_space=ActionSpace(
                        allowed_trigger_classes=[],
                        allowed_target_scope=[],
                        forbidden_actions=[],
                    ),
                    budget=BudgetConfig(
                        max_experiments=0,
                        max_duration_minutes=0,
                        max_concurrent_faults=0,
                    ),
                    safety_constraints=[],
                    expected_agent_output=[],
                    leakage_controls=[],
                )
            )

    return episodes


def _parse_episode_file(yaml_file: Path) -> Episode:
    """Parse a single episode YAML file."""
    with open(yaml_file, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not data:
        return None

    # Parse nested structures
    episode_data = {**data}

    if "application" in data:
        episode_data["application"] = EpisodeApplication(**data["application"])

    if "environment_snapshot" in data:
        episode_data["environment_snapshot"] = EnvironmentSnapshot(
            **data["environment_snapshot"]
        )

    if "workload" in data:
        episode_data["workload"] = WorkloadConfig(**data["workload"])

    if "observability" in data:
        episode_data["observability"] = ObservabilityConfig(**data["observability"])

    if "source_access" in data:
        episode_data["source_access"] = SourceAccess(**data["source_access"])

    if "action_space" in data:
        episode_data["action_space"] = ActionSpace(**data["action_space"])

    if "budget" in data:
        episode_data["budget"] = BudgetConfig(**data["budget"])

    return Episode(**episode_data)
