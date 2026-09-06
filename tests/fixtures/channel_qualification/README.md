This directory contains small sanitized replay fixtures for channel qualification
event adjudication tests.

`base-codex-a2-notice-ack-events.json` was extracted from the A2 Codex base
channel qualification failure. It keeps only the platform-owned fields needed to
validate MCP call/result matching, confirm/consult denial events, notice
delivery, explicit notice acknowledgement, and final result submission. Original
sequence numbers and the `controller_notices` carrier plus later
`harness_poll_notices` acknowledgement relationship are preserved.

Business payloads, node or service addresses, raw stdout, and the full Agent
result were removed. This fixture is a sanitized replay for event judgment; it
is not a simulation of tool execution and is not itself a qualification artifact.
