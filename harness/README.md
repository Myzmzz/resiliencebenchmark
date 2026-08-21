# Harness

本目录用于接入不同 Agent/模型、暴露受控工具、执行权限检查，并记录可回放的提示、工具调用、观测和最终输出。

Harness 负责传递和记录 Agent 行为，但不负责最终成功判定。

## Boundary

Harness 是 Agent 运行边界，不是 Oracle。它只负责三件事：

1. 根据统一配置启动 BladeAI、Claude Code、Codex 或 DeepSeek Harness。
2. 向 Agent 暴露受控 MCP 工具，并记录完整交互轨迹。
3. 把 Agent 输出交给 `evaluator/`，由独立 Oracle 做最终判定。

Harness 不向 Agent 暴露 Ground Truth、隐藏缺陷标签、独立 Oracle 结果或标准答案。即使 Agent 自称已经修复或定位成功，也只能作为被评测输出，不能作为通过依据。

## Files

```text
harness/
├── harnesses.yaml              # Agent/Harness adapter registry
├── models.yaml                 # LLM alias registry and capability probes
├── mcp-tools.yaml              # MCP tool surface exposed to agents
├── schemas/
│   ├── agent-result.schema.json # Common structured final result
│   └── run-trace.schema.json    # Replayable trial trace contract
└── prompts/
    ├── common-task.md          # Shared task envelope
    ├── full-lifecycle.md       # Positive-control lifecycle prompt
    └── minimal-intent.md       # Minimal complete-intent prompt
```

## Secret Policy

Configuration files in this directory must only contain environment-variable references, credential reference names, or placeholders. Do not commit API keys, SSH passwords, cluster endpoints, Harbor credentials, kubeconfig paths, or local source-code paths.

Expected runtime references:

- `RESBENCH_LLM_BASE_URL`: OpenAI/NewAPI-compatible gateway base URL.
- `RESBENCH_LLM_API_KEY`: gateway credential.
- `RESBENCH_KUBECONFIG_REF`: runtime credential reference for the benchmark kubeconfig.
- `RESBENCH_SOURCE_INDEX_REF`: runtime credential/reference for the service-to-source index.

## Lifecycle

The default lifecycle expected by Harness is:

```text
prepare -> qualify -> baseline -> plan -> execute -> observe -> recover -> report -> cleanup
```

`controller/` owns phase transitions, safety checks, budgets, and cleanup. Harness adapters must stop when the controller revokes a tool, expires a budget, or requests cleanup.
