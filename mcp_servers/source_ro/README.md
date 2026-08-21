# source_ro MCP server

`source_ro` exposes read-only access to materialized source snapshots from
`environment/shared/source-locks.yaml`.

Runtime scope is fail-closed. Every episode must set exactly one application:

```bash
export RESBENCH_SOURCE_ROOT=/path/to/benchmark-sources/materialized
export RESBENCH_SOURCE_ALLOWED_APPLICATIONS=train-ticket
```

Use `RESBENCH_SOURCE_ALLOWED_REPOSITORIES` only to narrow that application to a
subset of lock ids:

```bash
export RESBENCH_SOURCE_ALLOWED_APPLICATIONS=sock-shop
export RESBENCH_SOURCE_ALLOWED_REPOSITORIES=sock-shop-orders-0.4.7,sock-shop-carts-0.4.8
```

Do not run one episode with multiple applications. A Train-Ticket episode must
not be able to list or directly read Sock Shop or OpenTelemetry Demo source.
Out-of-scope `repo_id` calls return a structured `repository_out_of_scope`
error through the MCP tool result.

Transport:

- Default: stdio, selected when `RESBENCH_MCP_TRANSPORT` is unset.
- HTTP: set `RESBENCH_MCP_TRANSPORT=streamable-http`; authentication is handled
  by the shared `mcp_servers.http_runtime` helper.
