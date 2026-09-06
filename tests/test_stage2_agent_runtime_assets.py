"""Static contracts for WP1 agent-runtime deployment assets.

These assertions deliberately do not claim a Linux namespace/cgroup or CNI
qualification.  They prevent the manifest and image boundary from silently
regressing before that separate environment test is run.
"""

from __future__ import annotations

from pathlib import Path
import subprocess

import yaml

import harness.agent_exec.__main__ as agent_entrypoint
from harness.agent_exec.environment import AGENT_ENV_ALLOWLIST


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy/stage2"
TRIAL_ROOT = "/var/lib/resbench-stage2/agent-trials"


def _pod_specs(path: Path):
    documents = [item for item in yaml.safe_load_all(path.read_text(encoding="utf-8")) if item]
    return [
        item["spec"]["template"]["spec"]
        for item in documents
        if item.get("kind") in {"Deployment", "Job"}
    ]


def _container(spec, name: str):
    return next(item for item in spec["containers"] if item["name"] == name)


def test_all_stage2_pods_share_only_trial_and_ipc_with_agent_runtime():
    for filename in ("stage2.yaml", "stage2-integration.yaml", "stage2-matrix-job.yaml"):
        for spec in _pod_specs(DEPLOY / filename):
            assert spec["automountServiceAccountToken"] is False
            controller = _container(spec, "stage2" if filename != "stage2-matrix-job.yaml" else "matrix")
            agent = _container(spec, "agent-runtime")
            controller_mounts = {item["name"]: item["mountPath"] for item in controller["volumeMounts"]}
            agent_mounts = {item["name"]: item["mountPath"] for item in agent["volumeMounts"]}
            assert controller_mounts["trial-work"] == agent_mounts["trial-work"] == TRIAL_ROOT
            assert controller_mounts["agent-exec-ipc"] == agent_mounts["agent-exec-ipc"] == "/run/resbench"
            assert controller_mounts["sandbox-work"] == agent_mounts["sandbox-work"] == "/var/lib/resbench-stage2/sandbox-trials"
            assert "controller-service-account" in controller_mounts
            assert "controller-service-account" not in agent_mounts
            assert "data" not in agent_mounts
            assert "runtime" not in agent_mounts
            volume = next(item for item in spec["volumes"] if item["name"] == "controller-service-account")
            assert volume["projected"]["defaultMode"] == 0o440


