# BenchmarkFactory MCP servers

This directory contains the repository-owned MCP servers used by benchmark
agents. They use the official Python MCP SDK v2 and default to `stdio` for local
development. Harness trials use authenticated Streamable HTTP on loopback.

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

`chaos_control` is intentionally different from the three read-only servers.
Creation is disabled by default and remains unavailable until its controller
identity, baseline capability ledger, target Pod UID, global ChaosBlade
inventory, action budget, and cleanup handle all pass. Its cleanup ledger must
remain available independently of the tested Agent process.

The XML files under `mcp_servers/evaluations/` are stable tool-quality checks;
they are not benchmark task Ground Truth.
