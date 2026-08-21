# Minimal Complete Intent Prompt

Diagnose the resilience issue in the current benchmark episode and handle it safely.

Use the available MCP tools to gather evidence, perform only controller-approved actions, verify the effect and recovery, and return the structured JSON report required by `prompts/common-task.md`.

Do not access hidden Ground Truth, independent Oracle outputs, credentials, unrestricted shell, or unscoped cluster writes.
