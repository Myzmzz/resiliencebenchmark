#!/bin/bash
# Swap the NEW-environment Stage-2 deployment to a freshly built image pair.
#
# Usage: deploy_boundary.sh <build-metadata.json> [--dry-run]
#   <build-metadata.json>  written by build_boundary.sh (build-<sha>-image.json)
#   --dry-run              verify Harbor, validate the patch with a server-side
#                          dry run and stop; skips the idle gate, changes nothing.
#
# A real deploy:
#   1. both image refs resolve in Harbor to the digests the build recorded;
#   2. no Lx run is active (the deployment is Recreate: the old pod dies at once,
#      killing whatever runs in it);
#   3. one JSON patch replaces the 4 image refs (stage2, agent-runtime, the
#      agent-workspace-permissions initContainer, the source-head label). `test`
#      ops pin each index to the expected container name, so a reordered spec
#      fails instead of patching the wrong container. Env (the two Coroot
#      variables) is not touched;
#   4. rollout status, then print the new pod's images and the Coroot env;
#   5. restart the 28080 port-forward (it stays bound to the old, deleted pod);
#   6. wait until codex / claude-code / deepseek-harness are runnable with D7 and
#      D8 (the gateway probe takes ~3 min after a restart; "gateway_probe_in_progress"
#      until then is normal).
# Must run outside the Claude sandbox (it binds local port 28080).
# The cluster is the new Tencent environment: ~/.kube/resbench-new-config.
# (~/.kube/coroot-config is the OLD cluster, which has a deployment of the same name.)
set -u
META="${1:?usage: deploy_boundary.sh <build-metadata.json> [--dry-run]}"
MODE="${2:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
KCFG="${KCFG:-$HOME/.kube/resbench-new-config}"
NS=resiliencebenchmark-system
DEPLOY=resbench-stage2-integration
BASE=http://127.0.0.1:28080
k() { kubectl --kubeconfig "$KCFG" --request-timeout=30s -n "$NS" "$@"; }
log() { echo "$(date -u +%H:%M:%S) $*"; }

read -r HEAD_SHA CONTROLLER AGENT CONTROLLER_DIGEST AGENT_DIGEST < <(python3 - "$META" <<'EOF'
import json, sys
meta = json.load(open(sys.argv[1]))
print(meta["source_head"], meta["immutable_ref"], meta["agent_immutable_ref"], meta["digest"], meta["agent_digest"])
EOF
)
[ -n "${AGENT_DIGEST:-}" ] || { log "ABORT cannot read $META"; exit 2; }
log "build head=$HEAD_SHA"
log "controller=$CONTROLLER"
log "agent=$AGENT"

# 1. Harbor must hold exactly the digests the build recorded (tag -> digest).
verify_ref() {
  local ref="$1" want="$2" got
  got="$(crane digest --insecure "${ref%@*}" 2>&1)" || { log "ABORT ${ref%@*} not in Harbor: $got"; return 1; }
  [ "$got" = "$want" ] || { log "ABORT ${ref%@*} is $got, build recorded $want"; return 1; }
  case "$ref" in *"@$want") ;; *) log "ABORT $ref is not pinned to $want"; return 1 ;; esac
  log "ok Harbor ${ref%@*} = $want"
}
verify_ref "$CONTROLLER" "$CONTROLLER_DIGEST" || exit 3
verify_ref "$AGENT" "$AGENT_DIGEST" || exit 3

PATCH="$(python3 - "$CONTROLLER" "$AGENT" "$HEAD_SHA" <<'EOF'
import json, sys
controller, agent, head = sys.argv[1:4]
spec = "/spec/template/spec"
print(json.dumps([
    {"op": "test", "path": f"{spec}/initContainers/0/name", "value": "agent-workspace-permissions"},
    {"op": "test", "path": f"{spec}/containers/1/name", "value": "stage2"},
    {"op": "test", "path": f"{spec}/containers/2/name", "value": "agent-runtime"},
    {"op": "replace", "path": f"{spec}/initContainers/0/image", "value": agent},
    {"op": "replace", "path": f"{spec}/containers/1/image", "value": controller},
    {"op": "replace", "path": f"{spec}/containers/2/image", "value": agent},
    {"op": "replace", "path": "/spec/template/metadata/labels/resiliencebenchmark.io~1source-head", "value": head},
]))
EOF
)"

