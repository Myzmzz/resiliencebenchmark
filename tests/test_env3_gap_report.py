"""The env3 inventory is compared to the target state by machine, not by eye.

The target state is what the second environment was measured to be on
2026-09-12 (docs/status/stage2-env3-reference-state-20260912.md). Encoding it
here means a later inventory is judged against the same numbers rather than
against whoever is reading it that day.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "env3"))

import gap_report  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[1]

# What the second environment actually reported, used as the "all good" case.
GOOD = {
    "host": "vm-0-10-ubuntu",
    "os": "ubuntu 26.04",
    "kernel": "7.0.0-14-generic",
    "cpu_cores": "32",
    "mem_total_mb": str(123 * 1024),
    "cgroup_version": "v2",
    "swap_on": "no",
    "time_synced": "yes",
    "has_docker": "yes",
    "has_helm": "yes",
    "k8s_runtime": "docker://29.6.1",
    "k8s_server": "v1.31.14",
    "k8s_nodes": "2",
    "storageclass": "openebs-hostpath,nfs-client",
    "cni": "10-flannel.conflist",
    "apparmor_profile": "1",
    "egress_registry_k8s_io": "200",
    "egress_ghcr_io": "401",
    "egress_quay_io": "200",
    "egress_old_harbor": "200",
    "ns_otel-demo": "present",
    "ns_observability": "present",
    "ns_coroot": "present",
    "ns_chaos-mesh": "present",
    "ns_resiliencebenchmark-system": "present",
    "chaosblade_operator": "present",
    "chaosblade_wrapper": "present",
}


def _facts_text(overrides: dict | None = None) -> str:
    facts = {**GOOD, **(overrides or {})}
    return "\n".join(f"FACT {k}={v}" for k, v in facts.items()) + "\n"


def _run(overrides: dict | None = None, tmp_path: Path | None = None):
    facts = gap_report.parse_facts(_facts_text(overrides))
    return gap_report.render([("node.txt", facts)])


def test_the_second_environment_itself_shows_no_blocking_gap():
    """The target state must pass the checks that describe it."""
    text, code = _run()

    assert code == 0
    assert "阻塞级差距 0 项" in text
    assert "未采集到 0 项" in text


def test_an_empty_machine_reports_every_component_as_todo():
    text, _ = _run(
        {key: "absent" for key, _, _ in gap_report.COMPONENTS}
    )

    rows = [line for line in text.splitlines() if line.strip().startswith("待装 ")]

    assert len(rows) == len(gap_report.COMPONENTS)
    assert "deploy/chaosblade/cgroupns-wrapper.yaml" in text


@pytest.mark.parametrize(
    ("override", "label"),
    [
        ({"cgroup_version": "v1"}, "cgroup 版本"),
        ({"kernel": "5.4.0-generic"}, "内核版本"),
        ({"cpu_cores": "8"}, "CPU 核数"),
        ({"mem_total_mb": "16384"}, "内存"),
        ({"has_docker": "no"}, "Docker"),
        ({"k8s_runtime": "containerd://1.7.0"}, "集群运行时"),
        ({"k8s_server": "v1.28.2"}, "Kubernetes 版本"),
        ({"apparmor_profile": "0"}, "AppArmor profile resbench-agent-runtime"),
    ],
)
def test_each_blocking_prerequisite_is_caught(override: dict, label: str):
    text, code = _run(override)

    assert code == 1, f"{label} 应该是阻塞级差距"
    row = next(line for line in text.splitlines() if label in line)
    assert "差距" in row


def test_a_containerd_cluster_is_a_blocker_not_a_warning():
    """deploy/stage2/README.md pins the agent-exec cgroup design to Docker."""
    _, code = _run({"k8s_runtime": "containerd://1.7.0", "has_docker": "no"})

    assert code == 1


@pytest.mark.parametrize(
    "override",
    [{"egress_ghcr_io": "000"}, {"egress_old_harbor": "000"}, {"swap_on": "yes"}],
)
def test_network_and_swap_findings_are_warnings_not_blockers(override: dict):
    """They change how much mirroring is needed; they do not stop the build."""
    text, code = _run(override)

    assert code == 0
    assert "注意" in text


def test_a_cluster_without_any_storage_class_is_not_silently_ok():
    """The script emits ``unknown`` for an empty value, so it must not read as a pass."""
    text, _ = _run({"storageclass": "unknown"})

    row = next(line for line in text.splitlines() if "存储类" in line)
    assert "未知" in row and "ok" not in row


def test_an_uncollected_fact_is_never_counted_as_a_pass():
    text, code = _run({"cgroup_version": "unknown", "apparmor_profile": ""})

    assert "未采集到 2 项" in text
    assert "未采集到的不算通过" in text
    assert code == 0  # unknown is not a gap, but it is called out


def test_a_missing_fact_key_behaves_like_unknown():
    facts = gap_report.parse_facts("FACT host=n1\n")
    text, _ = gap_report.render([("n1.txt", facts)])

    assert "未采集到 " in text
    assert text.count("未知") >= len(gap_report.EXPECTATIONS)


def test_several_nodes_are_reported_separately():
    a = gap_report.parse_facts(_facts_text({"host": "n1"}))
    b = gap_report.parse_facts(_facts_text({"host": "n2", "cgroup_version": "v1"}))

    text, code = gap_report.render([("n1.txt", a), ("n2.txt", b)])

    assert "## n1.txt" in text and "## n2.txt" in text
    assert "n1" in text and "n2" in text
    assert code == 1


def test_parse_ignores_the_human_readable_part_of_the_report():
    text = (
        "========== 0 采集元信息 ==========\n"
        "--- $ hostname\nnode60\n"
        "FACT host=node60\n"
        "some noise FACT not_a_fact=1\n"
        "FACT cpu_cores=8\n"
    )

    facts = gap_report.parse_facts(text)

    assert facts == {"host": "node60", "cpu_cores": "8"}


# --- the script the facts come from --------------------------------------


def test_the_inventory_script_is_valid_shell():
    result = subprocess.run(
        ["bash", "-n", str(REPO_ROOT / "tools/env3/inventory.sh")],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_the_inventory_script_emits_every_fact_the_report_reads():
    """A renamed fact in one file and not the other would silently read unknown.

    Some names are composed in loops (``emit "ns_$ns"``), so the literal key is
    not in the script; for those the loop's own list is what has to contain it.
    """
    script = (REPO_ROOT / "tools/env3/inventory.sh").read_text(encoding="utf-8")
    needed = {item.key for item in gap_report.EXPECTATIONS}
    needed |= {key for key, _, _ in gap_report.COMPONENTS}

    missing = []
    for key in sorted(needed):
        if key in script:
            continue
        for prefix in ("ns_", "has_", "egress_"):
            # e.g. ns_otel-demo is emitted by `for ns in otel-demo ...`
            if key.startswith(prefix) and key[len(prefix):].replace("_", ".") in script:
                break
            if key.startswith(prefix) and key[len(prefix):] in script:
                break
        else:
            missing.append(key)

    assert missing == []
