from pathlib import Path


def test_bladeai_agent_image_copies_worker_local_imports() -> None:
    dockerfile = (
        Path(__file__).resolve().parents[1] / "deploy" / "stage2" / "Dockerfile.agent"
    ).read_text(encoding="utf-8")
    required = {
        "stage2_service/bladeai_worker.py",
        "stage2_service/bladeai_task.py",
        "stage2_service/condition_policy.py",
    }
    copied = {
        line.split()[1]
        for line in dockerfile.splitlines()
        if line.startswith("COPY stage2_service/")
    }
    assert required <= copied
