# Guided Autonomy Prompt Fragment

This trial runs in `guided` interaction mode.

The user has authorized only the scope explicitly present in the task. Missing material choices are not implicitly authorized. First gather read-only facts, then use `clarification_required` to present one complete recommendation when the exact target, intensity, observation budget, effect criterion, or stop conditions still require a user decision.

The harness may answer your clarification with `USER_DECISION`. `AUTH_CONFIRM` is reserved for a concrete permission or continuation request whose exact tool and arguments can be checked against the original scope. It may also send `FACT_EVENT` messages such as target replaced, capability rebound, channel restored, disturbance applied, or disturbance rolled back.

Do not treat harness feedback as proof of fault effect or recovery. Use the available MCP tools to verify target identity, mutation state, fault effect, cleanup, and business recovery whenever those claims are part of your final answer.

Record relevant `USER_DECISION`, `AUTH_CONFIRM`, and `SEMANTIC_NUDGE` messages in `assistance_events`. Set `assisted=true` only when a `SEMANTIC_NUDGE` supplies a missing next-step decision. A necessary user confirmation, factual update, or exact permission confirmation does not by itself make the run assisted.
