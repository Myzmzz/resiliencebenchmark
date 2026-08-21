# telemetry_ro MCP

`telemetry_ro` exposes read-only Prometheus, Jaeger, and Loki tools for bounded
BenchmarkFactory episode windows. Endpoint URLs are runtime configuration only;
agents cannot pass upstream URLs as tool arguments.

Required runtime scope:

- `RESBENCH_TELEMETRY_ALLOWED_NAMESPACES`: comma or whitespace separated
  Kubernetes namespaces for Prometheus and Loki result filtering.
- `RESBENCH_JAEGER_ALLOWED_SERVICES`: comma or whitespace separated Jaeger
  service names for trace filtering.

Prometheus and Loki result tools apply a fail-closed post-filter. A returned
series or stream must carry one of these labels with an allowlisted value:

- `namespace`
- `kubernetes_namespace`
- `exported_namespace`

Items without one of those labels are removed and counted in `scopedOutCount`.
This means agent-facing PromQL and LogQL should preserve the namespace label in
aggregations, for example by grouping with the namespace label when aggregating.

This post-filter is a guardrail against agent misreading and accidental
cross-namespace evidence exposure. It is not hard tenant isolation for arbitrary
PromQL, LogQL, or trace queries. In a production shared cluster, each benchmark
Episode must use a single namespace allowlist and the upstream telemetry endpoint
must point to a proxy, tenant, or query layer that enforces the same label scope
before data reaches this MCP server. If that upstream scope is absent, the
qualification check must not claim hard isolation.

All returned payloads are recursively redacted before leaving the MCP server.
Fields whose key names look credential-like are replaced with `<redacted>`, and
common credential-looking strings such as bearer headers, model-key prefixes,
and credential assignments are conservatively redacted.
