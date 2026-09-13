"""The post-deploy checks the operations manual asks for by hand.

Each of them is silent when it fails: the Pod comes up, the rollout succeeds,
and the next run is the thing that breaks.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

from scripts.verify_stage2_deployment import (
    check_private_file_modes,
    main,
    pod_spec_from_deployment,
    run_checks,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _deployment(**overrides) -> dict:
    document = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "resbench-stage2-integration"},
        "spec": {
            "template": {
                "spec": {
                    "securityContext": {
                        "fsGroup": 10001,
                        "fsGroupChangePolicy": "OnRootMismatch",
                    },
                    "nodeSelector": {"kubernetes.io/hostname": "vm-0-10-ubuntu"},
                    "containers": [
                        {"name": "litellm"},
                        {
                            "name": "stage2",
                            "env": [
                                {"name": "RESBENCH_COROOT_PROJECT_ID", "value": "p1nar0hw"},
                                {"name": "RESBENCH_COROOT_ALLOW_ANONYMOUS_READ", "value": "true"},
                                {
                                    "name": "STAGE2_HARNESS_CAPABILITIES_FILE",
                                    "value": "/var/lib/resbench-stage2/integration/private/harness-capabilities.json",
                                },
                            ],
                        },
                        {"name": "agent-runtime"},
                    ],
                }
            }
        },
    }
    document["spec"]["template"]["spec"].update(overrides)
    return document


def _failures(document, **kwargs) -> list[str]:
    kwargs.setdefault("expected_project", "p1nar0hw")
    kwargs.setdefault("require_node_selector", True)
    kwargs.setdefault("private_listing", None)
    return [check.name for check in run_checks(document, **kwargs) if not check.passed]


def test_a_correct_deployment_passes_every_check():
    assert _failures(_deployment()) == []


def test_a_missing_capabilities_path_is_caught():
    """It is in no rendered manifest, and losing it empties /options."""
    document = _deployment()
    stage2 = document["spec"]["template"]["spec"]["containers"][1]
    stage2["env"] = [
        item for item in stage2["env"] if item["name"] != "STAGE2_HARNESS_CAPABILITIES_FILE"
    ]

    assert _failures(document) == ["STAGE2_HARNESS_CAPABILITIES_FILE"]


def test_an_empty_capabilities_path_is_not_mistaken_for_a_set_one():
    document = _deployment()
    stage2 = document["spec"]["template"]["spec"]["containers"][1]
    for item in stage2["env"]:
        if item["name"] == "STAGE2_HARNESS_CAPABILITIES_FILE":
            item["value"] = ""

    assert _failures(document) == ["STAGE2_HARNESS_CAPABILITIES_FILE"]


def test_the_dns_fallback_is_only_checked_when_asked_for():
    """Most clusters have a working resolver; only opt-in clusters need this."""
    assert _failures(_deployment()) == []
    assert _failures(_deployment(), dns_fallback=["159.226.8.6"]) == ["dns-fallback"]


def test_a_present_dns_fallback_satisfies_the_check():
    document = _deployment(dnsConfig={"nameservers": ["159.226.8.6"]})

    assert _failures(document, dns_fallback=["159.226.8.6"]) == []


def test_a_missing_fs_group_change_policy_is_caught():
    """The setting that is in no repository manifest, so a redeploy drops it."""
    document = _deployment()
    del document["spec"]["template"]["spec"]["securityContext"]["fsGroupChangePolicy"]

    assert "fsGroupChangePolicy" in _failures(document)


def test_the_wrong_fs_group_change_policy_is_caught():
    document = _deployment()
    document["spec"]["template"]["spec"]["securityContext"]["fsGroupChangePolicy"] = "Always"

    assert "fsGroupChangePolicy" in _failures(document)


def test_a_pod_without_any_security_context_fails_both_fs_checks():
    document = _deployment()
    del document["spec"]["template"]["spec"]["securityContext"]

    failures = _failures(document)

    assert "fsGroupChangePolicy" in failures
    assert "fsGroup" in failures


@pytest.mark.parametrize(
    "env_name", ["RESBENCH_COROOT_PROJECT_ID", "RESBENCH_COROOT_ALLOW_ANONYMOUS_READ"]
)
def test_a_dropped_coroot_variable_is_caught(env_name: str):
    document = _deployment()
    container = document["spec"]["template"]["spec"]["containers"][1]
    container["env"] = [item for item in container["env"] if item["name"] != env_name]

    assert env_name in _failures(document)


def test_the_repository_copy_of_the_project_id_is_rejected_for_another_cluster():
    """The manifest in the repository carries the old cluster's project."""
    document = _deployment()
    container = document["spec"]["template"]["spec"]["containers"][1]
    container["env"][0]["value"] = "9auios5b"

    assert "RESBENCH_COROOT_PROJECT_ID" in _failures(document)


