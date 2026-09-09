# Lx implementation status (2026-09-09)

The Lx manual-test facade is implemented in `stage2_service/lx.py` and wired
into the Stage-2 API by `stage2_service/api.py` and `stage2_service/__main__.py`.
It provides immutable prompt-variant snapshots, typed L0--L4 metadata,
controller-bound C0 execution requests, status/interaction/usage/score
projections, filtering and bounded pagination. The execution and recovery path
continues to be the existing Stage-2 task and campaign pipeline.

The gateway callback now keeps the existing metadata-only request receipt and
writes a separate `<trial>.usage.jsonl` file for post-call usage. Each row
contains the request id, source, phase, token fields, cost basis and explicit
`measured`/`estimated`/`unavailable` quality state. Trial finalization copies a
bounded snapshot into `gateway-usage.jsonl`.

The pinned old-cluster gateway image was inspected in the running integration
Pod and reports LiteLLM `1.92.0`. The real `1.92.0` image was then run locally
with an in-container fake provider and no external network. Four protocol forms
(`chat/completions` non-stream and stream, `responses`, and `messages`) were
sent under four Harness identities. The probe produced 16 request receipts and
16 post-call usage rows, preserved the `C1_PLAN` phase header, passed anonymous
and invalid-key rejection checks, and did not expose prompt text or secrets.
This is gateway/protocol evidence only; it is not a live model, fault-effect,
business-recovery, or formal Harness qualification result.

The implementation image was built and pushed as a paired amd64 release:

```text
controller: 1.94.151.57:85/observe/resbench-stage2:stage2-d0-2356fff@sha256:aa0b41a383e723d323cb10bdc908ae95c711cca4eff8ee09a987ab9315e1f07c
agent:      1.94.151.57:85/observe/resbench-stage2:stage2-agent-2356fff@sha256:f1548ba3fbd0b8b19d3fa78be0f30b381517e2fd9f8a4da7dc0949af4baf20f7
```

Those images and the updated callback are deployed to the old-cluster
`resbench-stage2-integration` workload. The live Pod is 3/3 Ready and exposes
the Lx endpoints through the existing service. A read-only API check returned
`stage2-lx-levels.v1` with all five levels and an empty run list. No Lx run or
fault injection was started by this deployment.

The Lx tests and existing targeted Stage-2 tests pass. The repository-wide
suite has three pre-existing fixture assertions related to the separately
changed BladeAI default alias (`gpt-5.6-sol` versus old manual fixtures); those
are independent of the Lx implementation and are retained for explicit repair.
