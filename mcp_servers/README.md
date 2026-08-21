# BenchmarkFactory MCP servers

This directory contains the repository-owned MCP servers used by benchmark
agents. They use the official Python MCP SDK v2 and default to `stdio` for local
development. Codex, Claude Code, and DeepSeek Harness trials use authenticated
Streamable HTTP on loopback. BladeAI v0.6.2 verifier compatibility uses three
additional authenticated SSE listeners for the read-only servers only; Chaos
Control is deliberately not exposed through that BladeAI path.

Implemented servers:

- `k8s_ro`: allowlisted, sanitized Kubernetes reads.
- `telemetry_ro`: bounded Prometheus, Jaeger, and Loki reads.
- `source_ro`: read-only access to materialized, commit-locked source trees.
- `chaos_control`: controller-gated ChaosBlade create, inspect, recovery, and
  ledger-owned cleanup.

Run the preparation tests with:

```bash
make sync
make test-mcp
```

Every HTTP process must receive its configuration from a supervisor-owned
environment file. HTTP mode requires a bearer token, issuer/resource URLs, and
an explicit scope; the server binds to loopback by default. Never commit those
runtime values. Use a separate per-Episode MCP token and a single namespace
scope for shared clusters.

`telemetry_ro` defaults to structured queries and does not register arbitrary
PromQL, LogQL, or trace-id lookups in production. Its result filtering is a
second boundary, not a replacement for namespace-scoped upstream credentials or
proxy enforcement. `source_ro` similarly requires exactly one Episode
application at startup. The host units and dry-run-first deployment tooling are
under `environment/mcp/host/`; installing units alone does not qualify an
endpoint.

`chaos_control` is intentionally different from the three read-only servers.
Creation is disabled by default and remains unavailable until its controller
identity, baseline capability ledger, target Pod UID, global ChaosBlade
inventory, action budget, and cleanup handle all pass. Its cleanup ledger must
remain available independently of the tested Agent process.

The XML files under `mcp_servers/evaluations/` are stable tool-quality checks;
they are not benchmark task Ground Truth.
