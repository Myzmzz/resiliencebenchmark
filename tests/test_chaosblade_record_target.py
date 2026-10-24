"""How a ChaosBlade CR's target Pod is read, including label-selected experiments.

``_record_from_resource`` used to read the Pod name only from the ``names``
matcher.  Round eight (2026-09-16) showed BladeAI choosing the same Pod by
label instead, which left the name empty and broke foreign-fault attribution.
These tests pin the fallback to the Pod the operator actually hit.
"""

from __future__ import annotations

from mcp_servers.chaos_core.backends.chaosblade import _record_from_resource


def _cr(*, names=(), identifiers=(), namespace="otel-demo", success=True):
    """Build a CR with the given ``names`` matcher and operator-recorded hits."""
    return {
        "kind": "ChaosBlade",
        "metadata": {"name": "exp-1", "labels": {}},
        "spec": {
            "experiments": [
                {
                    "scope": "pod",
                    "target": "cpu",
                    "action": "fullload",
                    "matchers": [
                        {"name": "namespace", "value": [namespace]},
                        {"name": "names", "value": list(names)},
                        {"name": "labels", "value": ["app.kubernetes.io/component=cart"]},
                    ],
                }
            ]
        },
        "status": {
            "phase": "Running",
            "expStatuses": [
                {
                    "success": success,
                    "resStatuses": [
                        {"identifier": identifier, "kind": "pod", "success": success}
                        for identifier in identifiers
                    ],
                }
            ],
        },
    }


def test_names_matcher_wins_when_present():
    """A named Pod is the Agent's explicit choice; the status is not consulted."""
    record = _record_from_resource(
        _cr(names=["cart-named"], identifiers=["otel-demo/node-1/cart-other/cart/abc/docker"])
    )
    assert record.target_name == "cart-named"


def test_label_selection_falls_back_to_the_single_pod_the_operator_hit():
    """The cri identifier carries six fields; the Pod is always the third."""
    record = _record_from_resource(_cr(identifiers=["otel-demo/node-1/cart-1/cart/abc/docker"]))
    assert record.target_name == "cart-1"
    assert record.fault_type == "cpu-load"


def test_label_selection_hitting_several_pods_is_not_guessed():
    """Two different Pods means no single target, so the name stays empty."""
    record = _record_from_resource(
        _cr(
            identifiers=[
                "otel-demo/node-1/cart-1/cart/abc/docker",
                "otel-demo/node-2/cart-2/cart/def/docker",
            ]
        )
    )
    assert record.target_name == ""


def test_a_hit_in_another_namespace_is_ignored():
    """A replica must never take a Pod name from another replica's namespace."""
    record = _record_from_resource(_cr(identifiers=["otel-demo-09/node-1/cart-1/cart/abc/docker"]))
    assert record.target_name == ""


def test_a_failed_hit_is_not_used():
    """An injection the operator marked unsuccessful did not act on that Pod."""
    record = _record_from_resource(
        _cr(identifiers=["otel-demo/node-1/cart-1/cart/abc/docker"], success=False)
    )
    assert record.target_name == ""
