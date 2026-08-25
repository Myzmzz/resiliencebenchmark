# OTel Demo Episode v1 pairs

Each directory contains exactly two files: `episode-internal.yaml` for the
Controller/Evaluator and `episode-public.yaml` for the tested Agent. The pairs
use the canonical Pydantic models in
`resilience_agent.semantic_scan.episode_contracts`.

| Episode | Main fault | Candidate | Critical path |
| --- | --- | --- | --- |
| `EPI-OTEL-CART-DEADLINE-001` | network delay on `cart` | RD-01 request deadline propagation | cart operations |
| `EPI-OTEL-RECOMMENDATION-FALLBACK-002` | network loss on `product-catalog` | RD-05 breaker/fallback behavior | product browsing and recommendation |
| `EPI-OTEL-CURRENCY-INSTANCE-LOSS-003` | one Pod deletion on `currency` | RD-14 single-instance disruption | browse, cart, and checkout |

The runtime bindings were captured read-only from the `otel-demo` namespace at
`2026-08-24T08:46:23Z`. They expire as soon as the bound Pod UID, readiness,
image, source identity, or environment snapshot changes. The three defect
candidates still require live experiments; a valid file pair is not evidence
that the defect exists or that the Episode has passed live qualification.
