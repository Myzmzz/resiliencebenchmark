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

## Deterministic entry workloads

- One public contract now freezes application-specific random seeds, business-flow mixes, load models, 600-second runtime, result artifacts, and entry SLOs. The common gate is at least 95% success, at most 5% errors, application-specific p95 latency, and at least 95% of the calibrated healthy throughput.
- Train-Ticket uses seed `2026082201` and a 60/10/30 search/login/order mix. Sock Shop uses seed `2026082202` and a 50/20/15/15 catalogue/view-cart/add-cart/checkout mix. OTel Demo uses seed `2026082203` and a 74/19/7 browse/cart/checkout mix.
- All three controller-owned generators passed bounded 60-second business smoke tests and retained evidence on application-scoped PVCs. These runs prove executability and path coverage; they do not freeze throughput.
- OTel Demo formal calibration is complete: two independent 600-second runs evaluated only their final 300-second windows. Median healthy throughput is 7.621063 RPS, the frozen 95% floor is 7.240010 RPS, and throughput spread was 0.074%. Train-Ticket and Sock Shop remain uncalibrated while inactive.

## Active application selection

- OTel Demo is the only active test application. Its 22 business Deployments are Ready; the built-in load generator is intentionally 0 so controller-owned deterministic Jobs are the only load source.
- All 48 Train-Ticket Deployments, 3 Train-Ticket StatefulSets, and 14 Sock Shop Deployments are intentionally scaled to 0. Their namespaces, Services, PVCs, Secrets, application data, and original replica counts are retained; no application data was deleted.
- The active-system marker is `otel-demo/resbench-active-system`. Inactive controllers carry `resiliencebenchmark.io/standby-replicas`, making the selection reversible without guessing replica counts.

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

### Train-Ticket (inactive standby)

- All 48 Deployments and 3 StatefulSets are currently scaled to zero because OTel Demo is selected. Their earlier Ready and workload-smoke evidence below remains historical qualification evidence, not current activity.
- `ts-order-service` was restored from 0/2 to 2/2 Ready by a scoped rolling restart.
- The original runtime failure included a full accept queue, thousands of file descriptors, and accumulated `CLOSE_WAIT` connections; recurrence under sustained load is not yet excluded.
- The controller-owned workload image was built from commit `05685b72045d`, pushed to Harbor, and pinned by manifest digest `sha256:1f767740be52f0fd681bc16ef359c86a4989c67c5678d602239a52163a1018e0`. Its runtime account is held only in `train-ticket-workload-user`, and JTL evidence is stored on the Bound `train-ticket-workload-results` PVC.
- The mixed baseline generator was subsequently rebuilt from commit `1320c15bef95` and pinned by manifest digest `sha256:4be3b6e41094a8fbd05b580162f876825d413663372bd422b27d1a705d680397`.
- A 60-second Gateway-based order smoke completed 60 login, search, contact, preserve, query, and cancellation flows: 360/360 samples succeeded, p50 was 93 ms, p95 was 280 ms, and p99 was 473 ms. This is smoke evidence; the required 10-minute healthy baseline remains pending.
- A separate 60-second deterministic mixed smoke executed 60 business flows and 155 entry requests across search, login, and order paths: all samples succeeded and entry p95 was 269 ms. The first 60 slots contained 38 search, 3 login, and 19 order flows; the declared exact mix is guaranteed over each complete 100-slot schedule and will be checked in the 600-second calibration.
- Nacos replicas had diverged and advertised deleted order, preserve, and Gateway Pod IPs as healthy. A rolling Nacos restart, Distro-initialization wait, three-replica registry equality check, and subsequent Gateway restart removed the stale targets. Replica equality is not yet an automated qualification gate.
- The latest read-only runtime image inventory observed 85 application/init containers; all were Ready or successfully completed and had runtime SHA-256 digests. This does not yet prove the deployed image-to-source mapping.

### OTel Demo (selected active system)

- All 22 OTel Demo business Deployments are Ready; the standard load generator is intentionally scaled to 0 during controlled benchmark operation.
- Application metrics are visible in shared Prometheus under `otel-demo/*` jobs.
- Recent application traces are visible in shared Jaeger.
- `namespace=otel-demo` log streams are visible in Loki.
- Kubeletstats is incomplete because node serving certificates are expired; node-exporter, cAdvisor, and kube-state-metrics remain available.
- The latest three-application runtime image inventory observed 29 OTel Demo application/init containers; all were Ready or successfully completed and had runtime SHA-256 digests.
- The earlier fixed-seed OTel smoke recorded 474 requests, 0 failures, 110 ms entry p95, and 8.023 requests/s. Its smoke-only calibration-pending state is superseded by the formal calibration below.
- Two formal calibration windows each recorded 2280 requests and 0 failures. Their throughputs were 7.618242 and 7.623884 RPS, p95 values were 37 and 38 ms, and the resulting 95% throughput floor is 7.240010 RPS. Run CSVs, summaries, hashes, and the calibration artifact are retained under the PVC `calibration/` directory.
- Post-calibration read-only checks found 6,980 OTel Demo Prometheus series, seven matching Jaeger application services with a recent frontend trace, and nine OTel Demo Loki series.

### Sock Shop (inactive standby)

- All 14 source images were relayed to Harbor for `linux/amd64`, verified by manifest digest, and rendered in the cluster-required `repository:tag@digest` form. Multi-architecture source indexes were resolved to platform manifest digests before pinning.
- All 14 Deployments are currently scaled to zero because OTel Demo is selected. Their earlier Ready and business-smoke evidence below remains historical qualification evidence, not current activity.
- The latest runtime inventory observed 15 Sock Shop containers; every container was Ready or successfully completed and digest-qualified. The combined Train-Ticket, Sock Shop, and OTel Demo inventory qualified 129 containers with no missing runtime digest and no unready container.
- Prometheus namespace series and a recent Loki stream were observed. Sock Shop trace attribution in Jaeger and the two-run 10-minute throughput calibration remain blocking qualification gaps.
- Redis snapshot persistence was disabled for the ephemeral session store, and `carts-db` plus `orders-db` were pinned to the Harbor-verified MongoDB 3.4.24 linux/amd64 manifest required by the archived legacy drivers. The synthetic workload user now has one linked address and card.
- The fixed-seed Sock Shop workload smoke exercised catalogue, cart, and checkout paths and recorded 360 samples, 0 failures, 310 ms entry p95, and 6.0965 requests/s. Its structured summary remains on the `sock-shop-workload-results` PVC and explicitly marks throughput calibration as pending.

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

1. Keep OTel Demo as the only active test application and use the frozen 7.240010 RPS throughput floor for its fault Episodes; calibrate Train-Ticket or Sock Shop only when one of them is explicitly selected later.
2. Prove Sock Shop Jaeger attribution for front-end, catalogue, cart, order, payment, and shipping paths under the deterministic workload.
3. Renew kubelet serving certificates using the cluster distribution runbook.
4. Reconcile the historical ChaosBlade resources with their owning platform.
5. Run long-context, tool-error-recovery, hidden-Oracle-refusal, and representative prompt-budget behavioral checks across the seven-model matrix.
6. Qualify complete Codex/BladeAI/DeepSeek trajectory export, freeze source-to-image mappings, and only then unlock the first 6–10 Episodes.
