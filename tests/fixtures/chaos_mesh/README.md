# Real Chaos Mesh observation

`network_delay_injected.json` was captured on the approved old cluster on
2026-09-06, run `resbench-mesh-canary-20260906-a1`, from the exact temporary
source Pod `9734b453-6db5-4200-a54d-d386ca204fae`. It retains the actual manifest,
target labels and status; Kubernetes managedFields/finalizer bookkeeping was
omitted. No credentials, user prompt or model response is present.

The physical probe observed median HTTP latency rising from 1.022ms to
2001.251ms under configured 1000ms egress delay, then returning to 0.913ms.
Its source report is `artifacts/remediation/20260905/chaos-mesh-canary-a1/`.
Both canary Pods and all fault objects were subsequently removed.

The key contract is `desiredPhase: Run` versus actual
`AllInjected=True` and `containerRecords[].phase=Injected`. Desired state alone
must never generate a Running lifecycle fact. Negative variants in the unit
tests are explicitly synthetic mutations of this real observation, not other
live experiments. StressChaos/PodChaos are not live-qualified by this fixture.
