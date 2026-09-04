# Minimal Complete Intent Prompt

Complete the user-requested resilience experiment safely inside the published target, tool, and safety boundaries.

Decide which experiment duties are necessary for the user's request. Use only controller-approved actions and return the structured JSON report required by `prompts/common-task.md`. Do not assume that a successful command proves an experiment conclusion.

Do not access hidden Ground Truth, independent Oracle outputs, credentials, unrestricted shell, or unscoped cluster writes.
