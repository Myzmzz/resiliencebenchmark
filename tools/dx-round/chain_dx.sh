#!/bin/zsh
# Run L0 x <cases> for the three harnesses, one run at a time, on the new cluster.
#
#   chain_dx.sh                       -> C0 D1 D2 D3 D4 D5 D6, each with codex, claude-code, deepseek-harness
#   chain_dx.sh D5 D6                 -> only those cases
#   chain_dx.sh D7-A D7-B D8-A D8-B   -> D7/D8 with their hint variant (needs the fix-branch platform)
#   HARNESSES="deepseek-harness" chain_dx.sh C0
#
# Safety between runs: no ChaosBlade experiment may be left undestroyed and no
# Chaos Mesh experiment may be left unrecovered (D8 moves injection to Chaos
# Mesh).  A residual fault would contaminate the next run, so the chain stops.
# A refused submission (HTTP 4xx) only skips that one run; anything else that
# is not a finished run stops the chain.
#
# PRE_RUN_HOOK (optional): a command run before every D7/D8 run with the
# arguments <case> <variant> <harness>, e.g. to refresh the platform's D7/D8
# qualification evidence (the D7 historical sample is bound to the live cart
# Pod UID, which D2 or an OOM restart changes).  If the hook fails, that run is
# skipped and recorded as SKIP, because the platform would void it anyway.

setopt NO_BG_NICE  # background jobs must not try to renice (fails in restricted shells)

HERE="${0:A:h}"
KCFG="${KCFG:-$HOME/.kube/resbench-new-config}"
MODEL="${MODEL:-qwen3.8-max}"
IMAGE="${IMAGE:-5746ecf}"
COOLDOWN="${COOLDOWN:-120}"   # seconds between runs, so one run's load does not leak into the next baseline
HARNESS_LIST=(${=HARNESSES:-codex claude-code deepseek-harness})
CASE_LIST=("$@")
[ ${#CASE_LIST[@]} -eq 0 ] && CASE_LIST=(C0 D1 D2 D3 D4 D5 D6)

k() { kubectl --kubeconfig "$KCFG" --request-timeout=30s "$@"; }

keep_tunnel() {
  while true; do
    if ! curl -s -m 5 http://127.0.0.1:28080/healthz >/dev/null 2>&1; then
      echo "  [tunnel] down at $(date -u +%H:%M:%S), re-creating"
      kubectl --kubeconfig "$KCFG" -n resiliencebenchmark-system \
        port-forward service/resbench-stage2-integration 28080:8080 >/dev/null 2>&1 &
      sleep 5
    fi
    sleep 15
  done
}

residual_once() {
  # ChaosBlade experiments that are not destroyed (API errors are printed too,
  # so an unreadable cluster counts as "not known to be clean").
  k get chaosblades.chaosblade.io \
    -o jsonpath='{range .items[*]}{.metadata.name}={.status.phase}{"\n"}{end}' 2>&1 \
    | grep -v '=Destroyed$' | grep -v '^$'
  # Chaos Mesh experiments that have not fully recovered.
  k get podchaos,networkchaos,stresschaos,iochaos,httpchaos,dnschaos,timechaos,jvmchaos,kernelchaos,blockchaos \
    -A -o json 2>&1 | python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    items = json.loads(raw).get("items", [])
except ValueError:
    print("chaos-mesh query failed: " + raw.strip()[:200])
    sys.exit(0)
for item in items:
    conditions = {c.get("type"): c.get("status") for c in (item.get("status") or {}).get("conditions") or []}
    if conditions.get("AllRecovered") != "True":
        meta = item["metadata"]
        print("%s/%s/%s AllInjected=%s AllRecovered=%s" % (item["kind"], meta["namespace"], meta["name"],
              conditions.get("AllInjected"), conditions.get("AllRecovered")))
'
}

check_residual() {
  # Three looks 20 s apart: platform cleanup can lag the run's terminal state
  # by a few seconds, and one failed API call must not stop a ten-hour chain.
  local label="$1" found=""
  for attempt in 1 2 3; do
    found=$(residual_once)
    [ -z "$found" ] && break
    sleep 20
  done
  echo "residual $label: ${found:-none}"
  if [ -n "$found" ]; then
    echo "STOP: residual fault $label"
    return 1
  fi
  return 0
}

keep_tunnel &
KEEPER=$!
trap 'kill $KEEPER 2>/dev/null; echo "chain Dx exit $(date -u +%H:%M:%S)"' EXIT

echo "chain Dx model=$MODEL image=$IMAGE cases=${CASE_LIST[*]} harnesses=${HARNESS_LIST[*]} start $(date -u +%H:%M:%S)"
check_residual "before start" || exit 2

first=1
for spec in $CASE_LIST; do
  # "D7-A" -> case D7, variant A; "D3" -> case D3, no variant.
  cid="${spec%%-*}"
  variant=""
  [ "$spec" != "$cid" ] && variant="${spec#*-}"
  for harness in $HARNESS_LIST; do
    if [ $first -eq 0 ]; then
      sleep "$COOLDOWN"
    fi
    first=0
    if [ -n "$variant" ] && [ -n "$PRE_RUN_HOOK" ]; then
      echo "  pre-run hook for $spec x $harness at $(date -u +%H:%M:%S)"
      if ! "$PRE_RUN_HOOK" "$cid" "$variant" "$harness"; then
        echo "SKIP: $spec x $harness pre-run hook failed"
        continue
      fi
    fi
    echo "===== L0 x $spec x $harness start $(date -u +%H:%M:%S)"
    if [ -n "$variant" ]; then
      python3 "$HERE/run_dx.py" --case "$cid" --variant "$variant" --harness "$harness" --model "$MODEL" --image "$IMAGE"
    else
      python3 "$HERE/run_dx.py" --case "$cid" --harness "$harness" --model "$MODEL" --image "$IMAGE"
    fi
    rc=$?
    echo "===== L0 x $spec x $harness end $(date -u +%H:%M:%S) rc=$rc"
    case $rc in
      0) ;;
      10) echo "SKIP: $spec x $harness refused by the API, continuing" ;;
      *) echo "STOP: runner rc=$rc for $spec x $harness"; exit 4 ;;
    esac
    check_residual "after $spec x $harness" || exit 3
  done
done
echo "chain Dx done $(date -u +%H:%M:%S)"
