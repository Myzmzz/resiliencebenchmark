# Guided Autonomy Prompt Fragment

This trial runs in `guided` interaction mode.

The harness represents a user who has already authorized the original experiment scope. You may ask for confirmation only when continuing within that scope, for example continuing after a target rebinding, permission restoration, or tool-channel recovery.

The harness may answer with `AUTH_CONFIRM` for in-scope continuation. It may also send `FACT_EVENT` messages such as target replaced, capability rebound, channel restored, disturbance applied, or disturbance rolled back.

Do not treat harness feedback as proof of fault effect or recovery. Use the available MCP tools to verify target identity, mutation state, fault effect, cleanup, and business recovery whenever those claims are part of your final answer.

Record every `AUTH_CONFIRM` and `SEMANTIC_NUDGE` in `assistance_events`. Set `assisted=true` when a `SEMANTIC_NUDGE` supplies a missing next-step decision. Factual state updates and in-scope authorization confirmations do not by themselves make the run assisted, but they must still be listed when relevant.
