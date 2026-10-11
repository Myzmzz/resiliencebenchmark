"""Static contract for the ``stage2_service`` files copied into the Agent image.

BladeAI 0.7.0 runs as a black-box ``blade-ai server`` container, so the
in-process worker modules (``bladeai_worker``, ``bladeai_task``, ...) were
deleted.  The image may only COPY ``stage2_service`` files that still exist,
otherwise ``docker build`` fails on the COPY step.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_bladeai_agent_image_copies_only_existing_stage2_service_files() -> None:
    dockerfile = (ROOT / "deploy" / "stage2" / "Dockerfile.agent").read_text(encoding="utf-8")
    copied = {
        line.split()[1]
        for line in dockerfile.splitlines()
        if line.startswith("COPY stage2_service/")
    }
    assert "stage2_service/condition_policy.py" in copied
    missing_sources = sorted(source for source in copied if not (ROOT / source).is_file())
    assert missing_sources == []
