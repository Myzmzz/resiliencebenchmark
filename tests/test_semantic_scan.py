from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path

import pytest
from langchain.agents.structured_output import StructuredOutputValidationError
from langchain_core.messages import AIMessage

from resilience_agent.semantic_scan.agents import (
    LangChainSemanticAgents,
    SemanticAgentError,
    _bounded_tool_result,
)
from resilience_agent.semantic_scan.codegraph_driver import CodeGraphDriver
from resilience_agent.semantic_scan.config import (
    CodebaseConfig,
    CodeGraphConfig,
    KubernetesConfig,
    KubernetesSourceConfig,
    load_semantic_scan_config,
)
from resilience_agent.semantic_scan.contracts import (
    CodeGraphManifest,
    CoordinatorPlan,
    DClass,
    EvidenceKind,
    EvidenceRef,
    FaultInjectionTarget,
    FaultType,
    KubernetesManifest,
    MechanismStep,
    SemanticScanReport,
    TemplateAgentOutput,
    TemplateCoverage,
    TemplateFinding,
    TemplateMatch,
    TemplatePlan,
    VerificationDecision,
)
from resilience_agent.semantic_scan.episode_contracts import (
    InternalEpisode,
    PublicEpisodeTask,
)
from resilience_agent.semantic_scan.episode_generator import (
    EpisodeCompiler,
    load_episode_generation_config,
)
from resilience_agent.semantic_scan.evidence import EvidenceLedger
from resilience_agent.semantic_scan.kubernetes_scanner import KubernetesConfigScanner
from resilience_agent.semantic_scan.prompts import PromptRepository
from resilience_agent.semantic_scan.registry import (
    ACTIVE_TEMPLATE_IDS,
    load_template_registry,
)
from resilience_agent.semantic_scan.workflow import SemanticScanWorkflow

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG = REPO_ROOT / "resilience_agent/config/semantic-scan.otel-demo.yaml"


def test_semantic_config_registry_and_prompt_package_are_complete() -> None:
    config = load_semantic_scan_config(CONFIG)
    registry = load_template_registry(config.templates_path)
    prompts = PromptRepository(config.prompts_root)

    assert tuple(item.template_id for item in registry.templates) == ACTIVE_TEMPLATE_IDS
    assert len({item.prompt_file for item in registry.templates}) == 12
    assert all(item.fault_types for item in registry.templates)
    common = prompts.read("common_system.md")
    verifier = prompts.read("verifier.md")
    assert "CodeGraph" in common
    assert "主动寻找反证" in common
    assert "竞争解释" in verifier
    for template in registry.templates:
        specialized = prompts.read(template.prompt_file)
        assert template.template_id in specialized
        assert len(specialized) >= 200


def test_tool_result_is_strictly_bounded() -> None:
    result = _bounded_tool_result(
        {
            "nodes": [
                {"name": f"node-{index}", "code": "x" * 4000}
                for index in range(20)
            ]
        },
        [
            {"evidence_id": f"EV-{index:012d}", "statement": "y" * 1000}
            for index in range(20)
        ],
        8000,
    )

    assert len(json.dumps(result, ensure_ascii=False, sort_keys=True)) <= 8000
    assert result["truncated_to_context_budget"] is True

    positive = load_semantic_scan_config(
        REPO_ROOT / "resilience_agent/config/semantic-scan.rd14-positive.yaml"
    )
    negative = load_semantic_scan_config(
        REPO_ROOT / "resilience_agent/config/semantic-scan.rd14-negative.yaml"
    )
    assert positive.active_template_ids == negative.active_template_ids == ["RD-14"]
    assert positive.kubernetes.authoritative_for_namespace is True
    assert negative.kubernetes.authoritative_for_namespace is True


