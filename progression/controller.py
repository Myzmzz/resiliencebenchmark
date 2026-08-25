"""Sequential level progression, retry accounting, and resumable state."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from .builder import validate_multi_level_episode


class EpisodeProgressStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass(frozen=True)
class TrialTicket:
    trial_id: str
    run_id: str
    episode_id: str
    level_id: str
    attempt: int


class ProgressionStore(Protocol):
    def save(self, state: Mapping[str, Any]) -> None: ...

    def load(self) -> dict[str, Any] | None: ...


class JsonFileProgressionStore:
    """Atomic JSON checkpoint store for optional resume support."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def save(self, state: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        temp_path = Path(raw_path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(dict(state), handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temp_path, self.path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("progression checkpoint must contain a JSON object")
        return value


class ProgressionController:
    """Advance only after PASS and stop after retry/budget exhaustion."""

    def __init__(
        self,
        episode: Mapping[str, Any],
        *,
        run_id: str,
        agent_id: str,
        store: ProgressionStore | None = None,
        resume: bool = False,
        continue_after_failure: bool = False,
    ) -> None:
        validate_multi_level_episode(episode)
        self.episode = dict(episode)
        self.run_id = run_id
        self.agent_id = agent_id
        self.store = store
        self.continue_after_failure = continue_after_failure
        loaded = store.load() if resume and store else None
        self.state = loaded or self._new_state()
        self._validate_state()
        self._persist()

    @property
    def status(self) -> EpisodeProgressStatus:
        return EpisodeProgressStatus(self.state["status"])

    @property
    def current_level(self) -> Mapping[str, Any] | None:
        if self.status is not EpisodeProgressStatus.ACTIVE:
            return None
        return self.episode["levels"][int(self.state["current_level_index"])]

    def start_trial(self) -> TrialTicket:
        if self.status is not EpisodeProgressStatus.ACTIVE:
            raise RuntimeError(f"episode is terminal: {self.status.value}")
        if self.state.get("open_trial"):
            raise RuntimeError("cannot start a trial while another trial is open")
        level = self.current_level
        assert level is not None
        level_id = str(level["level_id"])
        attempts = int(self.state["level_attempts"].get(level_id, 0))
        if attempts >= int(level["retry_budget"]):
            self._finish(EpisodeProgressStatus.FAIL, "level_retry_budget_exhausted")
            raise RuntimeError("level retry budget is exhausted")
        if int(self.state["total_attempts"]) >= int(self.episode["total_retry_budget"]):
            self._finish(EpisodeProgressStatus.FAIL, "episode_retry_budget_exhausted")
            raise RuntimeError("episode retry budget is exhausted")
        attempt = attempts + 1
        trial_id = f"{self.run_id}-{level_id}-a{attempt}"
        self.state["level_attempts"][level_id] = attempt
        self.state["total_attempts"] = int(self.state["total_attempts"]) + 1
        self.state["open_trial"] = {
            "trial_id": trial_id,
            "level_id": level_id,
            "attempt": attempt,
            "started_at": _utc_now(),
        }
        self._persist()
        return TrialTicket(
            trial_id=trial_id,
            run_id=self.run_id,
            episode_id=str(self.episode["episode_id"]),
            level_id=level_id,
            attempt=attempt,
        )

    def record_result(
        self,
        trial_id: str,
        *,
        primary_status: str,
        result_ref: str,
        failure_status: str | None = None,
    ) -> dict[str, Any]:
        if primary_status not in {"PASS", "FAIL", "SKIP"}:
            raise ValueError("primary_status must be PASS, FAIL, or SKIP")
        open_trial = self.state.get("open_trial")
        if not isinstance(open_trial, Mapping) or open_trial.get("trial_id") != trial_id:
            existing = next(
                (item for item in self.state["history"] if item.get("trial_id") == trial_id),
                None,
            )
            if existing and existing.get("primary_status") == primary_status:
                return self.snapshot()
            raise ValueError("result does not match the open trial")
        completed = {
            **dict(open_trial),
            "completed_at": _utc_now(),
            "primary_status": primary_status,
            "failure_status": failure_status,
            "result_ref": result_ref,
        }
        self.state["history"].append(completed)
        self.state["open_trial"] = None
        level_id = str(open_trial["level_id"])
        if primary_status == "PASS":
            self.state["level_statuses"][level_id] = "PASS"
            next_index = int(self.state["current_level_index"]) + 1
            if next_index >= len(self.episode["levels"]):
                if self.continue_after_failure and "FAIL" in self.state[
                    "level_statuses"
                ].values():
                    self._finish(
                        EpisodeProgressStatus.FAIL,
                        "all_levels_executed_with_failures",
                    )
                else:
                    self._finish(EpisodeProgressStatus.PASS, "all_levels_passed")
            else:
                self.state["current_level_index"] = next_index
                self.state["terminal_reason"] = None
                self._persist()
        elif primary_status == "SKIP":
            self.state["level_statuses"][level_id] = "SKIP"
            self._finish(EpisodeProgressStatus.SKIP, failure_status or "precondition_not_met")
        else:
            self.state["level_statuses"][level_id] = "FAIL"
            level = self.episode["levels"][int(self.state["current_level_index"])]
            exhausted = int(self.state["level_attempts"][level_id]) >= int(level["retry_budget"])
            budget_exhausted = int(self.state["total_attempts"]) >= int(
                self.episode["total_retry_budget"]
            )
            if exhausted or budget_exhausted:
                next_index = int(self.state["current_level_index"]) + 1
                if self.continue_after_failure and next_index < len(
                    self.episode["levels"]
                ):
                    self.state["current_level_index"] = next_index
                    self.state["terminal_reason"] = None
                    self._persist()
                elif self.continue_after_failure:
                    self._finish(
                        EpisodeProgressStatus.FAIL,
                        "all_levels_executed_with_failures",
                    )
                else:
                    reason = (
                        "level_retry_budget_exhausted"
                        if exhausted
                        else "episode_retry_budget_exhausted"
                    )
                    self._finish(EpisodeProgressStatus.FAIL, reason)
            else:
                self.state["terminal_reason"] = None
                self._persist()
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.state))

    def _new_state(self) -> dict[str, Any]:
        return {
            "schema_version": "progression-state.v1",
            "episode_id": self.episode["episode_id"],
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "status": EpisodeProgressStatus.ACTIVE.value,
            "current_level_index": 0,
            "total_attempts": 0,
            "level_attempts": {level["level_id"]: 0 for level in self.episode["levels"]},
            "level_statuses": {level["level_id"]: "PENDING" for level in self.episode["levels"]},
            "open_trial": None,
            "history": [],
            "terminal_reason": None,
            "updated_at": _utc_now(),
        }

    def _finish(self, status: EpisodeProgressStatus, reason: str) -> None:
        self.state["status"] = status.value
        self.state["terminal_reason"] = reason
        self._persist()

    def _persist(self) -> None:
        self.state["updated_at"] = _utc_now()
        if self.store:
            self.store.save(self.state)

    def _validate_state(self) -> None:
        if self.state.get("episode_id") != self.episode.get("episode_id"):
            raise ValueError("checkpoint episode_id does not match")
        if self.state.get("run_id") != self.run_id or self.state.get("agent_id") != self.agent_id:
            raise ValueError("checkpoint run_id or agent_id does not match")
        if self.state.get("schema_version") != "progression-state.v1":
            raise ValueError("unsupported progression checkpoint schema")
        index = int(self.state.get("current_level_index", -1))
        if not 0 <= index < len(self.episode["levels"]):
            raise ValueError("checkpoint current_level_index is invalid")
        if int(self.state.get("total_attempts", -1)) > int(self.episode["total_retry_budget"]):
            raise ValueError("checkpoint exceeds total retry budget")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
