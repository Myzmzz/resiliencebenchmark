from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_CATALOG = REPO_ROOT / "tasks/catalog/otel-demo-harness-prompts.v1.yaml"
CANDIDATE_SET = REPO_ROOT / "tasks/catalog/otel-demo-static-candidates.v1.yaml"
EXPECTED_IDS = [f"OTEL-C{index:02d}" for index in range(1, 7)]


def load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_six_selected_candidates_have_direct_natural_language_prompts() -> None:
    catalog = load_yaml(PROMPT_CATALOG)
    candidate_set = load_yaml(CANDIDATE_SET)

    assert catalog["schema_version"] == "otel-demo-harness-prompt-catalog.v1"
    assert catalog["interaction_mode"] == "direct_natural_language"
    assert catalog["status"] == "ready_for_manual_harness_interaction"
    assert candidate_set["harness_prompt_catalog"] == (
        "tasks/catalog/otel-demo-harness-prompts.v1.yaml"
    )
    assert [item["candidate_id"] for item in catalog["entries"]] == EXPECTED_IDS
    assert [item["candidate_id"] for item in candidate_set["candidates"]] == EXPECTED_IDS

    prompt_root = (REPO_ROOT / catalog["prompt_root"]).resolve()
    for item in catalog["entries"]:
        path = (REPO_ROOT / item["prompt_file"]).resolve()
        path.relative_to(prompt_root)
        prompt = path.read_text(encoding="utf-8").strip()
        assert len(prompt) >= 600
        assert prompt.startswith("请在 OTel Demo 2.2.0 中验证")
        assert all(tool in prompt for tool in ("k8s_ro", "telemetry_ro", "source_ro", "chaos_control"))
        assert "最后用自然语言" in prompt
        assert "run_harness_trial.py" not in prompt
        assert "--execute" not in prompt
        assert "风险：" not in prompt
        assert "后果：" not in prompt
        assert "RESBENCH_MCP_TOKEN" not in prompt


def test_prompt_catalog_contains_no_shell_or_secret_material() -> None:
    rendered = PROMPT_CATALOG.read_text(encoding="utf-8")

    assert "shell_command" not in rendered
    assert "harness_entrypoint" not in rendered
    assert "RESBENCH_" not in rendered
    assert "http://" not in rendered
    assert "https://" not in rendered
