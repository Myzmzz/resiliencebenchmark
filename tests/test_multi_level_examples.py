import json
from pathlib import Path

import jsonschema
import yaml

from progression.builder import validate_multi_level_episode
from scoring.calculator import calculate_episode_score


REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_ROOT = REPO_ROOT / "artifacts/multi-level-run"


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_three_level_task_and_artifact_snapshot_validate():
    schema = load_json(REPO_ROOT / "tasks/schemas/multi-level-episode.schema.json")
    task_example = yaml.safe_load(
        (REPO_ROOT / "tasks/examples/multi-level/episode.3-levels.yaml").read_text()
    )
    artifact_episode = yaml.safe_load((ARTIFACT_ROOT / "episode.yaml").read_text())

    for episode in (task_example, artifact_episode):
        jsonschema.validate(episode, schema)
        validate_multi_level_episode(episode)
    assert task_example == artifact_episode


def test_controller_record_and_level_results_validate():
    disturbance_schema = load_json(
        REPO_ROOT / "disturbances/schemas/disturbance-event.schema.json"
    )
    records = [
        json.loads(line)
        for line in (ARTIFACT_ROOT / "controller-record.jsonl").read_text().splitlines()
        if line.strip()
    ]
    for record in records:
        jsonschema.validate(record, disturbance_schema)
    assert len(records) == 9
    assert {item["trial_id"] for item in records} == {
        "run-example-L2-a1",
        "run-example-L2-a2",
        "run-example-L3-a1",
    }

    level_schema = load_json(REPO_ROOT / "evaluator/schemas/level-result.schema.json")
    level_results = []
    for path in sorted((ARTIFACT_ROOT / "level-results").glob("*.json")):
        value = load_json(path)
        jsonschema.validate(value, level_schema)
        level_results.append(value)
    assert len(level_results) == 4


def test_artifact_score_is_reproducible_and_all_references_exist():
    level_results = [
        load_json(path)
        for path in sorted((ARTIFACT_ROOT / "level-results").glob("*.json"))
    ]
    expected = calculate_episode_score(
        episode_id="EPI-MULTI-NETWORK-001",
        agent_id="example-agent",
        level_results=level_results,
        total_levels=3,
        efficiency_score=0.8,
    )
    stored = load_json(ARTIFACT_ROOT / "episode-score.json")

    assert stored == expected
    jsonschema.validate(
        stored,
        load_json(REPO_ROOT / "scoring/schemas/episode-score.schema.json"),
    )
    summary = load_json(ARTIFACT_ROOT / "run-summary.json")
    refs = [
        *summary["level_result_refs"],
        summary["controller_record_ref"],
        summary["progression_state_ref"],
        summary["episode_score_ref"],
    ]
    assert all((ARTIFACT_ROOT / ref).is_file() for ref in refs)
    assert summary["empirical_status"] == "not_run_against_live_cluster"
