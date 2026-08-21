# BladeAI MCP Harness Adapter

This adapter targets BladeAI v0.6.2 as an MCP client. The verified local source
parses a Claude-style `mcp.json` with BladeAI extensions:

- root key: `mcpServers`
- transports: `stdio` and `http`
- `http` implementation: MCP SDK `sse_client`
- phase attachment: `clarification`, `phase1`, `phase2`, `verifier`
- enable switch: `BLADE_AI_MCP_ENABLED`
- declared config path switch: `BLADE_AI_MCP_CONFIG_PATH`

The important boundary is transport. BladeAI v0.6.2's `http` client uses the
MCP SDK SSE client. The benchmark host runtime prepares native authenticated SSE
listeners for BladeAI on loopback ports `18181-18183`. Live qualification
connected all three read-only clients to `verifier`; `chaos_control` remained
disabled and unconnected.

## Template

`mcp.json.template` wires the read-only benchmark MCP services:

- `k8s_ro`
- `telemetry_ro`
- `source_ro`

Each server uses `transport: "http"`, a host-native SSE URL supplied by runtime
rendering, and `Authorization: Bearer ${RESBENCH_MCP_TOKEN}`. The template
attaches these tools only to `verifier`. This is conservative for v0.6.2 because
BladeAI's target guard does not have a stable way to classify arbitrary dynamic
MCP tools as read-only during planning or execution.

`chaos_control` is present but disabled. ChaosBlade write actions for benchmark
runs must be performed by the external benchmark controller after the controller
budget token, target Pod UID, baseline capability, global ChaosBlade inventory,
and cleanup handle gates pass.

## Qualification

For a trial-local BladeAI run:

1. Render `mcp.json.template` with runtime-only environment variables.
2. Put the rendered file at `~/.blade-ai/mcp.json`.
3. Set `BLADE_AI_MCP_ENABLED=true`.
4. Set `BLADE_AI_MCP_CONFIG_PATH` to the rendered path for forward
   compatibility, but do not rely on it alone for v0.6.2.
5. Render the read-only URLs to the host-native authenticated SSE listeners on
   loopback ports `18181-18183`.
6. Start BladeAI and verify logs show MCP connections for `k8s_ro`,
   `telemetry_ro`, and `source_ro`.
7. Verify no BladeAI-visible `chaos_control` tools are connected.

The live host passed these read-only checks with BladeAI v0.6.2. The timed
server smoke emitted a non-fatal cancel-scope warning while disconnecting after
successful startup; scored trials still need complete BladeAI trajectory export.