def test_kubernetes_scanner_extracts_resilience_fields_and_redacts_secret_env(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "deployment.yaml"
    manifest.write_text(
        """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: checkout
  namespace: otel-demo
spec:
  replicas: 1
  strategy:
    rollingUpdate: {maxUnavailable: 1}
  template:
    spec:
      terminationGracePeriodSeconds: 10
      containers:
        - name: checkout
          image: example/checkout:v1
          resources:
            requests: {cpu: 100m, memory: 128Mi}
            limits: {cpu: 200m, memory: 256Mi}
          readinessProbe: {httpGet: {path: /ready, port: 8080}}
          env:
            - {name: WORKERS, value: "16"}
            - {name: API_TOKEN, value: should-not-leak}
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata: {name: checkout, namespace: otel-demo}
spec: {minAvailable: 1}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    scanner = KubernetesConfigScanner(
        KubernetesConfig(
            mode="manifest",
            namespace="otel-demo",
            sources=[KubernetesSourceConfig(alias="fixture", path=manifest)],
        )
    )

    result = scanner.scan()
    deployment = scanner.get_resource("Deployment", "checkout")["matches"][0]
    container = deployment["resource"]["containers"][0]

    assert result.resource_count == 2
    assert result.kinds == {"Deployment": 1, "PodDisruptionBudget": 1}
    assert deployment["resource"]["replicas"] == 1
    assert deployment["resource"]["termination_grace_period_seconds"] == 10
    assert container["readiness_probe"]["httpGet"]["path"] == "/ready"
    assert next(item for item in container["env"] if item["name"] == "API_TOKEN")[
        "value"
    ] == "<redacted>"
    assert "should-not-leak" not in json.dumps(deployment)


def test_codegraph_driver_controller_initializes_and_indexes_once(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    status_calls = 0

    def runner(argv, timeout):
        nonlocal status_calls
        calls.append(list(argv))
        if "--version" in argv:
            return "0.9.4\n"
        if "status" in argv:
            status_calls += 1
            return json.dumps(
                {"initialized": False}
                if status_calls == 1
                else {
                    "initialized": True,
                    "fileCount": 1,
                    "nodeCount": 2,
                    "edgeCount": 1,
                    "languages": ["python"],
                    "pendingChanges": {"added": 0, "modified": 0, "removed": 0},
                }
            )
        return ""

    (tmp_path / "app.py").write_text("def app(): return 1\n", encoding="utf-8")
    driver = CodeGraphDriver(
        CodebaseConfig(id="fixture", path=tmp_path, source_identity="fixture-v1"),
        CodeGraphConfig(command="fake-codegraph"),
        runner=runner,
    )

    manifest = driver.ensure_index()

    assert manifest.node_count == 2
    assert manifest.index_sha256
    assert any("init" in call for call in calls)
    assert any("index" in call for call in calls)


def test_evidence_ledger_repairs_only_one_unique_long_prefix() -> None:
    ledger = EvidenceLedger()
    items = ledger.add_codegraph(
        [
            {
                "node": {
                    "kind": "function",
                    "name": "checkout",
                    "qualifiedName": "checkout",
                    "filePath": "checkout.ts",
                    "startLine": 1,
                    "endLine": 4,
                }
            }
        ],
        query="checkout",
    )
    evidence_id = items[0].evidence_id

    assert ledger.resolve_id(evidence_id[:-1]) == evidence_id
    with pytest.raises(ValueError, match="unknown evidence ID"):
        ledger.resolve_id("EV-NOT-A-REAL-ID")


@pytest.mark.skipif(shutil.which("codegraph") is None, reason="CodeGraph CLI unavailable")
def test_real_codegraph_indexes_and_queries_a_wrapped_call(tmp_path: Path) -> None:
    (tmp_path / "client.ts").write_text(
        """
export async function boundedFetch(url: string) {
  return fetch(url, {signal: AbortSignal.timeout(1000)});
}
export async function checkout() { return boundedFetch('/checkout'); }
""".strip()
        + "\n",
        encoding="utf-8",
    )
    driver = CodeGraphDriver(
        CodebaseConfig(id="wrapped", path=tmp_path, source_identity="wrapped-v1"),
        CodeGraphConfig(timeout_seconds=120),
    )

    manifest = driver.ensure_index()
    result = driver.query("boundedFetch")
    callers = driver.callers("boundedFetch")

    assert manifest.node_count >= 2
    assert any(item["node"]["name"] == "boundedFetch" for item in result)
    assert any(item["name"] == "checkout" for item in callers["callers"])


class FakeCodeGraph:
    def ensure_index(self):
        self._manifest = CodeGraphManifest(
            codegraph_version="0.9.4",
            codebase_path="/fixture",
            source_identity="fixture-v1",
            initialized=True,
            file_count=2,
            node_count=3,
            edge_count=1,
            languages=["typescript"],
            index_sha256="a" * 64,
            status={"initialized": True},
        )
        return self._manifest

    @property
    def manifest(self):
        return self._manifest

    def context(self, task, **kwargs):
        return {
            "query": task,
            "nodes": [
                {
                    "kind": "route",
                    "name": "/checkout",
                    "qualifiedName": "checkoutRoute",
                    "filePath": "src/checkout.ts",
                    "startLine": 1,
                    "endLine": 10,
                }
            ],
            "edges": [],
            "codeBlocks": [],
        }

    def query(self, *args, **kwargs):
        return []

    def callers(self, *args, **kwargs):
        return {"callers": []}

    def callees(self, *args, **kwargs):
        return {"callees": []}


class FakeAgents:
    def __init__(self, ledger: EvidenceLedger):
        self.ledger = ledger

    def plan(self, context):
        return (
            CoordinatorPlan(
                plans=[
                    TemplatePlan(
                        template_id=template_id,
                        codegraph_focus=["critical path"],
                        kubernetes_focus=["workload"],
                        priority="medium",
                        rationale="Synthetic complete coverage plan.",
                    )
                    for template_id in ACTIVE_TEMPLATE_IDS
                ]
            ),
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    def analyze(self, template, context):
        if template.template_id != "RD-14":
            return (
                TemplateAgentOutput(
                    template_id=template.template_id,
                    defect_name=template.defect_name,
                    scan_status="not_found",
                    explanation="Synthetic negative result with no complete mechanism chain.",
                ),
                {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            )
        evidence = self.ledger.add_kubernetes(
            {
                "resources": [
                    {
                        "source_alias": "fixture",
                        "path": "k8s.yaml",
                        "resource": {
                            "kind": "Deployment",
                            "name": "checkout",
                            "namespace": "otel-demo",
                        },
                    }
                ]
            }
        )[0]
        evidence_id = evidence.evidence_id
        return (
            TemplateAgentOutput(
                template_id="RD-14",
                defect_name=template.defect_name,
                scan_status="candidate",
                explanation="Synthetic RD-14 candidate result.",
                findings=[
                    TemplateFinding(
                        finding_id="RD-14-F01",
                        proposed_d_class=DClass.D1,
                        evidence_explanation="One critical route is served by one unprotected replica.",
                        evidence_ids=[evidence_id],
                        mechanism_chain=[
                            MechanismStep(
                                order=1,
                                cause="One critical service replica",
                                relation="is removed by",
                                effect="No ready endpoint remains",
                                evidence_ids=[evidence_id],
                            )
                        ],
                        available_fault_types=[
                            FaultType(
                                fault_type="pod-delete",
                                actuator="ChaosBlade pod delete",
                                rationale="A bounded pod deletion distinguishes disruption protection.",
                            )
                        ],
                        fault_injection_target=FaultInjectionTarget(
                            component="checkout",
                            resource_kind="Pod",
                            resource_name="checkout-abc",
                            namespace="otel-demo",
                            selection_basis="The route is mapped to the checkout workload.",
                        ),
                        alternatives_checked=[],
                        locatable=True,
                        injectable=True,
                        oracle_observable=True,
                        cleanup_available=True,
                        confidence_claim=0.52,
                    )
                ],
            ),
            {"input_tokens": 2, "output_tokens": 2, "total_tokens": 4},
        )

    def verify(self, template, context):
        evidence_id = context["candidate"]["evidence_ids"][0]
        return (
            VerificationDecision(
                template_id=template.template_id,
                finding_id=context["candidate"]["finding_id"],
                verdict="confirmed",
                evidence_reproducible=True,
                mechanism_static_support="strong",
                safeguards_excluded=True,
                target_supported=True,
                fault_is_discriminating=True,
                cleanup_supported=True,
                verified_evidence_ids=[evidence_id],
                explanation="Synthetic verifier confirmed every hard evidence gate.",
            ),
            {"input_tokens": 2, "output_tokens": 2, "total_tokens": 4},
        )


def test_langgraph_workflow_emits_fixed_verified_match_contract(tmp_path: Path) -> None:
    source = tmp_path / "k8s.yaml"
    source.write_text(
        """
apiVersion: apps/v1
kind: Deployment
metadata: {name: checkout, namespace: otel-demo}
spec:
  replicas: 1
  template:
    spec:
      containers: [{name: checkout, image: fixture:v1}]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config = load_semantic_scan_config(CONFIG)
    config = config.model_copy(
        update={
            "output_dir": tmp_path / "output",
            "kubernetes": KubernetesConfig(
                mode="manifest",
                namespace="otel-demo",
                sources=[KubernetesSourceConfig(alias="fixture", path=source)],
            ),
        }
    )
    scanner = KubernetesConfigScanner(config.kubernetes)
    ledger = EvidenceLedger()

    report = SemanticScanWorkflow(
        config,
        codegraph=FakeCodeGraph(),
        kubernetes=scanner,
        ledger=ledger,
        agents=FakeAgents(ledger),
    ).run()

    assert report.question_eligible_count == 1
    assert report.matches[0].template_id == "RD-14"
    assert report.matches[0].defect_name == "单副本与中断保护缺失"
    assert report.matches[0].evidence_explanation
    assert report.matches[0].mechanism_chain
    assert report.matches[0].available_fault_types[0].fault_type == "pod-delete"
    assert report.matches[0].fault_injection_target.component == "checkout"
    assert (config.output_dir / report.run_id / "prompt-manifest.json").is_file()
    assert (config.output_dir / report.run_id / "semantic-scan-report.json").is_file()


def test_semantic_workflow_runs_templates_and_verifiers_with_bounded_concurrency(
    tmp_path: Path,
) -> None:
    source = tmp_path / "k8s.yaml"
    source.write_text(
        """
apiVersion: apps/v1
kind: Deployment
metadata: {name: checkout, namespace: otel-demo}
spec:
  replicas: 1
  template:
    spec:
      containers: [{name: checkout, image: fixture:v1}]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    active_templates = ["RD-01", "RD-02", "RD-05", "RD-06"]
    config = load_semantic_scan_config(CONFIG)
    config = config.model_copy(
        update={
            "output_dir": tmp_path / "output",
            "active_template_ids": active_templates,
            "planning_mode": "deterministic",
            "agents": config.agents.model_copy(update={"max_concurrency": 3}),
            "kubernetes": KubernetesConfig(
                mode="manifest",
                namespace="otel-demo",
                sources=[KubernetesSourceConfig(alias="fixture", path=source)],
            ),
        }
    )
    scanner = KubernetesConfigScanner(config.kubernetes)
    ledger = EvidenceLedger()

    class ConcurrentFakeAgents:
        def __init__(self, evidence_ledger: EvidenceLedger):
            self.ledger = evidence_ledger
            self.lock = threading.Lock()
            self.active_analyze = 0
            self.max_analyze = 0
            self.active_verify = 0
            self.max_verify = 0

        def _enter(self, active_attr: str, max_attr: str) -> None:
            with self.lock:
                active = getattr(self, active_attr) + 1
                setattr(self, active_attr, active)
                setattr(self, max_attr, max(getattr(self, max_attr), active))

        def _exit(self, active_attr: str) -> None:
            with self.lock:
                setattr(self, active_attr, getattr(self, active_attr) - 1)

        def analyze(self, template, context):
            self._enter("active_analyze", "max_analyze")
            try:
                time.sleep(0.05)
                evidence = self.ledger.add_kubernetes(
                    {
                        "resources": [
                            {
                                "source_alias": "fixture",
                                "path": "k8s.yaml",
                                "resource": {
                                    "kind": "Deployment",
                                    "name": template.template_id.lower(),
                                    "namespace": "otel-demo",
                                },
                            }
                        ]
                    }
                )[0]
                return (
                    TemplateAgentOutput(
                        template_id=template.template_id,
                        defect_name=template.defect_name,
                        scan_status="candidate",
                        explanation="Synthetic concurrent candidate.",
                        findings=[
                            TemplateFinding(
                                finding_id=f"{template.template_id}-F01",
                                proposed_d_class=(
                                    template.d_class.fixed
                                    or template.d_class.allowed[0]
                                ),
                                evidence_explanation="Synthetic evidence.",
                                evidence_ids=[evidence.evidence_id],
                                mechanism_chain=[
                                    MechanismStep(
                                        order=1,
                                        cause="Synthetic weakness",
                                        relation="can lead to",
                                        effect="synthetic outage",
                                        evidence_ids=[evidence.evidence_id],
                                    )
                                ],
                                confidence_claim=0.5,
                            )
                        ],
                    ),
                    {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                )
            finally:
                self._exit("active_analyze")

        def verify(self, template, context):
            self._enter("active_verify", "max_verify")
            try:
                time.sleep(0.05)
                evidence_id = context["candidate"]["evidence_ids"][0]
                return (
                    VerificationDecision(
                        template_id=template.template_id,
                        finding_id=context["candidate"]["finding_id"],
                        verdict="inconclusive",
                        evidence_reproducible=True,
                        mechanism_static_support="partial",
                        safeguards_excluded=False,
                        target_supported=False,
                        fault_is_discriminating=False,
                        cleanup_supported=False,
                        verified_evidence_ids=[evidence_id],
                        explanation="Synthetic verifier decision.",
                    ),
                    {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                )
            finally:
                self._exit("active_verify")

    agents = ConcurrentFakeAgents(ledger)
    events: list[dict[str, object]] = []
    report = SemanticScanWorkflow(
        config,
        codegraph=FakeCodeGraph(),
        kubernetes=scanner,
        ledger=ledger,
        agents=agents,
        event_sink=events.append,
    ).run()

    assert 1 < agents.max_analyze <= 3
    assert 1 < agents.max_verify <= 3
    assert [item.template_id for item in report.coverage] == active_templates
    first_template_completion = next(
        index
        for index, event in enumerate(events)
        if event["event"] == "template_completed"
    )
    assert (
        sum(
            event["event"] == "template_started"
            for event in events[:first_template_completion]
        )
        <= 3
    )


def test_agent_retry_is_bounded_and_excludes_quota_errors() -> None:
    config = load_semantic_scan_config(CONFIG)
    workflow = object.__new__(SemanticScanWorkflow)
    workflow.config = config
    workflow.run_id = "retry-policy-test"
    events: list[dict[str, object]] = []
    workflow.event_sink = events.append
    workflow._event_lock = threading.Lock()

    class OpenAITimeoutError(RuntimeError):
        pass

    timeout_attempts = 0

    def timeout_once():
        nonlocal timeout_attempts
        timeout_attempts += 1
        if timeout_attempts == 1:
            raise OpenAITimeoutError("transient")
        return "ok", {"total_tokens": 1}

    result = workflow._call_agent_with_retry(
        phase="template", template_id="RD-01", operation=timeout_once
    )

    assert result[0] == "ok"
    assert timeout_attempts == 2
    assert events[0]["event"] == "template_retrying"
    assert events[0]["attempt"] == 2

    class OpenAIRateLimitError(RuntimeError):
        pass

    quota_attempts = 0

    def quota_failure():
        nonlocal quota_attempts
        quota_attempts += 1
        raise OpenAIRateLimitError("insufficient_quota")

    with pytest.raises(OpenAIRateLimitError):
        workflow._call_agent_with_retry(
            phase="template", template_id="RD-02", operation=quota_failure
        )
    assert quota_attempts == 1


def test_provider_structured_output_failure_falls_back_to_tool_strategy() -> None:
    config = load_semantic_scan_config(CONFIG)
    agents = object.__new__(LangChainSemanticAgents)
    agents.config = config.agents
    calls: list[str] = []

    def fake_invoke(agent, context, schema, **kwargs):
        calls.append(agent)
        if agent == "provider":
            raise StructuredOutputValidationError(
                schema.__name__, ValueError("invalid provider JSON"), AIMessage("")
            )
        return "valid", {"total_tokens": 1}

    agents._invoke = fake_invoke

    result = agents._invoke_with_structured_fallback(
        "provider",
        {"task": "RD-09"},
        TemplateAgentOutput,
        fallback_factory=lambda: "tool",
    )

    assert result[0] == "valid"
    assert calls == ["provider", "tool"]


def test_missing_provider_structured_response_falls_back_to_tool_strategy() -> None:
    config = load_semantic_scan_config(CONFIG)
    agents = object.__new__(LangChainSemanticAgents)
    agents.config = config.agents
    calls: list[str] = []

    def fake_invoke(agent, context, schema, **kwargs):
        calls.append(agent)
        if agent == "provider":
            raise SemanticAgentError("agent did not return TemplateAgentOutput")
        return "valid", {"total_tokens": 1}

    agents._invoke = fake_invoke

    result = agents._invoke_with_structured_fallback(
        "provider",
        {"task": "RD-01"},
        TemplateAgentOutput,
        fallback_factory=lambda: "tool",
    )

    assert result[0] == "valid"
    assert calls == ["provider", "tool"]


def _episode_source_report() -> SemanticScanReport:
    evidence = EvidenceRef(
        evidence_id="EV-AAAAAAAAAAAA",
        kind=EvidenceKind.CODEGRAPH_NODE,
        statement="CodeGraph proves the public checkout path reaches CartService.",
        path="checkout.ts",
        start_line=1,
        end_line=14,
        symbol="CartService",
        source_hash="a" * 64,
    )
    match = TemplateMatch(
        template_id="RD-14",
        finding_id="RD-14-F001",
        defect_name="单副本与中断保护缺失",
        d_class=DClass.D1,
        evidence_explanation="One critical cart workload has one declared replica.",
        evidence=[evidence],
        mechanism_chain=[
            MechanismStep(
                order=1,
                cause="One critical cart replica",
                relation="is interrupted by",
                effect="No equivalent service capacity remains",
                evidence_ids=[evidence.evidence_id],
            )
        ],
        available_fault_types=[
            FaultType(
                fault_type="pod-delete",
                actuator="ChaosBlade pod deletion",
                rationale="A single-Pod deletion distinguishes replica protection.",
            )
        ],
        fault_injection_target=FaultInjectionTarget(
            component="cart",
            resource_kind="Deployment",
            resource_name="cart",
            namespace="semantic-fixture",
            selection_basis="Trusted source and workload mappings identify cart.",
        ),
        confidence=0.98,
        confidence_level="high",
        candidate_status="confirmed_candidate",
        verifier_status="confirmed",
        question_eligible=True,
        alternatives_checked=[],
        residual_hypotheses=[],
        provenance={"verified_evidence_ids": [evidence.evidence_id]},
    )
    return SemanticScanReport(
        run_id="semantic-rd14-positive-fixture",
        generated_at="2026-08-24T00:00:00Z",
        codegraph=CodeGraphManifest(
            codegraph_version="0.9.4",
            codebase_path="/fixture",
            source_identity="rd14-positive-v1",
            initialized=True,
            file_count=1,
            node_count=6,
            edge_count=2,
            languages=["typescript"],
            index_sha256="b" * 64,
            status={"initialized": True},
        ),
        kubernetes=KubernetesManifest(
            source_paths=["k8s.yaml"],
            resource_count=3,
            kinds={"Deployment": 1, "Service": 1, "Namespace": 1},
            manifest_sha256="c" * 64,
            authoritative_for_namespace=True,
        ),
        template_registry_version="active-chaosblade-12.v1",
        model={"provider": "fixture"},
        matches=[match],
        coverage=[
            TemplateCoverage(
                template_id="RD-14",
                status="matched",
                explanation="Fixture match confirmed.",
            )
        ],
        question_eligible_count=1,
        limitations=[],
    )


def test_episode_compiler_generates_one_private_and_one_public_task_per_match(
    tmp_path: Path,
) -> None:
    config = load_episode_generation_config(
        REPO_ROOT / "resilience_agent/config/episode-generation.rd14-positive.yaml"
    ).model_copy(update={"output_dir": tmp_path})

    report = EpisodeCompiler(config).compile(_episode_source_report())

    assert report.source_match_count == report.generated_count == 1
    assert report.episode_eligible_count == 1
    assert report.materialized_count == 1
    assert report.one_to_one_verified is True
    episode_root = tmp_path / report.items[0].episode_id
    internal = InternalEpisode.model_validate_json(
        (episode_root / "episode-internal.json").read_text(encoding="utf-8")
    )
    public = PublicEpisodeTask.model_validate_json(
        (episode_root / "episode-public.json").read_text(encoding="utf-8")
    )
    assert internal.main_fault.duration_seconds == 600
    assert internal.main_fault.fault_type == "pod-delete"
    assert internal.main_fault.fault_semantics == "one_shot"
    assert internal.main_fault.duration_semantics == "observation_window"
    assert internal.runtime_binding.pod_name in internal.main_fault.command_template
    assert internal.main_fault.target["pod_uid"] == internal.runtime_binding.pod_uid
    assert "{experiment_uid}" in internal.main_fault.cleanup_command
    assert "disturbances" not in internal.model_dump(mode="json")
    assert len(internal.oracle.gates) == 5
    rendered_public = json.dumps(public.model_dump(mode="json"), ensure_ascii=False)
    assert internal.defect_basis.defect_name not in rendered_public
    assert internal.runtime_binding.pod_uid not in rendered_public
    assert "command_template" not in rendered_public
    assert public.execution_budget["max_experiments"] == 5


def test_episode_compiler_reports_duplicate_findings_without_aborting(
    tmp_path: Path,
) -> None:
    config = load_episode_generation_config(
        REPO_ROOT / "resilience_agent/config/episode-generation.rd14-positive.yaml"
    ).model_copy(update={"output_dir": tmp_path})
    scan = _episode_source_report()
    scan = scan.model_copy(update={"matches": [scan.matches[0], scan.matches[0]]})

    report = EpisodeCompiler(config).compile(scan)

    assert report.source_match_count == 2
    assert report.episode_eligible_count == 1
    assert report.generated_count == 1
    assert report.materialized_count == 1
    assert report.skipped_count == 0
    assert report.items[1].status == "blocked"
    assert report.items[1].skipped_reason == "duplicate_finding"


def test_episode_compiler_skips_findings_without_verified_chaosblade_actuator(
    tmp_path: Path,
) -> None:
    config = load_episode_generation_config(
        REPO_ROOT / "resilience_agent/config/episode-generation.rd14-positive.yaml"
    ).model_copy(update={"output_dir": tmp_path})
    scan = _episode_source_report()
    unsupported = scan.matches[0].model_copy(
        update={
            "available_fault_types": [
                FaultType(
                    fault_type="traffic-spike",
                    actuator="external workload generator",
                    rationale="Not a ChaosBlade primitive in this stage.",
                )
            ]
        }
    )
    scan = scan.model_copy(update={"matches": [unsupported]})

    report = EpisodeCompiler(config).compile(scan)

    assert report.generated_count == 0
    assert report.episode_eligible_count == 0
    assert report.skipped_count == 1
    assert report.items[0].status == "skipped_no_supported_chaosblade_actuator"
    assert report.items[0].skipped_reason == "no_supported_chaosblade_actuator"


def test_episode_compiler_prefers_component_binding_and_caps_codegraph_entrypoints(
    tmp_path: Path,
) -> None:
    config = load_episode_generation_config(
        REPO_ROOT / "resilience_agent/config/episode-generation.rd14-positive.yaml"
    )
    cart_binding = config.runtime_bindings[0]
    checkout_binding = cart_binding.model_copy(
        update={
            "component": "checkout",
            "pod_name": "checkout-7d9f6c8b45-example",
            "pod_uid": "66666666-7777-4888-8999-AAAAAAAAAAAA",
        }
    )
    config = config.model_copy(
        update={
            "output_dir": tmp_path,
            "runtime_bindings": [cart_binding, checkout_binding],
            "codegraph_entrypoints": [f"entry-{index:02d}" for index in range(69)],
        }
    )
    scan = _episode_source_report()
    target = scan.matches[0].fault_injection_target.model_copy(
        update={"resource_name": "checkout"}
    )
    scan = scan.model_copy(
        update={
            "matches": [
                scan.matches[0].model_copy(
                    update={"fault_injection_target": target}
                )
            ]
        }
    )

    report = EpisodeCompiler(config).compile(scan)

    assert report.generated_count == 1
    internal = InternalEpisode.model_validate_json(
        (
            tmp_path
            / report.items[0].episode_id
            / "episode-internal.json"
        ).read_text(encoding="utf-8")
    )
    assert internal.runtime_binding.component == "cart"
    assert len(internal.agent_resources.codegraph.entrypoints) == 50
