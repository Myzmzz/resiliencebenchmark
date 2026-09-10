# Controlled code sandbox

`run_python` is intentionally not a host-process convenience tool.  Its MCP
service only validates source and writes a `SANDBOX_RUN` ledger event; execution
must be supplied by a Linux `SandboxExecutor` backed by `agent_exec` sandbox
mode.  When that executor is absent or cannot establish Linux mount/network
namespaces and cgroup containment, it returns an unavailable error rather than
running source in the MCP or Controller process.

Sandbox code reaches approved data only through `SandboxBroker`.  The guest
sends exactly `{type: "tool_call", tool, args}` on a dedicated Unix socket. The
broker has a per-Trial allowlist and invokes a control-plane-owned MCP client;
that client must target the normal PolicyGate-protected service. URLs, bearer
tokens, filesystem paths, environment mappings, LLM credentials, and trial
identifiers are not tool parameters and are never sent into the guest.

Deployment preconditions are strict: an agent-runtime root daemon, distinct
controller/agent/sandbox UIDs, cgroup v2 delegation, a root-owned agent-exec
socket directory inaccessible to the sandbox UID, and a pre-created writable
`.sandbox-tmp` directory for each sandbox work directory. The sandbox creates a
new network namespace (so loopback and Stage2 HTTP are unavailable) and remounts
the runtime root read-only before dropping privileges. Lack of any prerequisite
is a failed launch, not a host fallback.
