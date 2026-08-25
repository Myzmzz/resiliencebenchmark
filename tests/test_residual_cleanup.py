from __future__ import annotations

import json
from pathlib import Path

from controller.residual_cleanup import cleanup_run_workloads
from scripts.reset_episode import CommandResult


class Runner:
    def __init__(self):
        self.calls = []
        self.list_count = 0

    def run(self, argv, *, stdin=None, timeout=300):
        self.calls.append(argv)
        if "get" in argv:
            self.list_count += 1
            items = []
            if self.list_count == 1:
                items = [
                    {
                        "kind": "Job",
                        "metadata": {
                            "name": "owned-job",
                            "labels": {
                                "resiliencebenchmark.io/run-id": "exp-run-123-l1-a1"
                            },
                        },
                    },
                    {
                        "kind": "Job",
                        "metadata": {
                            "name": "other-job",
                            "labels": {
                                "resiliencebenchmark.io/run-id": "exp-other-run-l1-a1"
                            },
                        },
                    },
                ]
            return CommandResult(0, json.dumps({"items": items}), "")
        return CommandResult(0, "deleted", "")


def test_cleanup_deletes_only_objects_whose_label_contains_exact_run_id(
    tmp_path: Path,
) -> None:
    runner = Runner()

    result = cleanup_run_workloads(
        kubeconfig=tmp_path / "controller.kubeconfig",
        run_id="run-123",
        runner=runner,
    )

    delete_calls = [call for call in runner.calls if "delete" in call]
    assert result["verified"] is True
    assert result["owned_before"] == 1
    assert len(delete_calls) == 1
    assert "job/owned-job" in delete_calls[0]
