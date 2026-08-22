# Environment Preparation Status

> Snapshot date: 2026-08-22
> This is environment-preparation evidence, not a benchmark result.

## Repository contracts

- 30 defect classes are present and schema-validated.
- Public Episode, private Evaluator input, Controller plan, Agent result, run trace, Oracle contract, and Oracle observation schemas are present.
- Controller safety checks and independent Evaluator logic have unit tests.
- Repository validation, secret/path scanning, dry-run, and read-only cluster qualification are available.
- All seven current model aliases were probed through the rotated runtime gateway credential. GPT-5.6, DeepSeek V4 Pro, Qwen 3.8 Max, Claude Opus 5, GLM 5.3, and Kimi K2.5 passed every implemented transport check. MiniMax M3 passed alias resolution, Chat Completions, streaming, single-tool, and parallel-tool probes but did not produce valid structured JSON. GLM 4.5 and MiniMax M1 are superseded and no longer present in the active registries. Long-context, tool-error recovery, and hidden-Oracle refusal remain mandatory pre-matrix behavioral checks.
- DeepSeek Harness has an exact top-level package/integrity pin, a complete integrity-bearing npm lock that forces all observed DSH modules to rc.7, an idempotent `npm ci` host installer, and post-install dependency-tree validation. The remote root and `resbench` identities both report `0.1.0-rc.7`; its connectivity smoke passed, but the run trace still omits tool-call and tool-result events and therefore remains ineligible for scoring.
- Prometheus MCP Chart objects are installed with a pinned chart/image and restricted ServiceAccount, but its Deployment is scaled to zero because cluster nodes cannot pull the GHCR image; Harbor mirroring is required before qualification.
- Repository-local MCP implementations now exist for `k8s_ro`, `telemetry_ro`, `source_ro`, and `chaos_control`. Seven hardened loopback systemd units are deployed with per-service Unix identities, Episode-scoped RBAC/kubeconfigs, and 11 verified source locks. All four authenticated HTTP endpoints passed post-recovery qualification. BladeAI v0.6.2 connected all three read-only SSE clients to `verifier`; Chaos control was disabled and unconnected.
- Codex 0.120.0, Claude Code 2.1.197, and DeepSeek Harness 0.1.0-rc.7 completed the bounded connectivity smoke with schema-valid Agent results and all four evidence sources. Codex and Claude emitted inspectable MCP trace events; DeepSeek reported the evidence only in its final answer. A live K8s MCP repair changed namespace lists to bounded resource summaries and verified an OTel Demo Pod response under the size cap. The longer Codex public diagnosis trial exceeded its 300-second budget, so these are transport qualifications rather than scored diagnosis results.
- The current read-only cluster state has all three nodes Ready. Sock Shop is no longer scaled to zero; the remaining cluster-wide fault-execution hard gate is 157 historical cluster-scoped ChaosBlade resources.

## Recovered host incident

- At `2026-08-21T08:50:30Z`, worker `tcse-v100-03` stopped renewing its Node Lease after a recorded `SystemOOM`. The kernel selected an existing `all-in-one-linu` process as the OOM victim; the initial unlocked npm dependency resolution also consumed about 3.4 GiB and increased host pressure.
- The worker recovered at `2026-08-21T09:06:46Z`; SSH, kubelet, and Node Lease are current, and all three nodes are Ready. No benchmark fault was injected, and `RESBENCH_CHAOS_EXECUTE_ENABLED` remained false.
- Post-recovery qualification passed for the MCP host units, 11 source snapshots, DeepSeek runtime, all four authenticated HTTP endpoints, and all seven loopback listeners.
- The DeepSeek installer now uses a complete npm lock, rejects non-rc.7 DSH packages, requires 4 GiB available memory, caps the Node heap at 2 GiB, and lowers npm CPU/I/O priority.

## Source evidence

- Train-Ticket public upstream, OTel Demo 2.2.0, canonical Sock Shop deployment, and eight Sock Shop business component repositories are locked by commit and archive SHA-256.
- Local preparation materialization and offline re-verification passed for all locks.
- The Train-Ticket public upstream is not yet proven identical to the currently deployed Harbor images; a rebuild and digest map remain required.
- Sock Shop database and third-party infrastructure images intentionally have no application-source mapping.

## Live application state

### Train-Ticket

- All 48 Train-Ticket Deployments are Ready after worker recovery.
- `ts-order-service` was restored from 0/2 to 2/2 Ready by a scoped rolling restart.
- The original runtime failure included a full accept queue, thousands of file descriptors, and accumulated `CLOSE_WAIT` connections; recurrence under sustained load is not yet excluded.
- The controller-owned workload image was built from commit `05685b72045d`, pushed to Harbor, and pinned by manifest digest `sha256:1f767740be52f0fd681bc16ef359c86a4989c67c5678d602239a52163a1018e0`. Its runtime account is held only in `train-ticket-workload-user`, and JTL evidence is stored on the Bound `train-ticket-workload-results` PVC.
- A 60-second Gateway-based order smoke completed 60 login, search, contact, preserve, query, and cancellation flows: 360/360 samples succeeded, p50 was 93 ms, p95 was 280 ms, and p99 was 473 ms. This is smoke evidence; the required 10-minute healthy baseline remains pending.
- Nacos replicas had diverged and advertised deleted order, preserve, and Gateway Pod IPs as healthy. A rolling Nacos restart, Distro-initialization wait, three-replica registry equality check, and subsequent Gateway restart removed the stale targets. Replica equality is not yet an automated qualification gate.
- The latest read-only runtime image inventory observed 85 application/init containers; all were Ready or successfully completed and had runtime SHA-256 digests. This does not yet prove the deployed image-to-source mapping.

