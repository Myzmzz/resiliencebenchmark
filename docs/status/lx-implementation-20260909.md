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
controller: 1.94.151.57:85/observe/resbench-stage2:stage2-d0-120a60b@sha256:33cf5dc36df99acb7d82a4c69e854d02a41c1513308b0566a439f80795f0fc78
agent:      1.94.151.57:85/observe/resbench-stage2:stage2-agent-120a60b@sha256:c452ddaba41173dd51474fc59ca20626b280deb716e5beeacf95c4fe2c70c77e
```

Those images and the updated callback are deployed to the old-cluster
`resbench-stage2-integration` workload. The live Pod is 3/3 Ready and exposes
the Lx endpoints through the existing service. A read-only API check returned
`stage2-lx-levels.v1` with all five levels and an empty run list. No Lx run or
fault injection was started by this deployment.

After the final rollout, the normal asynchronous model preflight completed at
`2026-09-09T23:02:27Z` with `gateway_probe.status=complete` and all eight
configured gateway aliases available. The Stage-2 options endpoint reports all
four Harnesses runnable for the listed aliases. This confirms model/gateway
readiness only; it does not claim that an Lx fault Trial has been executed.

The Lx tests and existing targeted Stage-2 tests pass. The repository-wide
suite has three pre-existing fixture assertions related to the separately
changed BladeAI default alias (`gpt-5.6-sol` versus old manual fixtures); those
are independent of the Lx implementation and are retained for explicit repair.
