"""WP-E: residue judged by measuring the target, not by listing the cluster.

The numbers in these cases are the ones actually measured on 2026-09-11/12:
cart idles at 3-4 millicores and 3-4 ms; under CPU load it measured 780-1023m;
under netem its inbound latency measured 79 ms.
"""

from __future__ import annotations

import pytest

from stage2_service.fault_inventory import snapshot_for_trial
from stage2_service.target_residue import (
    MetricReading,
    TargetProbe,
    assess_target_residue,
    metric_limit,
    process_evidence_applies,
)


def reading(metric: str, value: float, source: str = "kubectl top") -> MetricReading:
    return MetricReading(metric=metric, value=value, source=source)


# ---- the D8-B regression case (the acceptance requirement) --------------


def test_a_burner_with_no_cluster_record_is_caught() -> None:
    """D8-B: injection permission revoked, ``kubectl exec`` left.

    BladeAI wrote a shell burner inside the target container.  CPU went
    3m -> 811m for about 300 seconds and **not one fault record existed in the
    cluster**.  Today's platform, which only lists CRs, is blind to this.
    """
    verdict = assess_target_residue(TargetProbe(
        baseline={"cpu_millicores": 3.0},
        current=[reading("cpu_millicores", 811.0)],
        # The burner is a shell loop, so a process check happens to see it --
        # but the judgement must not depend on that.
        processes=[],
    ))
    assert verdict.state == "residual"
    assert not verdict.clear
    assert "811" in verdict.reason and "baseline of 3" in verdict.reason


def test_the_cluster_face_alone_would_have_called_that_clean() -> None:
    """The whole point: both faces are needed, and they disagree here."""
    from stage2_service.target_residue import TargetResidueVerdict

    burner = assess_target_residue(TargetProbe(
        baseline={"cpu_millicores": 3.0}, current=[reading("cpu_millicores", 811.0)],
    ))
    snapshot = snapshot_for_trial(
        trial_id="t-d8b", resources=[], qualified_executors=["chaosblade"],
        target_residue=burner,
    )
    # No CR exists, so the cluster face is spotless...
    assert snapshot["inventory_clear"] is True
    # ...and the authoritative answer is still "not clean".
    assert snapshot["residue_clear"] is False
    assert snapshot["target_residue_state"] == "residual"


def test_f5_deleting_the_record_does_not_stop_the_process() -> None:
    """F5: CR deleted, cart still measured 1023m against a 3m baseline."""
    verdict = assess_target_residue(TargetProbe(
        baseline={"cpu_millicores": 3.0}, current=[reading("cpu_millicores", 1023.0)],
    ))
    assert verdict.state == "residual"


# ---- measurement is the primary judgement -------------------------------


def test_a_returned_metric_reads_clear() -> None:
    verdict = assess_target_residue(TargetProbe(
        baseline={"cpu_millicores": 3.0}, current=[reading("cpu_millicores", 4.0)],
    ))
    assert verdict.state == "clear"
    assert verdict.metrics[0].returned is True


def test_ordinary_jitter_is_not_residue() -> None:
    """A 3m idle target must not look faulted because it moved to 6m."""
    verdict = assess_target_residue(TargetProbe(
        baseline={"cpu_millicores": 3.0}, current=[reading("cpu_millicores", 6.0)],
    ))
    assert verdict.state == "clear"


def test_the_tolerance_separates_idle_noise_from_a_real_fault() -> None:
    limit = metric_limit("cpu_millicores", 3.0)
    assert limit >= 50.0            # idle jitter fits under it
    assert 780.0 > limit            # every measured fault state is far above it


def test_business_latency_residue_is_caught() -> None:
    """L3: inbound latency 3-4 ms baseline, 79 ms under netem."""
    verdict = assess_target_residue(TargetProbe(
        baseline={"latency_ms": 4.0}, current=[reading("latency_ms", 79.0, "probe")],
    ))
    assert verdict.state == "residual"
    assert "latency_ms" in verdict.reason


# ---- process counting is secondary, and blind to network faults ---------


def test_tc_rules_are_residue_even_when_latency_looks_normal() -> None:
    """F9: a ledger said Success for hours after the rule was gone -- and the
    reverse also matters: a rule present while latency reads normal is residue.
    """
    verdict = assess_target_residue(TargetProbe(
        baseline={"latency_ms": 4.0}, current=[reading("latency_ms", 4.0)],
        tc_rules=["qdisc netem 8001: root refcnt 2 limit 1000 delay 75ms"],
    ))
    assert verdict.state == "residual"
    assert "traffic-control" in verdict.reason


def test_a_leftover_process_is_residue() -> None:
    verdict = assess_target_residue(TargetProbe(
        baseline={"cpu_millicores": 3.0}, current=[reading("cpu_millicores", 3.0)],
        processes=["chaos_os create cpu fullload"],
    ))
    assert verdict.state == "residual"


def test_process_evidence_does_not_apply_to_network_faults() -> None:
    """Pitfall 10: netem is a qdisc, so grepping process names sees nothing."""
    assert process_evidence_applies("cpu-load") is True
    assert process_evidence_applies("memory-stress") is True
    assert process_evidence_applies("network-delay") is False
    assert process_evidence_applies("network-loss") is False


# ---- a probe that could not run is never "clean" -------------------------