def test_the_project_id_is_only_reported_when_no_expectation_is_given():
    document = _deployment()
    document["spec"]["template"]["spec"]["containers"][1]["env"][0]["value"] = "9auios5b"

    assert _failures(document, expected_project=None) == []


def test_a_missing_node_selector_is_caught_only_where_it_matters():
    document = _deployment()
    del document["spec"]["template"]["spec"]["nodeSelector"]

    assert "nodeSelector" in _failures(document)
    assert _failures(document, require_node_selector=False) == []


def test_a_missing_container_is_caught():
    document = _deployment()
    spec = document["spec"]["template"]["spec"]
    spec["containers"] = [item for item in spec["containers"] if item["name"] != "agent-runtime"]

    assert "containers" in _failures(document)


def test_a_live_pod_object_is_read_the_same_way_as_a_deployment():
    deployment = _deployment()
    pod = {
        "kind": "Pod",
        "spec": copy.deepcopy(deployment["spec"]["template"]["spec"]),
    }

    assert pod_spec_from_deployment(pod) == pod_spec_from_deployment(deployment)
    assert _failures(pod) == []


# --- private file modes ---------------------------------------------------


def test_private_files_that_are_group_readable_are_reported():
    listing = "600 /var/lib/resbench-stage2/integration/private/a\n640 /var/lib/resbench-stage2/integration/private/b\n"

    checks = check_private_file_modes(listing)

    assert checks[0].passed is False
    assert "private/b" in checks[0].detail


def test_private_files_at_0600_pass():
    listing = "600 /var/lib/resbench-stage2/integration/private/a\n600 /var/lib/resbench-stage2/integration/qualification/c\n"

    assert check_private_file_modes(listing)[0].passed is True


def test_an_empty_listing_is_not_treated_as_a_pass():
    """Nothing listed means the check did not run, not that it succeeded."""
    checks = check_private_file_modes("")

    assert checks[0].passed is False
    assert "did not run" in checks[0].detail


@pytest.mark.parametrize("mode", ["640", "604", "660", "606", "666", "700"])
def test_any_group_or_other_bit_fails(mode: str):
    passed = check_private_file_modes(f"{mode} /p")[0].passed

    assert passed is (mode == "700")


# --- CLI ------------------------------------------------------------------


def test_the_cli_reads_a_rendered_manifest_and_reports_json(tmp_path: Path, capsys):
    path = tmp_path / "rendered.yaml"
    path.write_text(yaml.safe_dump(_deployment()), encoding="utf-8")

    code = main(
        ["--manifest", str(path), "--coroot-project", "p1nar0hw", "--require-node-selector", "--json"]
    )

    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is True
    assert {check["name"] for check in report["checks"]} >= {
        "fsGroupChangePolicy",
        "RESBENCH_COROOT_PROJECT_ID",
        "RESBENCH_COROOT_ALLOW_ANONYMOUS_READ",
    }


def test_the_cli_exits_nonzero_when_a_check_fails(tmp_path: Path, capsys):
    document = _deployment()
    del document["spec"]["template"]["spec"]["securityContext"]["fsGroupChangePolicy"]
    path = tmp_path / "rendered.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    code = main(["--manifest", str(path), "--coroot-project", "p1nar0hw"])

    assert code == 1
    assert "FAIL  fsGroupChangePolicy" in capsys.readouterr().out


def test_the_repository_manifest_itself_would_fail_this_check(capsys):
    """Why the manual says never to apply the rendered manifests directly.

    The repository copy has no fsGroupChangePolicy at all, no nodeSelector, and
    the previous cluster's Coroot project id.
    """
    path = REPO_ROOT / "deploy/stage2/stage2-integration.yaml"
    text = (
        path.read_text(encoding="utf-8")
        .replace("__STAGE2_IMAGE__", "image:tag")
        .replace("__STAGE2_AGENT_IMAGE__", "image:tag")
        .replace("__SOURCE_HEAD__", "0000000")
    )
    document = next(
        item
        for item in yaml.safe_load_all(text)
        if isinstance(item, dict)
        and item.get("kind") == "Deployment"
        and item.get("metadata", {}).get("name") == "resbench-stage2-integration"
    )

    failures = _failures(document)

    assert "fsGroupChangePolicy" in failures
    assert "nodeSelector" in failures
    assert "RESBENCH_COROOT_PROJECT_ID" in failures