# Print what the pod template would carry (images, label, Coroot env) from a deployment JSON.
summarize() {
  python3 -c '
import json, sys
d = json.load(sys.stdin)
t = d["spec"]["template"]
print("  label source-head:", t["metadata"]["labels"].get("resiliencebenchmark.io/source-head"))
for kind in ("initContainers", "containers"):
    for c in t["spec"].get(kind, []):
        coroot = {e["name"]: e.get("value") for e in c.get("env", []) if e["name"].startswith("RESBENCH_COROOT")}
        print("  %s %s %s %s" % (kind, c["name"], c["image"], coroot or ""))
'
}

log "current deployment:"
k get deploy "$DEPLOY" -o json | summarize || { log "ABORT cannot read deployment"; exit 5; }
log "server-side dry run of the patch:"
k patch deploy "$DEPLOY" --type=json -p "$PATCH" --dry-run=server -o json | summarize || { log "ABORT patch rejected by the API server"; exit 5; }
if [ "$MODE" = "--dry-run" ]; then
  log "DRY_RUN_OK nothing changed"
  exit 0
fi

# 2. Idle gate: fail closed when the tunnel or the API does not answer.
active="$(curl -s --max-time 20 "$BASE/api/v1/stage2/lx/runs" | python3 -c '
import json, sys
body = json.load(sys.stdin)
print(",".join(r.get("run_id") or "?" for r in body.get("runs") or [] if r.get("terminal") is False) or "none")
' 2>/dev/null)"
if [ "$active" != "none" ]; then
  log "ABORT active run(s) or API unreachable: ${active:-no answer}"
  exit 4
fi
log "ok no active Lx run"

# 3 + 4. Patch and wait for the new pod.
k patch deploy "$DEPLOY" --type=json -p "$PATCH" || { log "ABORT patch failed"; exit 5; }
log "patched; waiting for rollout"
k rollout status "deploy/$DEPLOY" --timeout=15m || { log "ABORT rollout did not finish"; exit 6; }
log "deployment now:"
k get deploy "$DEPLOY" -o json | summarize
k get pods -o json | python3 -c '
import json, sys
for p in json.load(sys.stdin)["items"]:
    name = p["metadata"]["name"]
    if name.startswith("resbench-stage2-integration-") and not p["metadata"].get("deletionTimestamp"):
        print("  pod", name, p["status"].get("phase"))
        for s in p["status"].get("containerStatuses") or []:
            print("   ", s["name"], "ready" if s.get("ready") else "NOT READY", s.get("imageID", "")[-71:])
'

# 5. Restart the local tunnel (same command line as before, so pgrep keeps matching).
PF_CMD="kubectl --kubeconfig $KCFG -n $NS port-forward service/$DEPLOY 28080:8080"
for pid in $(pgrep -f "$PF_CMD"); do
  log "stopping old tunnel pid $pid"
  kill "$pid"
done
nohup kubectl --kubeconfig "$KCFG" -n "$NS" port-forward "service/$DEPLOY" 28080:8080 >>"$HERE/tunnel.log" 2>&1 &
disown
log "new tunnel pid $!"

# 6. Readiness: the three gated harnesses runnable with D7/D8 and capability loss runnable.
deadline=$(( $(date +%s) + 900 ))
while :; do
  state="$(curl -s --max-time 20 "$BASE/api/v1/stage2/options" | python3 -c '
import json, sys
d = json.load(sys.stdin)
rows = {h.get("harness"): h for h in d.get("harnesses") or []}
missing = []
for name in ("codex", "claude-code", "deepseek-harness"):
    h = rows.get(name) or {}
    if not h.get("runnable") or not {"D7", "D8"} <= set(h.get("runnable_cases") or []):
        missing.append(name + ":" + str(h.get("reason")))
if not (d.get("capability_loss") or {}).get("runnable"):
    missing.append("capability_loss")
print(" ".join(missing) or "ready")
' 2>/dev/null)"
  if [ "$state" = "ready" ]; then
    log "READY codex, claude-code, deepseek-harness runnable with D7/D8"
    exit 0
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    log "NOT_READY after 15 min: ${state:-no answer}"
    exit 7
  fi
  log "waiting: ${state:-no answer}"
  sleep 20
done
