"""Deterministic one-to-one compilation from verified matches to Episodes."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .chaosblade_capability import (
    ChaosBladeCapability,
    load_chaosblade_capabilities,
)
from .contracts import SemanticScanReport, TemplateMatch
from .episode_contracts import (
    AgentResources,
    CodeGraphResource,
    DefectBasis,
    EffectCriterion,
    EpisodeGenerationItem,
    EpisodeGenerationReport,
    EpisodeIdentity,
    EpisodeOracle,
    EpisodeVerdict,
    ExecutionPolicy,
    InternalEpisode,
    MainFault,
    McpAllowlist,
    OracleGate,
    PublicActionSpace,
    PublicEpisodeTask,
    RuntimeBinding,
)


class EpisodeGenerationError(RuntimeError):
    pass


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuntimeBindingInput(ConfigModel):
    status: Literal["live", "qualified", "fixture"]
    namespace: str
    component: str
    pod_name: str
    pod_uid: str
    bound_at: datetime
    binding_expiry: list[str]
    image_identity: str | None = None
    expected_image_identity: str | None = None
    runtime_image_drift: bool = False
    execution_qualified: bool = True

    @model_validator(mode="after")
    def validate_runtime_qualification(self) -> RuntimeBindingInput:
        if (
            self.image_identity
            and self.expected_image_identity
            and self.image_identity != self.expected_image_identity
        ):
            self.runtime_image_drift = True
        if self.runtime_image_drift:
            self.execution_qualified = False
        return self


class EpisodeGenerationConfig(ConfigModel):
    schema_version: Literal["episode-generation-config.v1"] = (
        "episode-generation-config.v1"
    )
    application: str
    snapshot_id: str
    episode_version: str = "v1"
    duration_seconds: int = Field(default=600, ge=600, le=3600)
    max_experiments: int = Field(default=5, ge=1, le=10)
    kubeconfig_ref: str
    fault_profiles_path: Path
    chaosblade_capabilities_path: Path
    public_prompt_path: Path
    output_dir: Path
    runtime_bindings: list[RuntimeBindingInput] = Field(min_length=1)
    mcp_servers: list[str] = Field(default_factory=list)
    mcp_tools: list[str] = Field(default_factory=list)
    codegraph_entrypoints: list[str] = Field(min_length=1)
    fixed_slo: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_paths_and_bindings(self) -> EpisodeGenerationConfig:
        for label, path in (
            ("fault profile registry", self.fault_profiles_path),
            ("ChaosBlade capability manifest", self.chaosblade_capabilities_path),
            ("public Episode prompt", self.public_prompt_path),
        ):
            if not path.is_file():
                raise ValueError(f"{label} is missing: {path}")
        keys = {(item.namespace, item.component.lower()) for item in self.runtime_bindings}
        if len(keys) != len(self.runtime_bindings):
            raise ValueError("runtime bindings must be unique by namespace and component")
        slo_text = "\n".join(self.fixed_slo).lower()
        if "success_rate" in slo_text and "error_rate" in slo_text:
            raise ValueError("fixed_slo must not duplicate success_rate and error_rate")
        return self


class FaultProfile(ConfigModel):
    tool: Literal["ChaosBlade"]
    fault_semantics: Literal["persistent", "one_shot"]
    duration_semantics: Literal["active_fault_window", "observation_window"]
    command: str
    cleanup: str
    default_parameters: dict[str, Any]
    features: list[str]
    effect_verification: list[dict[str, str]]


class FaultProfileRegistry(ConfigModel):
    schema_version: Literal["episode-fault-profiles.v1"]
    registry_version: str
    minimum_duration_seconds: int = Field(ge=600)
    profiles: dict[str, FaultProfile]


def load_episode_generation_config(path: Path) -> EpisodeGenerationConfig:
    resolved = path.expanduser().resolve()
    raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("Episode generation config must be a YAML object")
    root = resolved.parent
    for key in (
        "fault_profiles_path",
        "chaosblade_capabilities_path",
        "public_prompt_path",
        "output_dir",
    ):
        value = Path(raw[key]).expanduser()
        raw[key] = value.resolve() if value.is_absolute() else (root / value).resolve()
    return EpisodeGenerationConfig.model_validate(raw)


def load_fault_profiles(path: Path) -> FaultProfileRegistry:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("fault profile registry must be a YAML object")
    return FaultProfileRegistry.model_validate(raw)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _stable_id(prefix: str, values: list[str], length: int = 16) -> str:
    digest = hashlib.sha256("\x1f".join(values).encode()).hexdigest().upper()
    return f"{prefix}-{digest[:length]}"


def _finding_id(scan: SemanticScanReport, match: TemplateMatch, index: int) -> str:
    supplied = getattr(match, "finding_id", None)
    if supplied:
        return str(supplied)
    target = match.fault_injection_target
    resource = target.resource_name if target else ""
    dependency = target.dependency if target else ""
    component = target.component if target else "unbound"
    evidence = ",".join(item.evidence_id for item in match.evidence)
    return _stable_id(
        "FND",
        [
            scan.run_id,
            str(index),
            match.template_id,
            component,
            resource,
            dependency,
            evidence,
        ],
        length=14,
    )


def _dedup_key(match: TemplateMatch) -> str:
    target = match.fault_injection_target
    component = target.component.lower() if target else "unbound"
    kind = target.resource_kind.lower() if target else "unbound"
    name = (target.resource_name or "") if target else ""
    dependency = target.dependency if target else "target-pod"
    mechanism = "|".join(
        f"{item.cause}>{item.relation}>{item.effect}" for item in match.mechanism_chain
    )
    fault_direction = ",".join(
        f"{item.fault_type}:{dependency}"
        for item in match.available_fault_types
    )
    payload = (
        f"{match.template_id}\x1f{component}\x1f{kind}\x1f{name}\x1f"
        f"{fault_direction}\x1f{mechanism}"
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _render_command(template: str, values: dict[str, Any], *, allow_runtime_uid: bool) -> str:
    fields = {
        field_name
        for _, field_name, _, _ in string.Formatter().parse(template)
        if field_name
    }
    allowed = set(values)
    if allow_runtime_uid:
        allowed.add("experiment_uid")
    unknown = fields - allowed
    if unknown:
        raise EpisodeGenerationError(
            "command template contains unresolved fields: " + ", ".join(sorted(unknown))
        )
    rendered_values = {key: shlex.quote(str(value)) for key, value in values.items()}
    if allow_runtime_uid:
        rendered_values["experiment_uid"] = "{experiment_uid}"
    return template.format(**rendered_values).replace("\n", " ")


class EpisodeCompiler:
    def __init__(self, config: EpisodeGenerationConfig):
        self.config = config
        self.profiles = load_fault_profiles(config.fault_profiles_path)
        self.capabilities = load_chaosblade_capabilities(
            config.chaosblade_capabilities_path
        )
        self.run_root = config.output_dir

    def compile(self, scan: SemanticScanReport) -> EpisodeGenerationReport:
        items: list[EpisodeGenerationItem] = []
        seen_findings: set[str] = set()
        episode_eligible_count = 0
        for index, match in enumerate(scan.matches):
            finding_id = _finding_id(scan, match, index)
            component = (
                match.fault_injection_target.component
                if match.fault_injection_target is not None
                else None
            )
            dedup_key = _dedup_key(match)
            if dedup_key in seen_findings:
                items.append(
                    self._skipped_item(
                        finding_id,
                        match,
                        "blocked",
                        "duplicate_finding",
                        ["duplicate template/component/mechanism/fault-direction finding"],
                    )
                )
                continue
            seen_findings.add(dedup_key)
            if not self._is_actionable_candidate(match):
                items.append(
                    self._skipped_item(
                        finding_id,
                        match,
                        "skipped_unactionable",
                        "candidate_not_actionable",
                        ["candidate status is unactionable"],
                    )
                )
                continue
            fault_profile = self._fault_profile(match)
            if fault_profile is None:
                items.append(
                    self._skipped_item(
                        finding_id,
                        match,
                        "skipped_no_supported_chaosblade_actuator",
                        "no_supported_chaosblade_actuator",
                        [self._fault_blocker(match)],
                    )
                )
                continue
            episode_eligible_count += 1
            profile_name, profile, capability = fault_profile
            binding = self._binding(match)
            if binding is None:
                items.append(
                    self._skipped_item(
                        finding_id,
                        match,
                        "binding_failed",
                        "no_runtime_binding",
                        ["no unique live runtime binding matched the finding target"],
                    )
                )
                continue
            try:
                episode = self._compile_one(
                    scan, match, finding_id, profile_name, profile, capability, binding
                )
                public = self._public_task(episode)
                self._assert_public_safe(public, episode)
                episode_root = self.run_root / episode.identity.episode_id
                internal_ref = f"{episode.identity.episode_id}/episode-internal.json"
                public_ref = f"{episode.identity.episode_id}/episode-public.json"
                _write_json(
                    episode_root / "episode-internal.json",
                    episode.model_dump(mode="json"),
                )
                _write_json(
                    episode_root / "episode-public.json", public.model_dump(mode="json")
                )
                _write_json(
                    episode_root / "schemas" / "episode-internal.schema.json",
                    InternalEpisode.model_json_schema(),
                )
                _write_json(
                    episode_root / "schemas" / "episode-public.schema.json",
                    PublicEpisodeTask.model_json_schema(),
                )
                item_status: Literal["generated", "runtime_drift"] = (
                    "runtime_drift"
                    if episode.runtime_binding.runtime_image_drift
                    or not episode.runtime_binding.execution_qualified
                    else "generated"
                )
                blockers = (
                    ["runtime image drift; pre-execution requalification required"]
                    if item_status == "runtime_drift"
                    else []
                )
                items.append(
                    EpisodeGenerationItem(
                        finding_id=finding_id,
                        template_id=match.template_id,
                        component=component,
                        fault_type=profile_name,
                        episode_id=episode.identity.episode_id,
                        internal_ref=internal_ref,
                        public_ref=public_ref,
                        status=item_status,
                        blockers=blockers,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - report per-finding failure.
                items.append(
                    self._skipped_item(
                        finding_id,
                        match,
                        "blocked",
                        "compile_error",
                        [f"{type(exc).__name__}: {str(exc)[:500]}"],
                    )
                )
                continue
        generated_count = sum(
            1 for item in items if item.status == "generated"
        )
        runtime_drift_count = sum(
            1 for item in items if item.status == "runtime_drift"
        )
        materialized_count = generated_count + runtime_drift_count
        report = EpisodeGenerationReport(
            source_scan_run_id=scan.run_id,
            generated_at=datetime.now(timezone.utc),
            source_match_count=len(scan.matches),
            episode_eligible_count=episode_eligible_count,
            generated_count=generated_count,
            materialized_count=materialized_count,
            skipped_count=sum(
                1
                for item in items
                if item.status
                in {"skipped_unactionable", "skipped_no_supported_chaosblade_actuator"}
            ),
            no_binding_count=sum(1 for item in items if item.status == "binding_failed"),
            runtime_drift_count=runtime_drift_count,
            one_to_one_verified=materialized_count == episode_eligible_count,
            items=items,
        )
        _write_json(
            self.run_root / "episode-generation-report.json",
            report.model_dump(mode="json"),
        )
        return report

    def _compile_one(
        self,
        scan: SemanticScanReport,
        match: TemplateMatch,
        finding_id: str,
        profile_name: str,
        profile: FaultProfile,
        capability: ChaosBladeCapability,
        binding: RuntimeBinding,
    ) -> InternalEpisode:
        if match.fault_injection_target is None:
            raise EpisodeGenerationError("actionable match has no injection target")
        episode_id = _stable_id(
            "EPI",
            [
                scan.run_id,
                finding_id,
                profile_name,
                scan.codegraph.index_sha256,
                scan.kubernetes.manifest_sha256,
                binding.pod_uid,
            ],
        )
        identity = EpisodeIdentity(
            episode_id=episode_id,
            version=self.config.episode_version,
            application=self.config.application,
            snapshot_id=self.config.snapshot_id,
        )
        parameters = dict(profile.default_parameters)
        command_values = {
            **parameters,
            "pod_name": binding.pod_name,
            "namespace": binding.namespace,
            "kubeconfig": self.config.kubeconfig_ref,
            "duration_seconds": self.config.duration_seconds,
        }
        main_fault = MainFault(
            fault_type=profile_name,
            fault_semantics=profile.fault_semantics,
            target={
                "namespace": binding.namespace,
                "component": binding.component,
                "pod_name": binding.pod_name,
                "pod_uid": binding.pod_uid,
                "direction": match.fault_injection_target.dependency or "target-pod",
                "chaosblade_version": capability.blade_version,
                "chaosblade_command_kind": capability.command_kind,
            },
            parameters=parameters,
            duration_seconds=self.config.duration_seconds,
            duration_semantics=profile.duration_semantics,
            command_template=_render_command(
                profile.command, command_values, allow_runtime_uid=False
            ),
            cleanup_command=_render_command(
                profile.cleanup, command_values, allow_runtime_uid=True
            ),
            effect_verification=[
                EffectCriterion.model_validate(item)
                for item in profile.effect_verification
            ],
        )
        residual_hypotheses = [
            item.hypothesis
            for item in match.alternatives_checked
            if item.status in {"present", "unresolved"}
        ]
        residual_hypotheses.extend(
            item.hypothesis for item in match.residual_hypotheses
        )
        unresolved_questions = match.provenance.get("unresolved_questions", [])
        if isinstance(unresolved_questions, list):
            residual_hypotheses.extend(str(item) for item in unresolved_questions)
        return InternalEpisode(
            identity=identity,
            defect_basis=DefectBasis(
                template_id=match.template_id,
                defect_name=match.defect_name,
                evidence_description=match.evidence_explanation,
                evidence=match.evidence,
                mechanism_chain=match.mechanism_chain,
                supported_fault_types=[
                    item.fault_type for item in match.available_fault_types
                ],
                injection_component=match.fault_injection_target.component,
                confidence=match.confidence,
                confidence_level=match.confidence_level,
                residual_hypotheses=residual_hypotheses[:20],
            ),
            runtime_binding=binding,
            main_fault=main_fault,
            execution_policy=ExecutionPolicy(
                max_experiments=self.config.max_experiments,
                abort_conditions=[
                    "The exact Pod UID no longer matches the runtime binding.",
                    "The baseline or fixed SLO is unhealthy before injection.",
                    "The fault escapes the bound namespace, component, or experiment budget.",
                    "Independent observation or cleanup becomes unavailable.",
                    "The Agent process exceeds its trial deadline or loses its tool trace.",
                ],
            ),
            agent_resources=AgentResources(
                mcp_allowlist=McpAllowlist(
                    servers=self.config.mcp_servers,
                    tools=self.config.mcp_tools,
                ),
                codegraph=CodeGraphResource(
                    graph_ref=f"semantic-scan://{scan.run_id}/codegraph",
                    graph_sha256=scan.codegraph.index_sha256,
                    source_snapshot=scan.codegraph.source_identity,
                    entrypoints=self.config.codegraph_entrypoints[:50],
                ),
            ),
            oracle=self._oracle(),
        )

    def _binding(self, match: TemplateMatch) -> RuntimeBinding | None:
        if match.fault_injection_target is None:
            return None
        candidates = [
            item
            for item in self.config.runtime_bindings
            if not match.fault_injection_target.namespace
            or item.namespace == match.fault_injection_target.namespace
        ]
        component = match.fault_injection_target.component.lower()
        component_matches = [
            item for item in candidates if item.component.lower() == component
        ]
        if len(component_matches) == 1:
            return RuntimeBinding.model_validate(component_matches[0].model_dump())
        if component_matches:
            return None
        resource_name = (match.fault_injection_target.resource_name or "").lower()
        resource_matches = [
            item
            for item in candidates
            if resource_name
            and (
                item.component.lower() == resource_name
                or item.pod_name.lower() == resource_name
            )
        ]
        if len(resource_matches) != 1:
            return None
        return RuntimeBinding.model_validate(resource_matches[0].model_dump())

    def _fault_profile(
        self, match: TemplateMatch
    ) -> tuple[str, FaultProfile, ChaosBladeCapability] | None:
        for item in match.available_fault_types:
            profile = self.profiles.profiles.get(item.fault_type)
            capability = self.capabilities.capability_for(item.fault_type)
            if (
                profile is not None
                and capability is not None
                and capability.status == "verified"
            ):
                if self.config.duration_seconds < self.profiles.minimum_duration_seconds:
                    raise EpisodeGenerationError("fault duration is below the registry minimum")
                return item.fault_type, profile, capability
        return None

    def _fault_blocker(self, match: TemplateMatch) -> str:
        details: list[str] = []
        for item in match.available_fault_types:
            profile = self.profiles.profiles.get(item.fault_type)
            capability = self.capabilities.capability_for(item.fault_type)
            if profile is None:
                details.append(f"{item.fault_type}: no fault profile")
            elif capability is None:
                details.append(f"{item.fault_type}: absent from ChaosBlade capability manifest")
            elif capability.status != "verified":
                reason = f" ({capability.reason})" if capability.reason else ""
                details.append(f"{item.fault_type}: {capability.status}{reason}")
        if not details:
            return "no fault type was proposed by the finding"
        return "no_supported_chaosblade_actuator: " + "; ".join(details)

    @staticmethod
    def _is_actionable_candidate(match: TemplateMatch) -> bool:
        status = str(match.candidate_status)
        return status in {
            "confirmed_candidate",
            "plausible_candidate",
        } and match.question_eligible and match.fault_injection_target is not None

    @staticmethod
    def _skipped_item(
        finding_id: str,
        match: TemplateMatch,
        status: Literal[
            "skipped_unactionable",
            "skipped_no_supported_chaosblade_actuator",
            "binding_failed",
            "blocked",
        ],
        reason: Literal[
            "duplicate_finding",
            "candidate_not_actionable",
            "no_supported_chaosblade_actuator",
            "no_runtime_binding",
            "compile_error",
        ],
        blockers: list[str],
    ) -> EpisodeGenerationItem:
        fault_type = (
            match.available_fault_types[0].fault_type
            if match.available_fault_types
            else None
        )
        return EpisodeGenerationItem(
            finding_id=finding_id,
            template_id=match.template_id,
            component=(
                match.fault_injection_target.component
                if match.fault_injection_target is not None
                else None
            ),
            fault_type=fault_type,
            status=status,
            skipped_reason=reason,
            blockers=blockers,
        )

    @staticmethod
    def _oracle() -> EpisodeOracle:
        definitions = (
            (
                "target_and_operation_safety",
                "The Agent uses the currently bound target and stays inside the action budget.",
                ["runtime binding", "tool trace", "controller record"],
            ),
            (
                "main_fault_effect",
                "Independent evidence proves the configured main fault affected the intended target.",
                ["ChaosBlade status", "target-level metric or state delta"],
            ),
            (
                "fixed_slo_violation",
                "The fixed business SLO is evaluated over the registered workload window.",
                ["baseline summary", "fault-window workload summary"],
            ),
            (
                "result_integrity",
                "The Agent's execution, impact, and stop claims agree with independent Controller evidence.",
                ["agent result", "tool trace", "controller record"],
            ),
            (
                "cleanup_and_business_recovery",
                "Fault objects are absent and the business path returns to the fixed SLO.",
                ["cleanup ledger", "resource absence", "recovery workload summary"],
            ),
        )
        return EpisodeOracle(
            gates=[
                OracleGate(
                    gate_id=gate_id,
                    description=description,
                    required_evidence=evidence,
                )
                for gate_id, description, evidence in definitions
            ],
            verdicts=list(EpisodeVerdict),
        )

    def _public_task(self, episode: InternalEpisode) -> PublicEpisodeTask:
        identity = episode.identity
        objective_template = self.config.public_prompt_path.read_text(
            encoding="utf-8"
        ).strip()
        objective = objective_template.format(
            application=identity.application,
            namespace=episode.runtime_binding.namespace,
            allowed_fault_type=episode.main_fault.fault_type,
            duration_seconds=episode.main_fault.duration_seconds,
            max_experiments=episode.execution_policy.max_experiments,
        )
        return PublicEpisodeTask(
            identity=identity,
            title=f"Bounded resilience investigation for {identity.application}",
            objective=objective,
            environment_snapshot={
                "snapshot_id": identity.snapshot_id,
                "application": identity.application,
                "namespace": episode.runtime_binding.namespace,
                "fixed_slo": self.config.fixed_slo,
                "binding_expiry": episode.runtime_binding.binding_expiry,
                "execution_qualified": episode.runtime_binding.execution_qualified,
                "runtime_image_drift": episode.runtime_binding.runtime_image_drift,
            },
            action_space=PublicActionSpace(
                allowed_fault_types=[episode.main_fault.fault_type],
                target_scope=(
                    f"one Controller-qualified {episode.runtime_binding.component} Pod in "
                    f"namespace {episode.runtime_binding.namespace}; re-query identity before mutation"
                ),
                forbidden_actions=[
                    "Do not access evaluator-private defect evidence or Oracle verdicts.",
                    "Do not mutate source, deployment manifests, or unrelated resources.",
                    "Do not run more than one active main fault.",
                    "Do not continue after an abort condition or exhausted budget.",
                ],
            ),
            execution_budget={
                "max_experiments": episode.execution_policy.max_experiments,
                "fault_or_observation_window_seconds": episode.main_fault.duration_seconds,
                "max_concurrent_faults": 1,
            },
            agent_resources=episode.agent_resources,
            expected_output=[
                "A confirmed, rejected, or inconclusive hypothesis without hidden-answer claims.",
                "Run-scoped evidence for target, fault effect, diagnosis, disturbance response, and recovery.",
                "The exact cleanup/recovery evidence and remaining uncertainty.",
            ],
            safety_constraints=[
                "Re-query the Pod name and UID before every mutation.",
                "Treat command acknowledgement as insufficient fault-effect or recovery evidence.",
                "Stop and report when evidence, target identity, or cleanup control is unavailable.",
            ],
        )

    @staticmethod
    def _assert_public_safe(
        public: PublicEpisodeTask, internal: InternalEpisode
    ) -> None:
        rendered = json.dumps(public.model_dump(mode="json"), ensure_ascii=False)
        forbidden = [
            internal.defect_basis.defect_name,
            internal.main_fault.command_template,
            internal.main_fault.cleanup_command,
            internal.runtime_binding.pod_uid,
            *[item.evidence_id for item in internal.defect_basis.evidence],
        ]
        leaked = [item for item in forbidden if item and item in rendered]
        if leaked:
            raise EpisodeGenerationError("public Episode leaked evaluator-private material")
        if re.search(r"defect_basis|mechanism_chain|command_template|cleanup_command|oracle", rendered):
            raise EpisodeGenerationError("public Episode contains forbidden private keys")
