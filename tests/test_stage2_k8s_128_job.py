"""Static Kubernetes 1.28 compatibility checks; no manifest is applied."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
JOB = ROOT / "deploy/stage2/stage2-matrix-job.yaml"


def test_matrix_job_uses_explicit_controller_completion_sentinel_not_native_sidecars() -> None:
    raw = JOB.read_text(encoding="utf-8")
    assert "restartPolicy: Always" not in raw
    document = yaml.safe_load(raw)
    pod = document["spec"]["template"]["spec"]
    init = pod["initContainers"][0]
    assert "mkdir -p /work /sandbox /ipc/controller" in init["args"][0]
    assert "chmod 0700 /ipc/controller" in init["args"][0]
    containers = {item["name"]: item for item in pod["containers"]}
    assert {"matrix", "litellm", "agent-runtime"} <= set(containers)
    matrix = containers["matrix"]
    assert "job-complete" in matrix["args"][0]
    assert "code=$?" in matrix["args"][0]
    litellm = containers["litellm"]
    assert "job-complete" in litellm["args"][0]
    assert any(mount["mountPath"] == "/run/resbench" and mount.get("readOnly") is True for mount in litellm["volumeMounts"])
    agent = containers["agent-runtime"]
    assert "--job-completion-file" in agent["args"]
    assert "/run/resbench/controller/job-complete" in agent["args"]