### OTel Demo

- All 23 OTel Demo Deployments are Ready after worker recovery; the standard load generator is 1/1 Ready.
- Application metrics are visible in shared Prometheus under `otel-demo/*` jobs.
- Recent application traces are visible in shared Jaeger.
- `namespace=otel-demo` log streams are visible in Loki.
- Kubeletstats is incomplete because node serving certificates are expired; node-exporter, cAdvisor, and kube-state-metrics remain available.
- The latest three-application runtime image inventory observed 29 OTel Demo application/init containers; all were Ready or successfully completed and had runtime SHA-256 digests.

### Sock Shop

- All 14 source images were relayed to Harbor for `linux/amd64`, verified by manifest digest, and rendered in the cluster-required `repository:tag@digest` form. Multi-architecture source indexes were resolved to platform manifest digests before pinning.
- All 14 Deployments are Ready. The front-end root, catalogue, tags, and customers paths returned HTTP 200.
- The latest runtime inventory observed 15 Sock Shop containers; every container was Ready or successfully completed and digest-qualified. The combined Train-Ticket, Sock Shop, and OTel Demo inventory qualified 129 containers with no missing runtime digest and no unready container.
- Prometheus namespace series and a recent Loki stream were observed. Sock Shop trace attribution in Jaeger and a repeatable 10-minute load profile remain blocking qualification gaps.

## Shared observability

- Prometheus, Jaeger, Loki, OTel Collector, Promtail, kube-state-metrics, and both node-exporter instances are Ready after worker recovery.
- Four private Harbor images had a non-executable `/wrapper.sh`; the live Deployments now bypass it with verified native binary paths and use `repository:tag@digest` pins. Prometheus, Jaeger, and Loki readiness endpoints return HTTP 200.
- `metrics-server` is temporarily pinned to the recovered worker because another worker could not pull its image. The aggregated API is Available again; Harbor digest mirroring and removal of this node pin remain required.
- Mutable Harbor tags caused different image contents on different nodes for node-exporter and Prometheus. Both workloads now use explicit upstream digest references; runtime versions must be recorded independently of tags.
- Prometheus now uses a protected 20 GiB PVC with 15-day / 15 GiB retention. A one-shot OTLP sentinel remained queryable after a controlled restart.
- Promtail collects `train-ticket`, `sock-shop`, and `otel-demo` pod logs and drops entries outside the Loki retention window.

## Scoped cleanup

- Deleted namespaces: `ai-obs`, `aiops`, `apitest`, `bliyun-ecs`, `chaos-mesh`, `fault-test`, and `xlang-beyla`.
- Deleted 40 `default` namespace objects with `mocker-`, `model-`, or `ts-` names; 19 associated PVs are gone. Declared PVC requests totaled 109.72 GiB.
- Deleted 13 Chaos Mesh Workflows, 13 WorkflowNodes, 44 Chaos Mesh CRDs, their Helm releases, webhooks, and orphan cluster RBAC.
- Three residual default/model NFS directories (about 198 MiB of data) were archived with SHA-256 verification before deletion; all target NFS paths are absent.
- `ischaos`, `observability`, and all 157 historical ChaosBlade resources were intentionally preserved. The cleanup backup is tracked by `runtime-backup://cleanup-20260821T114446Z`; Kubernetes manifests are recoverable, while PVC data not included in the NFS archive is not.

## Blocking safety state

- 157 pre-existing cluster-scoped ChaosBlade resources are owned by the existing fault platform, not this benchmark. They have no Kubernetes owner references or benchmark run IDs and must not be deleted automatically.
- The live CRD is cluster-scoped `chaosblade.io/v1alpha1`; the running operator reports version 1.8.0. Current CR phases are 104 `Running` and 53 `Error`.
- Formal fault execution remains blocked until their owner confirms the supported recovery path and target-side residue is checked.
- The local `chaos_control` MCP service treats ChaosBlade as cluster-scoped, verifies live target Pod UID before create, requires a one-time baseline capability, blocks on unsafe unowned global ChaosBlade state, and deletes only ledger-owned experiments. A durable deadline ledger and independent watchdog enforce `duration_seconds` and retry failed cleanup. Because the 157 historical resources are still present, real fault execution remains blocked.
- Harbor and model-gateway runtime credentials are supplied through root-owned mode-600 files and were used without persisting values in the repository. The model base URL is normalized to `/v1`; reports were scanned to ensure neither the endpoint nor API key was persisted.

## Next completion gates

1. Run and archive the required 10-minute Train-Ticket baseline; add fail-closed Nacos replica/Kubernetes Pod-IP reconciliation to reset qualification.
2. Add a repeatable Sock Shop steady-load profile and prove Jaeger trace attribution for its critical paths.
3. Renew kubelet serving certificates using the cluster distribution runbook.
4. Reconcile the historical ChaosBlade resources with their owning platform.
5. Run long-context, tool-error-recovery, hidden-Oracle-refusal, and representative prompt-budget behavioral checks across the seven-model matrix.
6. Qualify complete Codex/BladeAI/DeepSeek trajectory export, freeze source-to-image mappings, and only then unlock the first 6–10 Episodes.
