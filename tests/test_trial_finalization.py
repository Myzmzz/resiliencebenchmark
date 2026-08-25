from __future__ import annotations

from pathlib import Path

from controller.trial_finalization import PerTrialFinalizer
from controller.trial_preparation import TrialRuntimeContextStore
from progression.controller import TrialTicket


def _ticket() -> TrialTicket:
    return TrialTicket(
        trial_id="run-1-L1-a1",
        run_id="run-1",
        episode_id="EPI-1",
        level_id="L1",
        attempt=1,
    )


class FaultControl:
    def __init__(self, *, absent: bool = True):
        self.absent = absent
        self.handles = []

    def destroy(self, cleanup_handle: str):
        self.handles.append(cleanup_handle)
        return {"verified_absent": self.absent, "state": "destroyed" if self.absent else "running"}

    def recovery_status(self, cleanup_handle: str):
        return {"state": "absent" if self.absent else "running"}

    def inventory(self, namespace: str):
        return {
            "global_chaosblade_count": 0 if self.absent else 1,
            "active_owned_count": 0 if self.absent else 1,
        }


def test_finalizer_uses_controller_owned_handle_and_requires_business_recovery(
    tmp_path: Path,
) -> None:
    contexts = TrialRuntimeContextStore(tmp_path / "private" / "contexts")
    contexts.save(
        "run-1-L1-a1",
        {"cleanup_handle": "cleanup-run-1-l1-a1", "target": {"uid": "pod-uid"}},
    )
    control = FaultControl()
    finalizer = PerTrialFinalizer(
        context_store=contexts,
        main_fault_control=control,
        business_recovery_verifier=lambda *_args: {
            "verified": True,
            "evidence_refs": ["workload/recovery.json"],
        },
    )

    result = finalizer(_ticket(), {"level_id": "L1"}, {"status": "completed"})

    assert result["verified"] is True
    assert result["fault_absent"] is True
    assert control.handles == ["cleanup-run-1-l1-a1"]


def test_finalizer_fails_when_fault_control_cannot_verify_absence(tmp_path: Path) -> None:
    contexts = TrialRuntimeContextStore(tmp_path / "private" / "contexts")
    contexts.save("run-1-L1-a1", {"cleanup_handle": "cleanup-run-1-l1-a1"})
    finalizer = PerTrialFinalizer(
        context_store=contexts,
        main_fault_control=FaultControl(absent=False),
        business_recovery_verifier=lambda *_args: {"verified": True},
    )

    result = finalizer(_ticket(), {"level_id": "L1"}, {"status": "completed"})

    assert result["verified"] is False
    assert result["status"] == "failed"
