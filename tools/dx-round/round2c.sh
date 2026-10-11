#!/bin/zsh
# Dx round, part 2c (2026-09-11): resume D4-D6 after the DashScope arrearage stop.
#
# Around 14:22-14:33 UTC the Alibaba Model Studio (DashScope) account went into
# arrears: qwen3.8-max answered HTTP 400 "Arrearage", claude-code died mid-run
# (D4 x claude-code, CASE_INVALID) and round2b was stopped at 14:38 before
# D4 x deepseek-harness was submitted.  This part reruns D4 x claude-code, runs
# D4 x deepseek-harness, then D5 and D6 for all three harnesses, still on the
# 1807322 image (the 60309d3 rules go live between D6 and D7, user decision).
#
# Before each chain it waits until the gateway probe reports qwen3.8-max and the
# three harnesses runnable; if the account is still in arrears it stops instead
# of letting the chain turn every remaining run into a refusal or a crash.
# GET /options starts a re-probe once the last result has expired (takes minutes).
# IMAGE must name the deployed image, e.g. IMAGE="1807322+coroot".
HERE="${0:A:h}"
export IMAGE="${IMAGE:?set IMAGE to the deployed image label, e.g. 1807322+coroot}"
BASE=http://127.0.0.1:28080

wait_for_qwen() {
  local deadline=$(( $(date +%s) + 900 )) state
  while :; do
    state="$(curl -s --max-time 20 "$BASE/api/v1/stage2/options" | python3 -c '
import json, sys
d = json.load(sys.stdin)
probe = (d.get("model_probes") or {}).get("qwen3.8-max") or {}
rows = {h.get("harness"): h for h in d.get("harnesses") or []}
missing = [n for n in ("codex", "claude-code", "deepseek-harness") if not (rows.get(n) or {}).get("runnable")]
if probe.get("runnable") is True and not missing:
    print("ready")
else:
    print("qwen3.8-max runnable=%s status=%s; harnesses not runnable: %s"
          % (probe.get("runnable"), probe.get("probe_status"), ",".join(missing) or "none"))
' 2>/dev/null)"
    if [ "$state" = "ready" ]; then
      echo "model gate: qwen3.8-max and all three harnesses runnable at $(date -u +%H:%M:%S)"
      return 0
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo "STOP: model gate not passed after 15 min: ${state:-no answer from $BASE}"
      return 1
    fi
    echo "  $(date -u +%H:%M:%S) model gate waiting: ${state:-no answer}"
    sleep 30
  done
}

wait_for_qwen || exit 5
HARNESSES="claude-code deepseek-harness" zsh "$HERE/chain_dx.sh" D4 || exit $?
wait_for_qwen || exit 5
zsh "$HERE/chain_dx.sh" D5 D6