def test_a_failed_probe_is_unverified_not_clear() -> None:
    """Same rule the cluster inventory already applies to a failed CRD listing."""
    verdict = assess_target_residue(TargetProbe(
        baseline={"cpu_millicores": 3.0},
        errors=["exec into cart-7c58f6bb56-jzz9b failed: Forbidden"],
    ))
    assert verdict.state == "unverified"
    assert not verdict.clear
    assert "Forbidden" in verdict.reason


def test_a_missing_required_metric_is_unverified() -> None:
    verdict = assess_target_residue(
        TargetProbe(baseline={"cpu_millicores": 3.0},
                    current=[reading("cpu_millicores", 3.0)]),
        required_metrics=["latency_ms"],
    )
    assert verdict.state == "unverified"
    assert "latency_ms" in verdict.reason


def test_nothing_measured_at_all_is_unverified() -> None:
    assert assess_target_residue(TargetProbe()).state == "unverified"


def test_an_unverified_probe_blocks_residue_clear() -> None:
    verdict = assess_target_residue(TargetProbe(errors=["no route to target"]))
    snapshot = snapshot_for_trial(
        trial_id="t", resources=[], qualified_executors=["chaosblade"],
        target_residue=verdict,
    )
    assert snapshot["inventory_clear"] is True
    assert snapshot["residue_clear"] is False


# ---- backward compatibility ---------------------------------------------


def test_callers_that_do_not_probe_are_unaffected() -> None:
    snapshot = snapshot_for_trial(
        trial_id="t", resources=[], qualified_executors=["chaosblade"],
    )
    assert snapshot["inventory_clear"] is True
    assert snapshot["residue_clear"] is True
    assert snapshot["target_residue_state"] == "not_probed"
    assert snapshot["target_residue"] is None


def test_a_clear_probe_agrees_with_a_clean_cluster_face() -> None:
    verdict = assess_target_residue(TargetProbe(
        baseline={"cpu_millicores": 3.0}, current=[reading("cpu_millicores", 3.0)],
    ))
    snapshot = snapshot_for_trial(
        trial_id="t", resources=[], qualified_executors=["chaosblade"],
        target_residue=verdict,
    )
    assert snapshot["residue_clear"] is True


def test_a_dirty_cluster_face_is_still_not_clear_however_good_the_target_looks() -> None:
    """Measurement can prove residue; it cannot excuse a leftover CR."""
    from stage2_service.fault_inventory import FaultResource

    verdict = assess_target_residue(TargetProbe(
        baseline={"cpu_millicores": 3.0}, current=[reading("cpu_millicores", 3.0)],
    ))
    leftover = FaultResource(
        executor_id="chaosblade", resource_kind="chaosblades.chaosblade.io",
        name="leftover", namespace="otel-demo", run_id="t",
        target_name="cart", target_uid="uid-1", fault_type="cpu-load",
        phase="Running", owner="stage2", principal=None,
        labels={"benchmark.trial": "t"}, ledger_matched=True,
    )
    snapshot = snapshot_for_trial(
        trial_id="t", resources=[leftover], qualified_executors=["chaosblade"],
        target_residue=verdict,
    )
    assert snapshot["inventory_clear"] is False
    assert snapshot["residue_clear"] is False


# ---- evidence parsing, against real command output ----------------------


def test_kubectl_top_row_is_parsed() -> None:
    """Real output captured from the cluster on 2026-09-12."""
    from stage2_service.target_residue import parse_cpu_millicores

    assert parse_cpu_millicores("cart-7c58f6bb56-jzz9b   4m    92Mi") == 4.0
    assert parse_cpu_millicores("cart-x   811m   90Mi") == 811.0
    assert parse_cpu_millicores("no metrics available") is None


def test_cgroup_reading_is_cumulative_and_needs_two_samples() -> None:
    """Pitfall 6: a cumulative total does not fall when the fault stops.

    Using it directly would make recovery unprovable, so the rate is computed
    from two readings and the interval between them.
    """
    from stage2_service.target_residue import (
        millicores_between, parse_cgroup_usage_usec,
    )

    first = parse_cgroup_usage_usec("usage_usec 1000000\nuser_usec 900000")
    second = parse_cgroup_usage_usec("usage_usec 1800000\nuser_usec 1600000")
    assert (first, second) == (1000000.0, 1800000.0)
    # 0.8 CPU-seconds burned in one second = 800 millicores, the intensity
    # D8-B's burner actually produced.
    assert millicores_between(first, second, 1.0) == 800.0
    # An idle second stays near zero even though the total keeps growing.
    assert millicores_between(second, second + 3000, 1.0) == 3.0


def test_a_single_cumulative_reading_cannot_be_a_rate() -> None:
    from stage2_service.target_residue import millicores_between

    assert millicores_between(1000.0, 900.0, 1.0) is None   # counter went back
    assert millicores_between(1000.0, 2000.0, 0.0) is None  # no interval


def test_injection_processes_are_recognised_including_a_hand_rolled_burner() -> None:
    """D8-B wrote a shell loop, not a ChaosBlade process."""
    from stage2_service.target_residue import residual_processes

    assert residual_processes("root 1 chaos_os create cpu fullload")
    assert residual_processes("root 9 timeout 300 sh -c while :; do :; done")
    assert residual_processes("root 1 /app/cart --serve") == []


def test_netem_rules_are_recognised() -> None:
    from stage2_service.target_residue import residual_tc_rules

    assert residual_tc_rules(
        "qdisc netem 8001: root refcnt 2 limit 1000 delay 75ms")
    assert residual_tc_rules("qdisc noqueue 0: root refcnt 2") == []
