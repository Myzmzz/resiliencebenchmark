# Tasks

本目录用于存放版本化的题目和 Episode 契约，包括 Agent 可见输入、安全约束、实验预算、预期输出以及仅供 Evaluator 使用的 Ground Truth schema。

公开样例与隐藏评测题应物理隔离，避免 Agent 通过读取仓库直接获得答案。

## Directory layout

```text
tasks/
├── catalog/        # Public defect-class catalog and representative DefectSpec entries.
├── contracts/      # Public policy for the runtime-private Evaluator bundle.
├── examples/       # Agent-visible public examples and unlocked smoke cases.
└── schemas/        # Versioned JSON Schema contracts.
```

## Contract split

The task layer separates three objects that are easy to conflate:

- `DefectSpec`: a representative resilience defect class. It describes the latent defect, the fault trigger that can expose it, the failure outcome, observable evidence, recovery expectations, candidate applications, and leakage controls.
- `EpisodePublic`: the Agent-visible assignment. It gives the application snapshot, workload, tool permissions, budget, safety boundary, allowed triggers, observability endpoints, source-code scope, and required final report format. It must not contain the root cause answer.
- `GroundTruth`: the Evaluator-only causal truth. This repository defines the schema only; real hidden instances should be supplied by a private evaluation bundle or service.

## Versioning

All task files use explicit `schema_version` values. Breaking field changes should create a new schema version instead of silently changing existing files. Public examples are allowed to name an unlocked defect class, but production evaluation bundles should keep concrete hidden Ground Truth outside Agent-readable paths.

## Minimum evidence model

Every defect class should identify:

- the `latent_defect`: the design or implementation weakness that exists before any runtime perturbation;
- the `fault_trigger`: the bounded perturbation or workload condition used to expose it;
- the `failure_outcome`: the externally observable SLO or correctness impact;
- the `observable_evidence`: metrics, traces, logs, Kubernetes state, and source-code cues the Agent may use;
- the `recovery`: cleanup and recovery conditions that the Controller and Evaluator can verify;
- the `leakage_controls`: fields that prevent the public task from revealing the hidden answer.
