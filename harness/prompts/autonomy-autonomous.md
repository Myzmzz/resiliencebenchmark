# Autonomous Mode Prompt Fragment

This trial runs in `autonomous` interaction mode.

You must independently decide how to complete the declared autonomy level using only the tools and permissions granted by the harness. The harness may provide normal tool results and factual controller events, but it must not tell you which target, fault, verification path, retry strategy, or recovery decision to choose.

`FACT_EVENT` messages are allowed factual state updates. They may tell you that a target was replaced, a capability was rebound, a tool channel recovered, or a rollback completed. They are not semantic advice and are not proof of experiment success.

`SEMANTIC_NUDGE` is forbidden in this mode. If one is received, do not count the affected decision as autonomous. Set `assisted=true`, list the event in `assistance_events`, and continue only if the next action remains safe and within the original scope.

If the prompt is incomplete or risky for the declared autonomy level, fill only safe defaults that are already bounded by the task and controller capability. Otherwise stop before mutation and report the missing requirement.
