# BladeAI MCP Harness Adapter

The Stage-2 adapter runs BladeAI through its native Session/Turn/SSE service
interface. It does not use the former fixed L4 worker that pre-filled a target,
fault, and recovery policy.

The host-qualified BladeAI `0.7.0` runtime is an MCP client. Its configuration
uses a Claude-style `mcp.json` with BladeAI extensions:

- root key: `mcpServers`;
- transports: `stdio` and `http`;
- the current `http` client connects to an MCP SSE endpoint;
- phase attachment: `clarification`, `phase1`, `phase2`, or `verifier`;
- enable switch: `BLADE_AI_MCP_ENABLED`.

## Static permission profile behavior

Every Trial receives a private `HOME`. The adapter renders the selected static
permission profile to:

```text
$HOME/.blade-ai/mcp.json
```

Only selected MCP servers are written. When no MCP server is selected,
`BLADE_AI_MCP_ENABLED=false` and no MCP token is passed to the BladeAI process.
The configured path environment variable is also set for forward compatibility,
but the standard `HOME` location remains authoritative for the current loader.

The read-only services are attached to both `clarification` and `verifier` so
BladeAI can discover current evidence before planning and verify the outcome
after execution:

- `k8s_ro`;
- `telemetry_ro`;
- `source_ro`.

`chaos_control` remains absent from the BladeAI MCP graph in this phase because
BladeAI retains its native ChaosBlade execution path. That native path is
controlled independently through the Trial's scoped Kubernetes identity and
the `bladeai_native.chaosblade_execute` profile flag.

## Native permission controls

The same static profile independently controls:

- `bladeai_native.kubernetes_read`;
- `bladeai_native.kubernetes_metrics`;
- `bladeai_native.chaosblade_execute`.

The Permission Manager always creates a Trial-specific ServiceAccount and
kubeconfig, but creates only the RoleBindings selected by the profile. This
allows an MCP channel to be removed while retaining BladeAI's legal native
alternative, or vice versa.

## Qualification boundary

The target host has produced real `tool_start` and `tool_end` evidence for the
custom `k8s_ro` and `telemetry_ro` services. That proves client compatibility.
The new Stage-2 Session adapter and all P0-P5 profiles still require the user's
manual integration qualification before they can be described as deployed or
used for formal scores.
