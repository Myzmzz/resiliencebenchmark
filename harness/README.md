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
├── permission-profiles/        # P0-P5 static permission profiles
├── codex/                      # Trial-local Codex MCP config template
├── claude-code/                # Strict Claude Code MCP config template
├── bladeai/                    # Trial-local BladeAI SSE MCP template
├── deepseek-harness/           # DSH Trial config and Cordis MCP patch
├── runner/                     # Uniform trial launcher contract
├── schemas/
│   ├── agent-result.schema.json # Common structured final result
│   ├── permission-profile.schema.json # Static permission profile
│   └── run-trace.schema.json    # Replayable trial trace contract
└── prompts/
    ├── common-task.md          # Shared task envelope
    ├── connectivity-smoke.md   # Bounded read-only Harness qualification
    ├── full-lifecycle.md       # Positive-control lifecycle prompt
    └── minimal-intent.md       # Minimal complete-intent prompt
```

## Secret Policy

Configuration files in this directory must only contain environment-variable references, credential reference names, or placeholders. Do not commit API keys, SSH passwords, cluster endpoints, Harbor credentials, kubeconfig paths, or local source-code paths.

Expected runtime references:

- `RESBENCH_LLM_BASE_URL`: OpenAI/NewAPI-compatible gateway base URL.
- `RESBENCH_LLM_API_KEY`: gateway credential.
- `RESBENCH_K8S_MCP_URL`, `RESBENCH_TELEMETRY_MCP_URL`, `RESBENCH_SOURCE_MCP_URL`, `RESBENCH_CHAOS_CONTROL_MCP_URL`: per-trial loopback MCP endpoints.
- `RESBENCH_MCP_TOKEN`: per-trial bearer token; templates reference its variable name and never store its value.

## Lifecycle

The default lifecycle expected by Harness is:

```text
prepare -> qualify -> baseline -> plan -> execute -> observe -> recover -> report -> cleanup
```

`controller/` owns phase transitions, safety checks, budgets, and cleanup. Harness adapters must stop when the controller revokes a tool, expires a budget, or requests cleanup.

## Static permission profiles

Stage-2 Campaign requests carry one `permission_profile`. The built-in P0-P5
profiles live under `permission-profiles/` and are also available from:

```text
GET /api/v1/permission-profiles
```

The selected profile is applied before a Trial process starts:

- Codex receives a filtered Trial-local `config.toml`;
- Claude Code receives a filtered MCP JSON and matching `--allowedTools`;
- DeepSeek Harness receives a filtered Trial-local Cordis patch;
- BladeAI receives a Trial-local `$HOME/.blade-ai/mcp.json` plus independently
  scoped native Kubernetes permissions.

`p5-no-mcp` intentionally supplies no MCP token or endpoint to the Agent. A
disabled MCP Server is omitted from the rendered configuration rather than
merely described as disabled in the prompt.
