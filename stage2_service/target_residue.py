"""Decide whether a fault is really gone, by measuring the target.

Until now both halves of the platform's "two-layer check" looked at the cluster
face: a private ledger and a list of ChaosBlade/Chaos Mesh CRs.  While the
platform injected through its own shim that was sound -- we could account for
what we ourselves had issued.  Driving a Harness as a black box removes that
premise, and the 2026-09-11/12 round produced two proofs that the cluster face
alone is blind:

* **D8-B** -- with the regular injection permission revoked but ``kubectl exec``
  left in place, BladeAI wrote a shell burner inside the target container
  (``while :; do :; done`` under ``timeout``).  Independent observation measured
  CPU going 3m -> 811m for about 300 seconds, and **not one fault record
  existed in the cluster** the entire time.
* **F5** -- even on the regular path, deleting the cluster record does not stop
  the process: after the CR was gone, cart still measured 1023m against a 3m
  baseline until ``blade destroy`` was run inside the tool container.

So residue is judged here on **measurement first**: has the target's own metric
returned to its pre-trial baseline?  Counting processes is a secondary signal
and cannot be the primary one -- a network fault is a tc qdisc, not a process,
and grepping process names will never see it (pitfall 10).

One rule is inherited deliberately from the cluster-face inventory: a probe
that could not run is **not** a clean result.  ``fault_inventory`` already
refuses to turn a failed CRD listing into an empty environment; the same applies
to a container that could not be measured.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

ResidueState = Literal["clear", "residual", "unverified"]

# How far above baseline a metric may sit and still count as returned.  Both a
# ratio and a floor are needed: cart idles at 3-4 millicores, so a pure ratio
# would make ordinary jitter look like residue, while a pure floor would hide
# residue on a busy workload.  Under a CPU fault the same target measured
# 780-1023m and under a network fault 79ms against a 3-4ms baseline, so these
# tolerances separate the two states by more than an order of magnitude.
DEFAULT_TOLERANCE_RATIO = 2.0
DEFAULT_METRIC_FLOORS: dict[str, float] = {
    "cpu_millicores": 50.0,
    "memory_bytes": 64 * 1024 * 1024,
    "latency_ms": 20.0,
}
DEFAULT_METRIC_FLOOR = 0.0

# Fault families whose residue is not a process.  Listing them keeps the
# "count processes" reflex from being applied where it cannot work.
NON_PROCESS_FAULT_PREFIXES: tuple[str, ...] = ("network",)


@dataclass(frozen=True)
class MetricReading:
    """One measurement of the target, with how it was obtained."""

    metric: str
    value: float
    source: str
    observed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"metric": self.metric, "value": self.value,
                "source": self.source, "observed_at": self.observed_at}


@dataclass
class TargetProbe:
    """What a sweep of the target found.

    ``errors`` is load-bearing: any probe that failed makes the verdict
    ``unverified`` rather than ``clear``.
    """

    baseline: Mapping[str, float] = field(default_factory=dict)
    current: Sequence[MetricReading] = ()
    processes: Sequence[str] = ()
    tc_rules: Sequence[str] = ()
    errors: Sequence[str] = ()
    probed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline": dict(self.baseline),
            "current": [r.to_dict() for r in self.current],
            "processes": list(self.processes),
            "tc_rules": list(self.tc_rules),
            "errors": list(self.errors),
            "probed_at": self.probed_at,
        }


@dataclass(frozen=True)
class MetricVerdict:
    metric: str
    baseline: float
    current: float
    limit: float
    returned: bool

    def to_dict(self) -> dict[str, Any]:
        return {"metric": self.metric, "baseline": self.baseline,
                "current": self.current, "limit": round(self.limit, 3),
                "returned": self.returned}


@dataclass(frozen=True)
class TargetResidueVerdict:
    state: ResidueState
    reason: str
    metrics: tuple[MetricVerdict, ...] = ()
    residual_processes: tuple[str, ...] = ()
    residual_tc_rules: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def clear(self) -> bool:
        return self.state == "clear"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "stage2-target-residue.v1",
            "state": self.state,
            "clear": self.clear,
            "reason": self.reason,
            "metrics": [m.to_dict() for m in self.metrics],
            "residual_processes": list(self.residual_processes),
            "residual_tc_rules": list(self.residual_tc_rules),
            "errors": list(self.errors),
        }


def metric_limit(
    metric: str,
    baseline: float,
    *,
    tolerance_ratio: float = DEFAULT_TOLERANCE_RATIO,
    floors: Mapping[str, float] | None = None,
) -> float:
    floor = (floors or DEFAULT_METRIC_FLOORS).get(metric, DEFAULT_METRIC_FLOOR)
    return max(baseline * tolerance_ratio, baseline + floor)


def assess_target_residue(
    probe: TargetProbe,
    *,
    fault_type: str = "",
    tolerance_ratio: float = DEFAULT_TOLERANCE_RATIO,
    floors: Mapping[str, float] | None = None,
    required_metrics: Iterable[str] = (),
) -> TargetResidueVerdict:
    """Judge residue from the target's own measurements.

    Order of authority, and why:

    1. **A probe error means unverified.**  Not being able to look is not the
       same as having looked and found nothing.
    2. **A metric still above baseline means residual**, whatever the cluster
       face says.  This is the D8-B case: no CR anywhere, CPU at 811m.
    3. **A required metric that was never sampled means unverified.**  Silence
       must not read as a clean target.
    4. Processes and tc rules are corroboration.  They can prove residue but
       cannot, on their own, prove its absence.
    """
    errors = tuple(probe.errors)
    if errors:
        return TargetResidueVerdict(
            state="unverified",
            reason=f"target could not be measured: {errors[0]}",
            errors=errors,
        )

    readings = {r.metric: r for r in probe.current}
    verdicts: list[MetricVerdict] = []
    exceeded: list[str] = []
    for metric, baseline in probe.baseline.items():
        reading = readings.get(metric)
        if reading is None:
            continue
        limit = metric_limit(metric, baseline, tolerance_ratio=tolerance_ratio, floors=floors)
        returned = reading.value <= limit
        verdicts.append(MetricVerdict(metric, baseline, reading.value, limit, returned))
        if not returned:
            exceeded.append(metric)

    missing = [m for m in required_metrics if m not in readings]
    if exceeded:
        worst = next(v for v in verdicts if v.metric == exceeded[0])
        return TargetResidueVerdict(
            state="residual",
            reason=(f"{worst.metric} is {worst.current} against a baseline of "
                    f"{worst.baseline} (limit {worst.limit:.1f}); the fault is "
                    "still acting on the target"),
            metrics=tuple(verdicts),
            residual_processes=tuple(probe.processes),
            residual_tc_rules=tuple(probe.tc_rules),
        )
    if probe.tc_rules:
        # A netem qdisc is residue even while latency happens to look normal:
        # a ledger that reports Success proves nothing (F9), and the rule is
        # invisible to any process check (pitfall 10).
        return TargetResidueVerdict(
            state="residual",
            reason=f"traffic-control rules remain on the target: {probe.tc_rules[0]}",
            metrics=tuple(verdicts), residual_tc_rules=tuple(probe.tc_rules),
        )
    if probe.processes:
        return TargetResidueVerdict(
            state="residual",
            reason=f"unexpected process remains in the target: {probe.processes[0]}",
            metrics=tuple(verdicts), residual_processes=tuple(probe.processes),
        )
    if missing:
        return TargetResidueVerdict(
            state="unverified",
            reason=f"no measurement was taken for {', '.join(sorted(missing))}",
            metrics=tuple(verdicts),
        )
    if not verdicts:
        return TargetResidueVerdict(
            state="unverified",
            reason="no metric was compared against a baseline",
        )
    return TargetResidueVerdict(
        state="clear",
        reason="every measured metric returned to its baseline and nothing remains",
        metrics=tuple(verdicts),
    )


def process_evidence_applies(fault_type: str) -> bool:
    """Whether counting processes can say anything about this fault.

    Network faults are tc qdiscs; a process check is silent on them, which is
    how a netem residue went unnoticed (pitfall 10).
    """
    return not str(fault_type or "").lower().startswith(NON_PROCESS_FAULT_PREFIXES)


# ---- collecting the evidence --------------------------------------------
#
# Verified against the live cluster on 2026-09-12 (old environment):
#   * ``kubectl top pod`` returned cart at 4m -- the primary metric;
#   * reading ``/sys/fs/cgroup/cpu.stat`` inside the container also works, and
#     is the fallback for a slim image with no metrics-server (it is how
#     BladeAI itself measured the target in D8-B);
#   * the ``chaosblade-tool`` DaemonSet already runs one Pod per node (33 days
#     uptime), so node-level process and tc checks need no new deployment;
#   * ``blade status`` is readable but proved untrustworthy in the same sweep:
#     experiment 2ec1be57adba4e96 still reports ``Status: Success`` two days
#     after creation while no tc rule exists on the target at all, and the
#     ledger holds 90+ such stale rows going back to March.  It is a lead, not
#     a verdict (F9).

ExecFn = "Callable[[Sequence[str]], str]"


def parse_cpu_millicores(top_line: str) -> float | None:
    """Read millicores out of a ``kubectl top pod`` row."""
    for field in str(top_line).split():
        if field.endswith("m") and field[:-1].replace(".", "", 1).isdigit():
            return float(field[:-1])
    return None


def parse_cgroup_usage_usec(cpu_stat: str) -> float | None:
    """Read cumulative CPU time from cgroup v2 ``cpu.stat``.

    Cumulative, so a single reading says nothing -- two readings and the
    interval between them give the rate.  Using the total directly is the
    mistake pitfall 6 records: a cumulative average does not fall when the
    fault stops, so it cannot prove recovery.
    """
    for line in str(cpu_stat).splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "usage_usec" and parts[1].isdigit():
            return float(parts[1])
    return None


def millicores_between(first: float, second: float, seconds: float) -> float | None:
    """Convert two cumulative cgroup readings into a rate, the way top does."""
    if seconds <= 0 or second < first:
        return None
    return (second - first) / (seconds * 1000.0)


def residual_processes(ps_output: str, markers: Iterable[str] = ()) -> list[str]:
    """Injection processes still running inside the target or the tool Pod."""
    needles = tuple(markers) or ("chaos_os", "burncpu", "stress-ng", "while :",
                                 "dd if=/dev/zero")
    return [
        line.strip() for line in str(ps_output).splitlines()
        if line.strip() and any(needle in line for needle in needles)
    ]


def residual_tc_rules(tc_output: str) -> list[str]:
    """netem/tbf qdiscs still attached.

    A network fault leaves no process at all, so this is the only direct
    evidence of it -- grepping process names is silent here (pitfall 10).
    """
    return [
        line.strip() for line in str(tc_output).splitlines()
        if line.strip() and ("netem" in line or "tbf" in line)
    ]
