#!/bin/bash
# Copy one finished run's campaign directory out of the stage2 pod as a rescoring fixture.
#
# Usage: fetch_fixture.sh <run-dir-name>      e.g. L0-D4-codex-lxr-2fd3541741c741ad
#   - The campaign is found by its trial directory (<campaign>-<harness>-<case>-1),
#     newest first, and confirmed by the run's task id appearing in the campaign
#     records, so a same-named older campaign cannot be picked by mistake.
#   - stdout/stderr are left out (large, and not needed to recompute a decision).
#   - An existing fixture is never overwritten.
# New environment only (~/.kube/resbench-new-config).
# Run it outside the Claude sandbox: inside it, the 300 KB kubectl exec stream of
# L0-D4-codex was cut ("read message: unexpected EOF"); outside it worked first time.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(cd "$HERE/.." && pwd)"
NAME="${1:?usage: fetch_fixture.sh <run-dir-name>}"
KCFG="${KCFG:-$HOME/.kube/resbench-new-config}"
NS=resiliencebenchmark-system
ART=/var/lib/resbench-stage2/integration/artifacts
k() { kubectl --kubeconfig "$KCFG" --request-timeout=120s -n "$NS" "$@"; }

OUT="$SCRATCH/rescore-fixtures/$NAME.tgz"
if [ -e "$OUT" ]; then
  echo "exists, not refetched: $OUT"
  exit 0
fi

# L0-D4-codex-lxr-... -> case D4, harness codex (harness names may contain '-').
rest="${NAME#*-}"
CASE="${rest%%-*}"
rest="${rest#*-}"
HARNESS="${rest%-lxr-*}"
CASE_LOWER="$(printf '%s' "$CASE" | tr 'A-Z' 'a-z')"
TASK="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["task_id"])' "$HERE/runs/$NAME/summary.json")" || {
  echo "ABORT no task_id in runs/$NAME/summary.json"
  exit 2
}

POD="$(k get pods -o name | grep resbench-stage2-integration- | head -1)"
CAMPAIGN="$(k exec "$POD" -c stage2 -- sh -c '
cd "$1" || exit 1
for d in $(ls -dt campaign-*/campaign-*-"$2"-"$3"-1 2>/dev/null | head -n 5); do
  c=${d%%/*}
  if grep -q -s "$4" "$c"/campaign/*.json; then echo "$c"; exit 0; fi
done
exit 1' sh "$ART" "$HARNESS" "$CASE_LOWER" "$TASK")" || {
  echo "ABORT no campaign for $HARNESS/$CASE with task $TASK"
  exit 3
}

k exec "$POD" -c stage2 -- tar czf - -C "$ART" --exclude='stdout*' --exclude='stderr*' "$CAMPAIGN" >"$OUT.part" \
  && mv "$OUT.part" "$OUT" || { rm -f "$OUT.part"; echo "ABORT tar failed"; exit 4; }
echo "$NAME task=$TASK campaign=$CAMPAIGN size=$(wc -c <"$OUT" | tr -d ' ') members=$(tar tzf "$OUT" | wc -l | tr -d ' ') stdout_stderr=$(tar tzf "$OUT" | grep -c -E '(^|/)(stdout|stderr)')"