def test_agent_runtime_daemon_has_distinct_identities_mandatory_cgroups_and_uid_egress_policy():
    for filename in ("stage2.yaml", "stage2-integration.yaml", "stage2-matrix-job.yaml"):
        spec = _pod_specs(DEPLOY / filename)[0]
        agent = _container(spec, "agent-runtime")
        args = agent["args"]
        assert args[args.index("--trial-root") + 1] == TRIAL_ROOT
        assert args[args.index("--controller-uid") + 1] == "10001"
        assert args[args.index("--agent-uid") + 1] == "10002"
        assert args[args.index("--agent-gid") + 1] == "10004"
        assert args[args.index("--sandbox-uid") + 1] == "10003"
        assert args[args.index("--sandbox-gid") + 1] == "10003"
        assert args[args.index("--sandbox-trial-root") + 1] == "/var/lib/resbench-stage2/sandbox-trials"
        assert args[args.index("--memory-max") + 1] == "536870912"
        assert args[args.index("--pids-max") + 1] == "64"
        security = agent["securityContext"]
        assert security["runAsUser"] == 0
        # The trusted daemon must chmod Agent-owned 0600 reports after cgroup
        # cleanup; CHOWN alone does not authorize chmod of another UID's file.
        assert {"NET_ADMIN", "SYS_ADMIN", "FOWNER"}.issubset(security["capabilities"]["add"])
        values = {
            item["name"]: item["value"]
            for item in agent["env"]
            if "value" in item
        }
        assert values["RESBENCH_AGENT_EXEC_EGRESS_POLICY"] == "required"
        assert "18090" in values["RESBENCH_AGENT_EXEC_ALLOWED_LOOPBACK_PORTS"]
        assert "18088" in values["RESBENCH_AGENT_EXEC_ALLOWED_LOOPBACK_PORTS"]
        assert "18188" in values["RESBENCH_AGENT_EXEC_ALLOWED_LOOPBACK_PORTS"]
        assert "4000" not in values["RESBENCH_AGENT_EXEC_ALLOWED_LOOPBACK_PORTS"]
        assert "8080" not in values["RESBENCH_AGENT_EXEC_ALLOWED_LOOPBACK_PORTS"]
        pod_uid = next(item for item in agent["env"] if item["name"] == "RESBENCH_AGENT_EXEC_POD_UID")
        assert pod_uid["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.uid"
        assert any(item["name"] == "delegated-cgroup" and item["mountPath"] == "/run/resbench-cgroups" for item in agent["volumeMounts"])
        assert args[args.index("--cgroup-root") + 1] == "/run/resbench-cgroups"
        cgroup = next(item for item in spec["volumes"] if item["name"] == "delegated-cgroup")
        assert cgroup["hostPath"] == {"path": "/sys/fs/cgroup/resbench-agent-exec", "type": "DirectoryOrCreate"}
        namespace = next(item for item in spec["volumes"] if item["name"] == "host-cgroup-namespace")
        assert namespace["hostPath"] == {"path": "/proc/1/ns/cgroup", "type": "File"}
        assert {"name": "host-cgroup-namespace", "mountPath": "/run/resbench-host/cgroupns", "readOnly": True} in agent["volumeMounts"]
        assert not spec.get("hostPID") and not spec.get("hostNetwork")
        for container in spec["containers"]:
            if container["name"] != "agent-runtime":
                assert not any(v["name"] == "host-cgroup-namespace" for v in container.get("volumeMounts", []))
        assert "DAC_OVERRIDE" not in agent["securityContext"]["capabilities"]["add"]
        readiness = agent["readinessProbe"]
        assert readiness["exec"]["command"][:2] == ["/opt/agent/.venv/bin/python", "-c"]
        assert "s.connect('/run/resbench/agent-exec.sock')" in readiness["exec"]["command"][2]
        assert readiness["initialDelaySeconds"] >= 3 and readiness["failureThreshold"] == 1


def test_agent_image_has_only_runtime_assets_and_harness_package_is_side_effect_free():
    image = (DEPLOY / "Dockerfile.agent").read_text(encoding="utf-8")
    assert "! command -v kubectl" in image
    assert "! command -v helm" in image
    assert "test ! -e /opt/blade-ai/vendor/chaosblade" in image
    assert '"mcp>=1.0,<2.0"' in image
    assert '"mcp[cli]==${BLADEAI_MCP_VERSION}"' in image
    assert "mcp').split('.')[0] == '1'" in image
    assert "COPY controller" not in image
    assert "COPY stage2_service /" not in image
    for required in (
        "COPY harness/schemas/agent-result.schema.json /app/harness/schemas/agent-result.schema.json",
        "COPY stage2_service/bladeai_worker.py /app/stage2_service/bladeai_worker.py",
        "COPY stage2_service/bladeai_task.py /app/stage2_service/bladeai_task.py",
        "COPY stage2_service/bladeai_shim.py /app/stage2_service/bladeai_shim.py",
        "COPY stage2_service/bladeai_read_cli.py /app/stage2_service/bladeai_read_cli.py",
        "COPY deploy/stage2/codex-eval /usr/local/bin/codex-eval",
        "/opt/bladeai-venv/bin/python",
        "ln -s /opt/resiliencebenchmark/deepseek-harness/bin/dsh /usr/local/bin/dsh",
        "sed -i '1c #!/opt/bladeai-venv/bin/python' /app/harness/bladeai/blade-shim/blade",
    ):
        assert required in image
    for path in ("/app/harness/bladeai/blade-shim/blade", "/app/harness/bladeai/kubectl-shim/kubectl", "/usr/local/bin/blade"):
        assert path in image
    assert "chmod 0755" in image
    package = (ROOT / "harness/__init__.py").read_text(encoding="utf-8")
    assert "from .streaming" not in package
    assert "from .live_runner" not in package


def test_uid_egress_rules_allow_only_relay_and_mcp_loopback(monkeypatch):
    commands = []
    payloads = []
    monkeypatch.setattr(agent_entrypoint.sys, "platform", "linux")
    monkeypatch.setattr(agent_entrypoint.os, "geteuid", lambda: 0)

    def execute(command, payload):
        commands.append(tuple(command))
        if payload:
            payloads.append(payload.decode())
        if "-C" in command:
            raise subprocess.CalledProcessError(1, command)

    agent_entrypoint.configure_agent_egress(agent_uid=10002, runner=execute)

    rendered = "\n".join([*(" ".join(command) for command in commands), *payloads])
    assert "--dport 18090" in rendered
    assert "--dport 18088" in rendered
    assert "--dport 18188" in rendered
    assert "--dport 4000" not in rendered
    assert "--dport 8080" not in rendered
    assert "--uid-owner 10002" in rendered
    assert "ip6tables -I OUTPUT 1 -m owner --uid-owner 10002 -j REJECT" in rendered


def test_daemon_has_static_agent_only_environment_allowlist_without_manifest_args():
    assert {"PYTHONPATH", "RESBENCH_MCP_TOKEN", "RESBENCH_HARNESS_CHANNEL_TOKEN"}.issubset(
        AGENT_ENV_ALLOWLIST
    )
    daemon = (ROOT / "harness/agent_exec/server.py").read_text(encoding="utf-8")
    assert "set(args.allow_env) or set(AGENT_ENV_ALLOWLIST)" in daemon


def test_gateway_config_is_fixed_and_audit_volume_excludes_the_agent():
    for filename in ("stage2.yaml", "stage2-integration.yaml", "stage2-matrix-job.yaml"):
        spec = _pod_specs(DEPLOY / filename)[0]
        controller = _container(spec, "matrix" if "matrix" in filename else "stage2")
        gateway = _container(spec, "litellm")
        agent = _container(spec, "agent-runtime")
        for component in (controller, gateway):
            config_mount = next(m for m in component["volumeMounts"] if m["mountPath"] == "/etc/litellm/config.yaml")
            assert config_mount == {"name": "litellm-config", "mountPath": "/etc/litellm/config.yaml", "subPath": "config.yaml", "readOnly": True}
            assert any(m["name"] == "gateway-audit" for m in component["volumeMounts"])
            env = {e["name"]: e.get("value") for e in component["env"]}
            assert env["STAGE2_LITELLM_CONFIG_FILE"] == "/etc/litellm/config.yaml"
        assert not any(m["name"] in {"litellm-config", "gateway-audit"} for m in agent["volumeMounts"])
        assert not any(m["name"] == "data" for m in gateway["volumeMounts"])
        callback = next(m for m in gateway["volumeMounts"] if m["mountPath"] == "/etc/litellm/gateway_audit.py")
        assert callback["subPath"] == "gateway_audit.py" and callback["readOnly"] is True
        assert "chmod 0700 /gateway-audit" in spec["initContainers"][0]["args"][0]


def test_projected_namespace_file_uses_a_valid_field_reference():
    for filename in ("stage2.yaml", "stage2-integration.yaml", "stage2-matrix-job.yaml"):
        spec = _pod_specs(DEPLOY / filename)[0]
        volume = next(v for v in spec["volumes"] if v["name"] == "controller-service-account")
        source = next(s for s in volume["projected"]["sources"] if "downwardAPI" in s)
        assert source["downwardAPI"]["items"] == [{"path": "namespace", "fieldRef": {"fieldPath": "metadata.namespace"}}]


def test_agent_image_checks_privileged_network_commands_at_their_installed_paths():
    dockerfile = (DEPLOY / "Dockerfile.agent").read_text()
    for command in ("iptables-restore", "iptables", "ip6tables"):
        assert f"/usr/sbin/{command} --version" in dockerfile
    path_line = next(line for line in dockerfile.splitlines() if line.strip().startswith("PATH="))
    assert "/usr/sbin" not in path_line


def test_dedicated_apparmor_profile_never_disables_enforcement_or_allows_arbitrary_mounts():
    profile = (DEPLOY / "apparmor/resbench-agent-runtime").read_text()
    assert "profile resbench-agent-runtime" in profile
    mount_rules = [line.strip() for line in profile.splitlines() if line.strip().startswith(("mount ", "remount "))]
    assert len(mount_rules) == 4
    assert "mount," not in [line.strip() for line in profile.splitlines()] and "complain" not in profile
    assert "deny /sys/kernel/security/** rwklx," in profile
    assert "mount options=(rw,rprivate) -> /," in mount_rules
    assert "remount options=(ro) /," in mount_rules
    for rule in mount_rules[2:]:
        assert "/var/lib/resbench-stage2/sandbox-trials/*/.sandbox-tmp/" in rule
    for filename in ("stage2.yaml", "stage2-integration.yaml", "stage2-matrix-job.yaml"):
        workload = next(d for d in yaml.safe_load_all((DEPLOY / filename).read_text()) if d and d.get("kind") in {"Deployment", "Job"})
        annotations = workload["spec"]["template"]["metadata"]["annotations"]
        assert annotations["container.apparmor.security.beta.kubernetes.io/agent-runtime"] == "localhost/resbench-agent-runtime"
        assert all("unconfined" not in str(value).lower() for value in annotations.values())
