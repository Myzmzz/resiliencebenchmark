#!/bin/zsh
# PRE_RUN_HOOK for chain_dx.sh (arguments: <case> <variant> <harness>).
#
# Before every D7 run, refresh the platform's D7 historical samples: the D7
# precheck only accepts a sample bound to the live target Pod UID, and D2 (or
# any Pod replacement) changes that UID.  D8 needs only the canaries, which the
# D7/D8 block runs once up front with a 24 h validity, so D8 runs pass through.
#
# The probe takes the platform runtime lock, so it can only run between runs;
# chain_dx.sh calls this hook after the previous run is terminal and before it
# submits the next one.  A non-zero exit makes the chain skip that run, which is
# what the platform would do anyway (it voids a D7 run without a valid sample).
case_id="$1"
variant="$2"
harness="$3"
[ "$case_id" != "D7" ] && exit 0

KCFG="${KCFG:-$HOME/.kube/resbench-new-config}"
NS=resiliencebenchmark-system
POD=$(kubectl --kubeconfig "$KCFG" -n "$NS" get pods -o name | grep resbench-stage2-integration- | head -1)
if [ -z "$POD" ]; then
  echo "  [hook] no stage2 pod found" >&2
  exit 1
fi
echo "  [hook] refreshing D7 samples before D7-$variant x $harness via $POD"
kubectl --kubeconfig "$KCFG" -n "$NS" exec "$POD" -c stage2 -- \
  python -m stage2_service.capability_loss.qualification_probe --d7 --namespace otel-demo --target cart --ttl-hours 24
