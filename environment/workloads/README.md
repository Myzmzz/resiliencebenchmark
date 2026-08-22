# Deterministic Workloads

`deterministic-profiles.yaml` is the common workload and entry-SLO contract for
Train-Ticket, Sock Shop, and OTel Demo. It freezes the seed, traffic mix, load
model, warmup and evaluation windows, evidence artifact, and fault-injection
gate for each application.

The common objective is a minimum 95% successful entry flow and maximum 5%
error rate. Latency is evaluated at p95 against the per-application threshold.
Throughput must remain at least 95% of that application's calibrated healthy
baseline; the ratio is used instead of inventing an absolute RPS before the
10-minute calibration run exists.

Random choices must be derived from the declared seed plus the run and logical
flow slot or user/iteration. A shared process-global random generator is not a
valid reproducibility mechanism because thread scheduling can change selection
order. Every result artifact must record the resolved seed, requested and
observed flow counts, generator image digest, warmup window, measurement window,
and entry-SLO verdict.

Fault injection is fail-closed when the profile is invalid, weights do not sum
to 100, the baseline does not meet the entry SLO, the result artifact is
missing, cleanup fails, or a repeated no-fault run cannot reproduce the traffic
mix and entry metrics within the calibrated tolerance.

## What must exist before a run

Each application needs five runtime objects: a reachable in-cluster entry
Service, the deterministic profile, a digest-pinned generator image, a Bound
results PVC, and (where login is required) a Kubernetes Secret reference. The
Secret value is never rendered into a plan or written to a result artifact.

Apply the three result PVC manifests once. Train-Ticket additionally requires a
runtime workload-user Secret; Sock Shop requires a synthetic user with one
linked address and card. OTel Demo uses no workload credential.

Use the bounded 60-second override only for installation smoke tests:

```bash
python3 scripts/train_ticket_workload.py start \
  --profile baseline \
  --fixture environment/workloads/train-ticket/runtime-fixture.example.yaml \
  --run-id tt-baseline-smoke \
  --duration-seconds 60 \
  --image "$TRAIN_TICKET_WORKLOAD_PIN" \
  --kubeconfig "$KUBECONFIG_PATH" \
  --execute

python3 scripts/locust_workload.py start \
  --application sock-shop \
  --fixture environment/workloads/sock-shop/runtime-fixture.example.yaml \
  --run-id sock-baseline-smoke \
  --duration-seconds 60 \
  --image "$LOCUST_WORKLOAD_PIN" \
  --kubeconfig "$KUBECONFIG_PATH" \
  --execute
```

Replace `sock-shop` with `otel-demo` and its fixture for the third system.
`stop --execute` removes only that run's Job and ConfigMap; result PVCs and
runtime Secrets are retained.

## Calibration gate

The formal baseline is two independent 600-second no-fault runs per
application, not the 60-second smoke. Both runs must use the same seed, traffic
mix, load parameters, generator digest, and entry Service. Both must satisfy at
least 95% success, at most 5% errors, the application p95 threshold, and
complete artifact checks. Only after throughput repeatability is accepted is
the calibrated throughput frozen; a fault run must then retain at least 95% of
that value. Until this gate is complete, result summaries deliberately report
that throughput calibration is required and Chaos execution remains blocked.

For Locust workloads, cumulative statistics are reset 300 seconds into the
600-second run, so the final summary represents the last 300 seconds. Freeze
the median throughput only when both summaries pass their entry SLO and their
throughput spread is at most 10%:

```bash
python3 scripts/calibrate_workload_results.py \
  --application otel-demo \
  --summary run-1/otel-demo-summary.json \
  --summary run-2/otel-demo-summary.json \
  --maximum-throughput-spread-ratio 0.10 \
  --minimum-throughput-ratio 0.95
```
