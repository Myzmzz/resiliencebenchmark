from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_harness_runtimes_are_environment_provided() -> None:
    registry = yaml.safe_load(
        (REPO_ROOT / "harness/harnesses.yaml").read_text(encoding="utf-8")
    )
    deepseek = registry["harnesses"]["deepseek-harness"]

    assert deepseek["entrypoint"]["command"] == "dsh"
    assert "version_pin" not in deepseek
    assert not (REPO_ROOT / "scripts/deploy_deepseek_harness.py").exists()
    assert not (REPO_ROOT / "harness/deepseek-harness/install.sh").exists()
    assert not any((REPO_ROOT / "harness/deepseek-harness/runtime-lock").glob("*"))


def test_stage2_image_does_not_install_evaluated_harnesses() -> None:
    dockerfile = (REPO_ROOT / "deploy/stage2/Dockerfile").read_text(encoding="utf-8")

    assert "@openai/codex" not in dockerfile
    assert "@anthropic-ai/claude-code" not in dockerfile
    assert "@deepseek-ai/dsh" not in dockerfile
    assert "deepseek-harness/runtime-lock" not in dockerfile
    assert "bladeai-src" not in dockerfile
    assert "/opt/blade-ai" not in dockerfile
    assert "/opt/bladeai-venv" not in dockerfile
