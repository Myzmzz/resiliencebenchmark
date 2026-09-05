# telemetry_ro MCP

`telemetry_ro` exposes read-only Prometheus, Jaeger, Loki, and benchmark workload tools for bounded
BenchmarkFactory episode windows. Endpoint URLs are runtime configuration only;
agents cannot pass upstream URLs as tool arguments.

Required runtime scope:

- `RESBENCH_TELEMETRY_ALLOWED_NAMESPACES`: exactly one Kubernetes namespace for
  the current BenchmarkFactory Episode. A telemetry_ro runtime must not span
  multiple namespaces.
- `RESBENCH_JAEGER_ALLOWED_SERVICES`: comma or whitespace separated Jaeger
  service names for trace filtering.
- `RESBENCH_TELEMETRY_ALLOW_RAW_QUERIES`: optional development switch. Raw
  arbitrary PromQL/LogQL tools and direct Jaeger trace-id lookup are hidden and
  rejected by default. Set to `true` only for explicit qualification or
  debugging runs; those tools are unqualified for shared-cluster production use.
- `RESBENCH_WORKLOAD_STATS_URL`: Controller-configured Locust request-statistics
  endpoint. Agents never provide or receive this URL.
- `RESBENCH_WORKLOAD_STAT_NAME`: the single workload row exposed to the current
  Episode, such as `/api/cart`.

`telemetry_workload_current` returns the scoped workload's raw request count,
failure count, response-time sum, average/P95 latency, RPS, and success rate.
It does not reveal the independent Oracle threshold or verdict. A zero-request
snapshot is returned as `sample_status=insufficient`, not as a business failure.

Production-default Prometheus and Loki tools are structured. Agents provide a
metric name, exact non-namespace label filters, optional `rate`/`increase`
transform, allowlisted `group_by`, or a bounded literal Loki `contains` string.
The server constructs the actual PromQL/LogQL and injects
`namespace="<episode namespace>"`. Namespace labels are reserved; callers cannot
override them or pass arbitrary query strings in strict mode.

Returned Prometheus series and Loki streams still apply a fail-closed
post-filter. A returned item must carry one of these labels with the configured
Episode namespace:

- `namespace`
- `kubernetes_namespace`
- `exported_namespace`

Items without one of those labels are removed. In strict mode, responses expose
only `scopeFiltered: true` or `false`; they do not expose the number of filtered
items, because that count can become a shared-cluster side channel. Raw
development mode may include diagnostic filtered counts.

Jaeger `find_traces` is the default trace retrieval path and returns only traces
whose discovered service names are within the configured allowlist. Direct
`get_trace(trace_id)` is a raw development tool only, because arbitrary known
trace ids can otherwise be used as an out-of-scope trace existence probe.

The structured tools define the repository-level hard boundary for agent-facing
queries in this BenchmarkFactory codebase: arbitrary PromQL/LogQL is not exposed
by default, namespace is server-owned, query windows and result sizes are
bounded, and payloads are redacted. True multi-tenant isolation still needs
defense in depth upstream: a Prometheus/Loki tenant, proxy, or query layer must
enforce the same namespace scope before data reaches this MCP server. If that
upstream scope is absent, the qualification check must not claim infrastructure
tenant isolation.

All returned payloads are recursively redacted before leaving the MCP server.
Fields whose key names look credential-like are replaced with `<redacted>`, and
common credential-looking strings such as bearer headers, model-key prefixes,
and credential assignments are conservatively redacted.

## Controller-owned disturbance hook

`TelemetryROService(..., disturbance_hook=...)` accepts the optional
`TelemetryDisturbanceRuleEngine` used by multi-level benchmark runs. The hook
can fail deterministic metric-range call slots or remove deterministic matrix
points before the response reaches the Agent. It is not exposed as an
Agent-visible MCP tool: rule registration/removal remains a Controller-only
control path, and every rule/hit/removal must be copied into
`controller_record` by the runtime adapter.

Direct service composition raises `TelemetryInjectedFailure`; an HTTP reverse
proxy may map its `http_status=503` or `mode=timeout` to transport behavior.
Without that proxy mapping, this is an MCP tool failure rather than proof that
an actual HTTP 503 was emitted. Production qualification must test the deployed
transport, not only the in-process rule engine.
