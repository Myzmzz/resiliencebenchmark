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
