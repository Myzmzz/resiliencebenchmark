"""The env3 overlay must put back exactly what the base manifest cannot carry.

Applying the repository manifest as-is loses three per-cluster settings, and
each loss is silent at deploy time — the Pod comes up and the next run is what
breaks. The renderer and the post-deploy checker are two halves of the same
guarantee, so the end-to-end test here runs one into the other.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "deploy/stage2/env3"))

import render  # noqa: E402

from scripts.verify_stage2_deployment import run_checks  # noqa: E402


VALUES = yaml.safe_load((REPO_ROOT / "deploy/stage2/env3/values.yaml").read_text(encoding="utf-8"))


def _base_documents() -> list[dict]:
    text = (
        (REPO_ROOT / "deploy/stage2/stage2-integration.yaml")
        .read_text(encoding="utf-8")
        .replace("__STAGE2_IMAGE__", "registry.example/stage2:tag")
        .replace("__STAGE2_AGENT_IMAGE__", "registry.example/agent:tag")
        .replace("__SOURCE_HEAD__", "0000000")
    )
    return [doc for doc in yaml.safe_load_all(text) if isinstance(doc, dict)]


def _deployment(documents) -> dict:
    return next(
        doc
        for doc in documents
        if doc.get("kind") == "Deployment"
        and doc["metadata"]["name"] == "resbench-stage2-integration"
    )


def _failures(document) -> list[str]:
    return [
        check.name
        for check in run_checks(
            document,
            expected_project=VALUES["coroot"]["projectId"],
            require_node_selector=True,
            private_listing=None,
            dns_fallback=VALUES["dnsFallback"]["nameservers"],
        )
        if not check.passed
    ]


def test_the_base_manifest_alone_fails_every_environment_check():
    """This is why the manual says never to apply the rendered manifests."""
    failures = _failures(_deployment(_base_documents()))

    assert set(failures) == {
        "fsGroupChangePolicy",
        "RESBENCH_COROOT_PROJECT_ID",
        "STAGE2_HARNESS_CAPABILITIES_FILE",
        "nodeSelector",
        "dns-fallback",
    }


def test_the_rendered_manifest_passes_the_post_deploy_checker():
    """Renderer and checker are two halves of one guarantee; run them together."""
    rendered = render.apply_overlay(_base_documents(), VALUES)

    assert _failures(_deployment(rendered)) == []


def test_the_overlay_carries_the_values_measured_on_that_cluster():
    assert VALUES["nodeSelector"] == {"kubernetes.io/hostname": "otcaix-62"}
    assert VALUES["storageClassName"] == "nfs-client"
    assert VALUES["securityContext"]["fsGroupChangePolicy"] == "OnRootMismatch"
    assert VALUES["coroot"]["projectId"] == "po24tcoz"
    assert VALUES["coroot"]["allowAnonymousRead"] is True
    assert VALUES["dnsFallback"]["nameservers"] == ["159.226.8.6"]
    assert VALUES["harnessCapabilitiesFile"].startswith("/var/lib/resbench-stage2/")


def test_the_storage_class_reaches_the_pvc():
    rendered = render.apply_overlay(_base_documents(), VALUES)
    pvcs = [doc for doc in rendered if doc.get("kind") == "PersistentVolumeClaim"]

    for pvc in pvcs:
        if pvc["metadata"]["name"] == "resbench-stage2-data":
            assert pvc["spec"]["storageClassName"] == "nfs-client"


def test_image_references_are_left_to_the_deploy_script():
    """Images go through deploy_boundary.sh, which has the idle gate."""
    base = _base_documents()
    rendered = render.apply_overlay(base, VALUES)

    def images(documents):
        spec = _deployment(documents)["spec"]["template"]["spec"]
        return [c["image"] for c in spec["containers"]] + [
            c["image"] for c in spec.get("initContainers", [])
        ]

    assert images(rendered) == images(base)


def test_everything_else_is_left_alone():
    """Only the four documented fields may differ from the base."""
    base = _deployment(_base_documents())
    rendered = _deployment(render.apply_overlay(_base_documents(), VALUES))

    base_spec = base["spec"]["template"]["spec"]
    rendered_spec = rendered["spec"]["template"]["spec"]
    changed = {
        key
        for key in set(base_spec) | set(rendered_spec)
        if base_spec.get(key) != rendered_spec.get(key)
    }

    assert changed <= {"securityContext", "nodeSelector", "containers", "dnsConfig"}


def test_the_capabilities_path_reaches_the_stage2_container():
    """Without it /options reports qualification_not_passed for every harness."""
    rendered = render.apply_overlay(_base_documents(), VALUES)
    spec = _deployment(rendered)["spec"]["template"]["spec"]
    stage2 = next(c for c in spec["containers"] if c["name"] == "stage2")
    env = {item["name"]: item.get("value") for item in stage2["env"]}

    assert env["STAGE2_HARNESS_CAPABILITIES_FILE"] == VALUES["harnessCapabilitiesFile"]


def test_the_dns_fallback_is_appended_not_substituted():
    """ClusterFirst keeps CoreDNS first; these only catch a SERVFAIL."""
    rendered = render.apply_overlay(_base_documents(), VALUES)
    spec = _deployment(rendered)["spec"]["template"]["spec"]

    assert spec["dnsConfig"]["nameservers"] == ["159.226.8.6"]
    assert spec.get("dnsPolicy") in (None, "ClusterFirst")


def test_a_non_clusterfirst_policy_is_refused_rather_than_quietly_useless():
    """Appending only happens under ClusterFirst, so anything else is a lie."""
    from scripts.verify_stage2_deployment import check_dns_fallback

    spec = {"dnsPolicy": "None", "dnsConfig": {"nameservers": ["159.226.8.6"]}}
    (check,) = check_dns_fallback(spec, expected=["159.226.8.6"])

    assert not check.passed


def test_a_rewritten_variable_keeps_its_position_and_drops_valuefrom():
    env = [
        {"name": "A", "value": "1"},
        {"name": "RESBENCH_COROOT_PROJECT_ID", "valueFrom": {"fieldRef": {}}},
        {"name": "Z", "value": "9"},
    ]

    render._set_env(env, "RESBENCH_COROOT_PROJECT_ID", "po24tcoz")

    assert [item["name"] for item in env] == ["A", "RESBENCH_COROOT_PROJECT_ID", "Z"]
    assert env[1] == {"name": "RESBENCH_COROOT_PROJECT_ID", "value": "po24tcoz"}


def test_a_base_without_the_deployment_is_refused_rather_than_silently_empty():
    with pytest.raises(render.OverlayError, match="nothing to overlay"):
        render.apply_overlay([{"kind": "ConfigMap", "metadata": {"name": "x"}}], VALUES)


def test_a_base_without_the_stage2_container_is_refused():
    documents = _base_documents()
    spec = _deployment(documents)["spec"]["template"]["spec"]
    spec["containers"] = [c for c in spec["containers"] if c["name"] != "stage2"]

    with pytest.raises(render.OverlayError, match="stage2 container"):
        render.apply_overlay(documents, VALUES)


def test_the_renderer_does_not_mutate_the_documents_it_was_given():
    base = _base_documents()
    before = yaml.safe_dump(base, sort_keys=True)

    render.apply_overlay(base, VALUES)

    assert yaml.safe_dump(base, sort_keys=True) == before
