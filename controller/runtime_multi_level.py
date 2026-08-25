"""Bind the durable control-plane Run to the live multi-level orchestrator."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from progression.controller import JsonFileProgressionStore
from scripts.run_harness_trial import run_multi_level_episode

from .run_contracts import RunRecord

RunEpisode = Callable[..., dict[str, Any]]


class RuntimeMultiLevelRunner:
    def __init__(
        self,
        *,
        repo_root: Path,
        run_artifacts_root: Path,
        private_state_root: Path,
        trial_runner: Any,
        level_evaluator: Any,
        injector_factory: Any,
        trial_preparer: Any,
        trial_finalizer: Any,
        continue_after_failure: bool = False,
        run_episode: RunEpisode = run_multi_level_episode,
    ):
        self.repo_root = repo_root.resolve()
        self.run_artifacts_root = run_artifacts_root.resolve()
        self.private_state_root = private_state_root.resolve()
        self.trial_runner = trial_runner
        self.level_evaluator = level_evaluator
        self.injector_factory = injector_factory
        self.trial_preparer = trial_preparer
        self.trial_finalizer = trial_finalizer
        self.continue_after_failure = continue_after_failure
        self.run_episode = run_episode

    def __call__(
        self,
        record: RunRecord,
        multi_level_episode: Mapping[str, Any],
        public_episode: Mapping[str, Any],
        baseline: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        run_root = self.run_artifacts_root / record.run_id
        episode_path = run_root / "locked" / "multi-level-episode.json"
        public_path = run_root / "locked" / "public-episode.json"
        if not episode_path.is_file() or not public_path.is_file():
            raise RuntimeError("locked Episode artifacts are missing")
        if json.loads(episode_path.read_text(encoding="utf-8")) != dict(multi_level_episode):
            raise RuntimeError("locked multi-level Episode differs from the execution handoff")
        if json.loads(public_path.read_text(encoding="utf-8")) != dict(public_episode):
            raise RuntimeError("locked public Episode differs from the execution handoff")
        progression_path = self.private_state_root / record.run_id / "progression.json"
        progression_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return self.run_episode(
            self.repo_root,
            episode_file=episode_path,
            run_id=record.run_id,
            agent_id=(
                f"{record.spec.harness.harness_id}:{record.spec.harness.model_alias}"
            ),
            trial_runner=self.trial_runner,
            level_evaluator=self.level_evaluator,
            injector_factory=self.injector_factory,
            trial_preparer=self.trial_preparer,
            trial_finalizer=self.trial_finalizer,
            progression_store=JsonFileProgressionStore(progression_path),
            resume=progression_path.is_file(),
            continue_after_failure=self.continue_after_failure,
        )
