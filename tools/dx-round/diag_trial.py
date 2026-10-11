"""Print why one fetched trial got its verdict.

Usage: python3 diag_trial.py <fixture.tgz>

Extracts the fixture once into ../rescore-extract/<fixture name>/ and reads only
that copy. Prints the trial's files and result record, the failed checks,
lifecycle event counts, the lifecycle events that decide the D3/D4 checks
(denials, rejections, verified/unverified claims), the agent's own assessment
fields, denied/auth-failure tool results, and which tools the agent used after
its first denial (did it switch to an observation path that was never revoked?).
Records a crashed trial never wrote are reported as missing instead of failing.
Written for the host python3 (3.9).
"""

import json
import pathlib
import re
import sys
import tarfile


def load_json(path):
    """Read one JSON record, or None when the trial never wrote it (e.g. the harness crashed)."""
    return json.loads(path.read_text()) if path.exists() else None


def scalar_fields(record, words):
    """Top-level scalar fields whose name contains one of ``words`` (for failure details)."""
    return {
        key: value for key, value in (record or {}).items()
        if any(word in key for word in words) and not isinstance(value, (dict, list))
    }


fixture = pathlib.Path(sys.argv[1]).resolve()
extract_dir = fixture.parent.parent / "rescore-extract" / fixture.name[: -len(".tgz")]
if not extract_dir.exists():
    extract_dir.mkdir(parents=True)
    with tarfile.open(fixture) as archive:
        archive.extractall(extract_dir)

campaign_dir = next(p for p in extract_dir.iterdir() if p.name.startswith("campaign-"))
trial_dir = next(
    p for p in campaign_dir.iterdir() if p.is_dir() and p.name.startswith(campaign_dir.name + "-")
)
controller_dir = campaign_dir / "trials" / trial_dir.name
print("controller files:", sorted(p.name for p in controller_dir.iterdir()))
print("trial files:", sorted(p.name for p in trial_dir.iterdir()))

result_record = load_json(controller_dir / "result.json") or {}
print("result.json keys:", list(result_record)[:25])
print("result.json:", json.dumps(
    {key: result_record[key] for key in ("status", "trial_validity", "failure", "reason_codes") if key in result_record},
    ensure_ascii=False)[:600])

decision = load_json(controller_dir / "evaluation-decision.json") or {}
print(
    "trial", trial_dir.name,
    "| experiment_verdict", decision.get("experiment_verdict"),
    "| agent_verdict", decision.get("agent_verdict"),
    "| agent_outcome", decision.get("agent_outcome"),
)
for check in decision.get("checks") or []:
    if check.get("passed") is False:
        print("  FAILED", check.get("rule_id"), "expected", check.get("expected"),
              "observed", check.get("observed"), "refs", check.get("evidence_refs"))

report = load_json(controller_dir / "harness-report.json") or {}
print("harness-report failure fields:", json.dumps(
    scalar_fields(report, ("error", "failure", "exit", "status", "reason", "timeout")), ensure_ascii=False)[:600])
lifecycle_events = report.get("lifecycle_events") or report.get("events") or []
kind_counts = {}
for event in lifecycle_events:
    kind_counts[event.get("kind")] = kind_counts.get(event.get("kind"), 0) + 1
print("lifecycle:", kind_counts)
deciding_kinds = {
    "permission_denied", "tool_request_rejected", "plan_rejected",
    "recovery_verified", "effect_verified", "recovery_unverified", "effect_unverified",
}
for event in lifecycle_events:
    if event.get("kind") in deciding_kinds:
        payload = event.get("payload") or {}
        shown = {key: payload[key] for key in payload if key not in ("native_call_id",)}
        print("  ", event.get("kind"), str(event.get("occurred_at", ""))[11:19],
              json.dumps(shown, ensure_ascii=False)[:300])

contradictions = (decision.get("effect_claim") or {}).get("contradictions")
if contradictions:
    print("effect_claim contradictions:", json.dumps(contradictions, ensure_ascii=False)[:500])

agent_result = load_json(trial_dir / "agent-result.json")
if agent_result is None:
    print("agent-result.json: MISSING")
else:
    wanted = ("assess", "verif", "claim", "outcome", "verdict", "effect", "recovery")
    picked = {key: value for key, value in agent_result.items() if any(word in key for word in wanted)}
    print("agent-result:", json.dumps(picked, ensure_ascii=False)[:700])

events_path = trial_dir / "canonical-events.jsonl"
if not events_path.exists():
    print("canonical-events.jsonl: MISSING")
    sys.exit(0)
rows = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
native_rows = [row for row in rows if row.get("source") == "native" and row.get("platform_sequence") is not None]
print("canonical events:", len(rows), "| native with sequence:", len(native_rows))
tool_by_call = {row.get("call_id"): row.get("tool") for row in native_rows if row.get("event_type") == "ToolCall"}

# A denial is a client-reported "denied" status, or an auth-failure text in a result
# that did not complete. Plain "forbidden" is left out on purpose: plan-validation
# findings such as SELECTOR_TARGET_FORBIDDEN are policy feedback, not a lost permission.
auth_failure = re.compile(
    r"invalid_token|Authentication required|re-authoriz|\b401\b|\b403\b|unauthori|permission denied",
    re.IGNORECASE,
)
denial_results = [
    row for row in native_rows
    if row.get("event_type") == "ToolResult"
    and (
        row.get("status") == "denied"
        or (row.get("status") != "completed"
            and auth_failure.search(json.dumps(row.get("payload"), ensure_ascii=False)))
    )
]
if not denial_results:
    print("no denied status or auth-failure text in native tool results")
    sys.exit(0)
first_sequence = min(row["platform_sequence"] for row in denial_results)
print("denied/auth-failure results:", len(denial_results), "| first at seq", first_sequence,
      "| sample:", json.dumps(denial_results[0].get("payload"), ensure_ascii=False)[:200])
after_counts = {}
for row in native_rows:
    if row.get("event_type") == "ToolResult" and row["platform_sequence"] > first_sequence:
        key = "%s:%s" % (tool_by_call.get(row.get("call_id"), "?"), row.get("status"))
        after_counts[key] = after_counts.get(key, 0) + 1
print("tool results after first denial:", dict(sorted(after_counts.items(), key=lambda kv: -kv[1])))
