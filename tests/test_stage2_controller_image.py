"""Qualification commands must ship in the Controller that runs them."""

from pathlib import Path
import shlex

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "script", ("qualify_agent_channel.py", "qualify_execution_identities.py")
)
def test_controller_image_includes_and_checks_qualification_entrypoint(script: str) -> None:
    dockerfile = (ROOT / "deploy/stage2/Dockerfile.runtime-overlay").read_text()
    assert (ROOT / "scripts" / script).is_file()
    assert f"COPY --chown=10001:10001 scripts/{script} /app/scripts/{script}" in dockerfile
    assert f"/app/.venv/bin/python /app/scripts/{script} --help" in dockerfile


@pytest.mark.parametrize("filename", ("Dockerfile.agent", "Dockerfile.runtime-overlay"))
def test_runtime_image_shell_instructions_have_balanced_quoting(filename: str) -> None:
    dockerfile = (ROOT / "deploy/stage2" / filename).read_text()
    for instruction in dockerfile.replace("\\\n", " ").splitlines():
        if instruction.startswith("RUN "):
            # Parse the actual build instruction, not a separately rewritten
            # test command. The image build remains the runtime verification.
            assert shlex.split(instruction[4:]), instruction
