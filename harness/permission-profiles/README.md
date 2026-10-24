# Static permission profiles

These profiles are the first permission-control stage. They are applied before
an Agent process starts; they do not perform a runtime permission disturbance.

The Stage-2 service exposes the installed profiles at:

```text
GET /api/v1/permission-profiles
```

To use one, copy the selected object into the Campaign request's
`permission_profile` field. The full resolved profile is retained in the
Campaign request artifact, and each Trial writes `permission-surface.json` with
the effective Agent-specific surface.

First-stage manual qualification should set:

```yaml
trial_kinds: [control]
```

This runs only the selected initial permission profile. Target-change and
runtime permission disturbances remain outside the first-stage qualification.
It does not make the selected Episode read-only: a Control Trial still executes
the Episode's normal Agent-owned lifecycle. Use a separately approved read-only
Episode when the manual qualification must not create a fault.

BladeAI intentionally compiles `chaos_control` to its native, RBAC-controlled
ChaosBlade path during this phase. Its effective MCP surface therefore contains
only the selected read-only servers. Codex, Claude Code, and DeepSeek Harness
receive the selected `chaos_control` MCP server directly.

Profile intent:

- `p0-full-authorized`: current full controlled surface;
- `p1-no-telemetry-mcp`: remove the telemetry MCP channel;
- `p2-no-k8s-mcp`: remove the Kubernetes read-only MCP channel;
- `p3-read-only`: remove fault execution;
- `p4-no-native-k8s-metrics`: keep telemetry MCP while removing BladeAI's
  native Metrics API path;
- `p5-no-mcp`: preserve only BladeAI's selected native capabilities; the other
  formal Harnesses receive no tools.

Manual integration qualification must verify the actual startup tool list and
at least one real allowed/denied call for every selected profile. Unit tests only
prove contract compilation and adapter rendering.

The execution environment must provide the four native launchers on the formal
runner path. BladeAI additionally requires `STAGE2_BLADEAI_PYTHON` and, whenever
`bladeai_native.chaosblade_execute=true`, `STAGE2_BLADEAI_CHAOSBLADE_ROOT`.
Missing environment-owned runtimes are a qualification failure, not an Agent
result.
