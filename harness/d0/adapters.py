"""D0 Agent adapters using the same isolated runtime as Stage2 tasks."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from stage2_service.contracts import (
    DecisionPolicy, ExpectedOutcome, HarnessKind, InteractionMode,
    PromptMode, STAGE2_BLADEAI_DEFAULT_MODEL, Stage2CaseId, default_case_specs,
)
from stage2_service.episode import load_fixed_episode
from stage2_service.gateway_evidence import read_gateway_artifact
from stage2_service.matrix import fixed_otel_episode_ref

from .common import utc_now, write_json

EventSink = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class AdapterResult:
    status: str
    started_at: str
    finished_at: str
    process_status: str
    artifact_ref: str = ""
    error: str = ""
    agent_recovery_requested: bool = False
    tool_calls: int = 0
    confirmations: int = 0
    failure_code: str = ""
    needs_human: bool = False
    native_session_trace_captured: bool = False
    model_alias: str = ""
    gateway_route: dict[str, Any] = field(default_factory=dict)
    gateway_config_sha256: str = ""
    gateway_evidence_verified: bool = False
    gateway_request_ids: tuple[str, ...] = ()
    gateway_evidence_ref: str = ""
    gateway_trial_id: str = ""


class D0Adapter(Protocol):
    name: str

    def run(self, *, prompt: str, trial_id: str, artifact_dir: Path,
            event_sink: EventSink) -> AdapterResult: ...

    def cancel(self) -> bool: ...


class NativeD0Adapter:
    """Qualify the measured Agent, not a Controller-side CLI or external SDK API."""

    def __init__(self, *, name: str, repo_root: Path, model_alias: str,
                 runtime_builder: Callable[..., Any], timeout_seconds: int):
        self.name = name
        self.repo_root = repo_root
        self.model_alias = model_alias
        self.runtime_builder = runtime_builder
        self.timeout_seconds = timeout_seconds
        self.cancel_event = threading.Event()
        self.cleanup_kubeconfig: Path | None = None

    def cancel(self) -> bool:
        self.cancel_event.set()
        return True

    def run(self, *, prompt: str, trial_id: str, artifact_dir: Path,
            event_sink: EventSink) -> AdapterResult:
        started = utc_now()
        self.cancel_event.clear()
        self.cleanup_kubeconfig = None
        harness = HarnessKind(self.name)
        episode = load_fixed_episode(fixed_otel_episode_ref(self.repo_root), root=self.repo_root)
        components = self.runtime_builder(episode, {harness: self.model_alias})
        self.cleanup_kubeconfig = Path(components.cleanup_backend.kubeconfig)
        campaign_id = artifact_dir.parent.name
        calls: set[str] = set()
        confirmations: set[str] = set()
        recovery_requested = False
        provisioned = False

        def observe(event: Any) -> None:
            nonlocal recovery_requested
            if hasattr(event, "model_dump"):
                event = event.model_dump(mode="json")
            if not isinstance(event, Mapping):
                return
            payload = dict(event.get("payload") or {})
            # Native text remains a transcript, never execution evidence.
            tool = event.get("tool")
            if tool and payload.get("source") != "mcp_server":
                return
            if event.get("event_type") not in {"TOOL_INTERACTION", "AGENT_MESSAGE"}:
                return  # Lifecycle facts are retained in harness-report.json.
            native_type = str(event.get("native_type") or "")
            call_id = str(payload.get("call_id") or "")
            if tool and native_type == "tool_call":
                calls.add(call_id)
                if str(tool).endswith("destroy_experiment"):
                    recovery_requested = True
            if (str(tool).endswith("harness_confirm") and native_type == "tool_result"
                    and (payload.get("result") or {}).get("allowed") is True):
                confirmations.add(call_id)
            event_sink({
                "ts": str(event.get("occurred_at") or utc_now()),
                "actor": "agent" if tool or event.get("event_type") == "AGENT_MESSAGE" else "harness",
                "agent": self.name,
                "kind": native_type or str(event.get("kind") or event.get("event_type") or "runtime_event"),
                "tool": tool,
                "source": payload.get("source", "native_stream"),
                "payload": payload,
            })

        try:
            components.traffic.start_sampling()
            runtime = components.preparer.prepare(
                trial_id, episode, namespace="otel-demo", target=None, main_fault=None,
            ).model_copy(update={"prompt_mode": PromptMode.VERBATIM, "interaction_mode": InteractionMode.GUIDED})
            capability = components.permissions.provision(campaign_id, trial_id, harness, episode, runtime)
            provisioned = True
            # These components belong solely to this D0 attempt. NativeRunner
            # still owns all homes, tokens, channel, audit, relay and AgentExec.
            components.harness_runner.artifact_root = artifact_dir / "native"
            components.harness_runner.timeout_seconds = self.timeout_seconds
            report = components.harness_runner.run(
                campaign_id=campaign_id, trial_id=trial_id, harness=harness,
                model_alias=self.model_alias, episode=episode, runtime_context=runtime,
                capability=capability, case=default_case_specs((Stage2CaseId.C0,))[0],
                base_prompt=prompt, prompt_mode=PromptMode.VERBATIM,
                interaction_mode=InteractionMode.GUIDED,
                decision_policy=DecisionPolicy.CLARIFY_MISSING,
                expected_outcome=ExpectedOutcome.EXECUTE_AND_RECOVER,
                prompt_level_label="UNSPECIFIED", event_observer=observe,
                cancel_requested=self.cancel_event.is_set,
            )
            write_json(artifact_dir / "harness-report.json", report.model_dump(mode="json"))
            gateway = self._gateway_metadata(
                report=report,
                native_root=artifact_dir / "native",
                trial_id=trial_id,
                harness=harness.value,
            )
            failure = str(report.final_output.get("harness_error_code") or "")
            if report.final_output.get("validation_error"):
                failure = failure or "RESULT_CONTRACT_INVALID"
            if report.status != "completed":
                failure = failure or "ADAPTER_PROCESS_FAILED"
            if not gateway["gateway_route"] or not gateway["gateway_config_sha256"]:
                failure = failure or "GATEWAY_ROUTE_EVIDENCE_MISSING"
            if gateway["gateway_evidence_verified"] is not True:
                failure = failure or "GATEWAY_EVIDENCE_MISSING"
            captured = any(
                ref.endswith("session-events.jsonl")
                and (artifact_dir / "native" / ref).is_file()
                and (artifact_dir / "native" / ref).stat().st_size > 0
                for ref in report.artifact_refs
            )
            if not captured:
                failure = failure or "CAPABILITY_TRACE_MISSING"
            return AdapterResult(
                status="finished" if report.status == "completed" and not failure else "failed",
                started_at=started, finished_at=utc_now(), process_status=report.status,
                artifact_ref="harness-report.json", tool_calls=len(calls),
                confirmations=len(confirmations), failure_code=failure,
                agent_recovery_requested=recovery_requested,
                native_session_trace_captured=captured,
                model_alias=gateway["model_alias"],
                gateway_route=gateway["gateway_route"],
                gateway_config_sha256=gateway["gateway_config_sha256"],
                gateway_evidence_verified=gateway["gateway_evidence_verified"],
                gateway_request_ids=gateway["gateway_request_ids"],
                gateway_evidence_ref=gateway["gateway_evidence_ref"],
                gateway_trial_id=gateway["gateway_trial_id"],
            )
        finally:
            try:
                components.supervisor.stop()
            finally:
                try:
                    components.traffic.close()
                finally:
                    if provisioned:
                        components.permissions.restore(trial_id)

    def _gateway_metadata(
        self,
        *,
        report,
        native_root: Path,
        trial_id: str,
        harness: str,
    ) -> dict[str, Any]:
        final = report.final_output if isinstance(report.final_output, Mapping) else {}
        model_alias = str(final.get("model_alias") or self.model_alias)
        if final.get("trial_id") and final.get("trial_id") != trial_id:
            return {
                "model_alias": model_alias,
                "gateway_route": {},
                "gateway_config_sha256": "",
                "gateway_evidence_verified": False,
                "gateway_request_ids": (),
                "gateway_evidence_ref": "",
                "gateway_trial_id": trial_id,
            }
        gateway_route = final.get("gateway_route") or {}
        if not isinstance(gateway_route, Mapping):
            gateway_route = {}
        gateway_hash = str(final.get("gateway_config_sha256") or "")
        request_ids = _string_tuple(final.get("gateway_request_ids"))
        evidence = self._persistent_gateway_evidence(
            report=report,
            native_root=native_root,
            raw_evidence_ref=str(final.get("gateway_evidence_ref") or ""),
            trial_id=trial_id,
            harness=harness,
            model_alias=model_alias,
            gateway_hash=gateway_hash,
            request_ids=request_ids,
        )
        evidence_verified = (
            final.get("gateway_evidence_verified") is True
            and bool(request_ids)
            and evidence is not None
        )
        return {
            "model_alias": model_alias,
            "gateway_route": dict(gateway_route),
            "gateway_config_sha256": gateway_hash,
            "gateway_evidence_verified": evidence_verified,
            "gateway_request_ids": request_ids,
            "gateway_evidence_ref": evidence["ref"] if evidence is not None else "",
            "gateway_trial_id": trial_id,
        }

    @staticmethod
    def _persistent_gateway_evidence(
        *,
        report,
        native_root: Path,
        raw_evidence_ref: str,
        trial_id: str,
        harness: str,
        model_alias: str,
        gateway_hash: str,
        request_ids: tuple[str, ...],
    ) -> dict[str, str] | None:
        if raw_evidence_ref != "gateway-requests.json":
            return None
        if not request_ids or len(set(request_ids)) != len(request_ids):
            return None
        for ref in getattr(report, "artifact_refs", ()) or ():
            ref_text = str(ref)
            if ref_text != raw_evidence_ref and not ref_text.endswith("/gateway-requests.json"):
                continue
            ref_path = Path(ref_text)
            if ref_path.is_absolute() or ".." in ref_path.parts:
                return None
            path = native_root / ref_path
            rows = read_gateway_artifact(
                path,
                trial_id=trial_id,
                harness=harness,
                model_alias=model_alias,
                config_sha256=gateway_hash,
                request_ids=set(request_ids),
            )
            if rows is not None:
                return {"ref": f"native/{ref_path.as_posix()}"}
            return None
        return None


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    items = tuple(value)
    if not all(isinstance(item, str) and item for item in items):
        return ()
    return items


def adapter_models(env: Mapping[str, str]) -> dict[str, str]:
    return {
        "bladeai": env.get("RESBENCH_D0_BLADEAI_MODEL", STAGE2_BLADEAI_DEFAULT_MODEL),
        "codex": env.get("RESBENCH_D0_CODEX_MODEL", "gpt-5.5"),
        "claude-code": env.get("RESBENCH_D0_CLAUDE_MODEL", "claude-opus-5"),
        "deepseek-harness": env.get("RESBENCH_D0_DSH_MODEL", "gpt-5.5"),
    }
