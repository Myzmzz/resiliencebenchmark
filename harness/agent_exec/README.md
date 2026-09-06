# Agent execution sidecar

`agent_exec` is the only process-launch boundary between the Stage-2 control
plane and an evaluated Agent CLI.  It is deliberately Linux-only: the daemon
authenticates the Unix peer with `SO_PEERCRED`, the client verifies the daemon's
UID the same way, and the daemon refuses to start unless the Agent UID differs
from both the daemon and Controller identities.

The deployment starts it explicitly, for example:

```text
python -m harness.agent_exec.server \
  --socket /run/resbench/agent-exec.sock \
  --trial-root /trials \
  --cgroup-root /run/resbench-cgroups \
  --memory-max 536870912 --pids-max 64 --cpu-max '100000 100000' \
  --controller-uid 10001 --socket-gid 10001 --agent-uid 10002 --agent-gid 10002 \
  --allow-env OPENAI_BASE_URL --allow-env OPENAI_API_KEY ...
```

The Agent must have no access to the socket directory.  Each request contains
only an allow-listed environment mapping; no daemon environment is inherited.
The requested working directory must be an existing non-symlink path below the
shared Trial root.  On cancellation, timeout, or peer disconnect, the daemon
sends signals to the child's process group and leaves a non-empty cgroup behind
as evidence rather than hiding a containment failure.

Control-plane integration uses:

```python
from harness.agent_exec.client import AgentExecClient, agent_exec_turn_executor

client = AgentExecClient("/run/resbench/agent-exec.sock", expected_server_uid=0)
turn_executor = agent_exec_turn_executor(client)
```

Pass `turn_executor` to `HarnessSession`; the session state machine then sends
both the initial command and every native resume command through the sidecar.
It retains feedback delivery, session-ID capture, retry budgets, and live
records. `agent_exec_streaming_runner` is intentionally only a one-turn helper
and rejects resume-related keyword arguments rather than silently discarding
them. Neither entry point provides a macOS or same-UID fallback: those are not
an execution isolation boundary.
