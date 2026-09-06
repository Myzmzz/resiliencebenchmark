# AgentExec AppArmor prerequisite

Kubernetes 1.28 selects `localhost/resbench-agent-runtime` only for the trusted
`agent-runtime` container using the Pod annotation. Install this profile on
each eligible node **before** scheduling that workload. Current live scope is
only old-cluster `tcse-v100-03`; the other nodes have not been qualified.

The profile retains Docker's proc/sys denial patterns and implicit denial of
unspecified mount operations. Its four allowed operations match the initializer:
private mount propagation, recursive read-only mount-tree attributes, bind of a sandbox temporary
directory, and writable remount of that bound temporary directory. Only the
root initializer holds mount capabilities; guest UID 10003 loses its capability
set before code executes. No AppArmor `unconfined` or complain-mode setting is
used. This asset does not modify the system-wide `docker-default` profile.

On the approved old node, place this exact file at
`/etc/apparmor.d/resbench-agent-runtime`, load it with
`apparmor_parser -r /etc/apparmor.d/resbench-agent-runtime`, then verify its entry
in `/sys/kernel/security/apparmor/profiles` ends in `(enforce)`. Keep copies of
previous project profiles when updating an existing installation. Installation
is not sandbox qualification: subsequently run the real UID/network/IPC checks.

The live failure that motivated this prerequisite was EACCES on privatizing
mount propagation under `docker-default`. The default profile explicitly denies
mount operations; no kernel denial log was available, so the final causal check
is rerunning the unchanged initializer under this constrained profile.

The initializer uses `mount_setattr(AT_RECURSIVE, MOUNT_ATTR_RDONLY)` before
binding back the one writable temporary directory. A read-only image root alone
does not make nested `emptyDir`/tmpfs mounts read-only. This requires Linux 5.12+
and the pinned libc wrapper; missing support is fatal, not a reason to use the
previous incomplete root-only remount. The old Linux 5.15 node has been checked.

References: [Docker 26.1.3 profile](https://raw.githubusercontent.com/moby/moby/v26.1.3/profiles/apparmor/template.go),
[AppArmor mount rule syntax](https://manpages.ubuntu.com/manpages/focal/man5/apparmor.d.5.html).
