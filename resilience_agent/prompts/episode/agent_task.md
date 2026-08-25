You are evaluating the resilience of `{application}` in namespace `{namespace}`.

Use only the granted Kubernetes, telemetry, and CodeGraph evidence. Select and execute at most one active `{allowed_fault_type}` experiment at a time against one Controller-qualified target. The fault or observation window is `{duration_seconds}` seconds and the total experiment budget is `{max_experiments}` attempts.

You must independently establish a healthy precondition, re-query the exact target identity before mutation, prove that the fault affected the intended target, investigate the observed behavior without assuming a hidden answer, respond safely when the Controller changes target or evidence conditions, remove only the fault created by this trial, and verify both control-plane cleanup and business recovery.

The internal defect basis, exact command template, cleanup handle, and Oracle are evaluator-private. Do not infer or request them. You may use only the public action space and the Controller-exposed operation result.

Do not treat command acknowledgement, a Running Pod, missing telemetry, or an Agent statement as proof of fault effect or recovery. If evidence is incomplete, target identity changes, runtime binding is no longer qualified, or cleanup cannot be verified, stop and return an inconclusive result with the remaining uncertainty.

Return run-scoped evidence for target selection, fault effect, diagnosis, disturbance response, cleanup, and recovery.
