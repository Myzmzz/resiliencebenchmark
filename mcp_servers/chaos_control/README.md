# chaos_control MCP

`chaos_control` exposes safety-gated ChaosBlade tools for benchmark episodes.
The live `ChaosBlade` CRD is treated as cluster-scoped: create, get, list, and
delete never use `kubectl -n`, and generated manifests do not set
`metadata.namespace`. The logical target namespace is encoded in labels and in
the experiment matcher named `namespace`.

Writes are disabled unless `RESBENCH_CHAOS_EXECUTE_ENABLED=true` and all gates
pass:

- explicit `RESBENCH_CHAOS_KUBECONFIG` match;
- namespace allowlist match;
- controller token reference match, never a raw token;
- controller Pod UID match against `RESBENCH_CHAOS_CONTROLLER_POD_UID`, which is
  an identity injected when this MCP process starts;
- optional live controller Pod UID verification when
  `RESBENCH_CHAOS_CONTROLLER_POD_NAMESPACE` and
  `RESBENCH_CHAOS_CONTROLLER_POD_NAME` are also configured;
- controller-owned baseline ledger capability under
  `RESBENCH_CHAOS_BASELINE_LEDGER_DIR`, addressed by SHA256(token), mode `0600`,
  with `passed=true`, exact run/namespace/target/controller fields, and an
  unexpired `expires_at`;
- one-time baseline capability use: a token hash found in any cleanup ledger is
  treated as consumed, so each experiment needs a fresh controller-issued
  baseline capability;
- live target Pod UID read from the Kubernetes API;
- global cluster inventory with no unsafe unowned ChaosBlade resources;
- single active owned experiment and cleanup handle ledger.

The server never stores the baseline token in plaintext. The cleanup ledger only
stores `baseline_gate_token_sha256`.

Create and destroy are serialized by an in-process async lock. This removes
same-process races but is not a distributed lock; run at most one
`chaos_control` MCP instance per benchmark controller. Create writes a pending
cleanup ledger before `kubectl apply` and marks it active only after readback,
so an apply crash still leaves a cleanup handle for recovery.

Agents never provide the kubeconfig path through MCP tool arguments. The path is
only read from the server process environment via `RESBENCH_CHAOS_KUBECONFIG`;
if it is missing, the service fails closed.
