"""Inspect real request-series labels and compare fixed, target-scoped windows."""

from __future__ import annotations

import json
import math
from datetime import datetime
from collections.abc import Mapping


def timestamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def request_observability(runtime, start, end, load):
    namespace = runtime.target.namespace
    check = {"status": "not_established", "target_uid": runtime.target.uid,
             "start": start, "end": end, "checks": [], "target_series": [],
             "alternative_paths": "not_assessed"}
    try:
        labels = load("labels", {"start": start, "end": end}).get("data") or []
        check["checks"].append({"tool": "telemetry_prom_list_labels", "labels": labels})
        names = load("label/__name__/values", {
            "start": start, "end": end, "match[]": f'{{namespace="{namespace}"}}',
        }).get("data") or []
        candidates = [name for name in names if isinstance(name, str) and name.endswith("_count")
                      and any(part in name.lower() for part in ("http", "rpc", "request"))]
        check["candidate_metrics"] = candidates
        for metric in candidates:
            rows = load("series", {
                "start": start, "end": end, "match[]": f'{metric}{{namespace="{namespace}"}}',
            }).get("data") or []
            check["checks"].append({"tool": "telemetry_prom_metric_series", "metric": metric, "series": rows})
            for row in rows:
                identity = next((label for label in ("pod", "pod_name", "k8s_pod_name")
                                 if row.get(label) == runtime.target.name), None)
                if identity:
                    entry = {"metric": metric, "identity_label": identity, "identity_value": runtime.target.name}
                    if entry not in check["target_series"]:
                        check["target_series"].append(entry)
        if check["target_series"]:
            check.update(status="observable", reason="target request series found")
        else:
            check.update(status="limited", reason="request metrics lack target identity; alternative paths are not yet assessed")
        check["paths"] = [{
            "path": "prometheus_request_metrics",
            "status": "observable" if check["target_series"] else (
                "not_observable" if any(item.get("series") for item in check["checks"]) else None
            ),
            "reason": check["reason"],
        }, {"path": "alternative_target_evidence", "status": None, "reason": "not assessed"}]
    except Exception as exc:
        check["reason"] = f"metadata query failed: {type(exc).__name__}"
    return check


def target_request_effect(runtime, observability, start, end, baseline_start, load):
    comparisons = []
    for candidate in observability.get("target_series") or ():
        metric = candidate["metric"]
        selector = '{namespace=' + json.dumps(runtime.target.namespace) + ',' + candidate["identity_label"] + '=' + json.dumps(runtime.target.name) + '}'
        base_start = max(baseline_start or start - 60, start - 60)
        if base_start >= start:
            continue
        measures = {}
        try:
            for label, (left, right) in {"baseline": (base_start, start), "fault": (start, end)}.items():
                values = {}
                for suffix, name in (("count", metric), ("sum", metric.removesuffix("_count") + "_sum")):
                    response = load(query=f"sum({name}{selector})", start=left, end=right, step=1)
                    series = (response.get("data") or {}).get("result") or ()
                    samples = [float(row[1]) for item in series for row in item.get("values") or ()]
                    if len(samples) >= 2 and all(math.isfinite(v) for v in samples) and samples[-1] >= samples[0]:
                        values[suffix] = samples[-1] - samples[0]
                count = values.get("count")
                measures[label] = {"window": [left, right], "request_count": count,
                                   "rps": count / (right-left) if count is not None else None,
                                   "average": values["sum"] / count if count and "sum" in values else None}
            before, fault = measures["baseline"], measures["fault"]
            fault_type = runtime.main_fault.get("fault_type")
            verified = False
            if fault_type == "network-delay" and before["average"] is not None and fault["average"] is not None:
                unit_ms = 1000 if "seconds" in metric else 1 if "milliseconds" in metric else None
                expected = float(runtime.main_fault.get("intensity", {}).get("delay_ms") or 0)
                if unit_ms and expected > 0:
                    delta = (fault["average"] - before["average"]) * unit_ms
                    threshold = float(runtime.main_fault.get("effect_min_delta_ms") or expected * 0.1)
                    verified = math.isfinite(delta) and delta >= threshold
                    measures.update(delta_ms=delta, required_delta_ms=threshold)
            elif fault_type == "network-loss" and before["rps"] and fault["rps"] is not None:
                loss = float(runtime.main_fault.get("intensity", {}).get("loss_percent") or 0)
                verified = loss > 0 and fault["rps"] <= before["rps"] * (1-loss/200)
            comparison = {"verified": verified, "target_uid": runtime.target.uid, "metric": metric,
                          "scope": "target_pod", "measurements": measures}
            comparisons.append(comparison)
            if verified:
                return {**comparison, "comparisons": comparisons}
        except Exception as exc:
            comparisons.append({"metric": metric, "error": type(exc).__name__})
    return {"verified": False, "comparisons": comparisons,
            "reason": "no attributable request difference established in the original fault window"}
