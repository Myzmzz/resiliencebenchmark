#!/bin/bash
# Build the paired controller + agent images for the D6 -> D7 boundary deploy.
#
# Usage: build_boundary.sh [worktree]
#   - Refuses to build from a dirty worktree: the image records the git head, so
#     uncommitted files would ship under a commit id that does not contain them.
#   - Refuses to build when either tag already exists in Harbor (tags are never
#     overwritten; a rebuilt image must get a new commit id).
#   - Writes metadata, rendered manifests and the log into the session scratchpad,
#     never into the repository (the default --render-dir would dirty the tree).
#   - Must run outside the Claude sandbox (Docker socket, Harbor, local proxy 7897).
# The build takes ~15 min on a cold cache, so run it with nohup in the background
# and wait for the BUILD_EXIT= line at the end of the log.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(cd "$HERE/.." && pwd)"
WORKTREE="${1:-$(cd "$HERE/../.." && pwd)}"   # 默认：本仓库根目录
PY="${PY:-uv run python}"                      # 需要 Python 3.12；默认走仓库的 uv 环境
BLADEAI_REPO="${BLADEAI_REPO:?设置 BLADEAI_REPO 指向 bladeai 上游仓库（约 200MB，不在本仓库内）}"
# Base images fetched ahead of time with crane (the builder cannot reach Docker Hub).
BASES="${BASES:?设置 BASES 指向三个离线基础镜像所在目录（约 200MB，不在本仓库内）}"
REPO="1.94.151.57:85/observe/resbench-stage2"

cd "$WORKTREE" || exit 2
if [ -n "$(git status --porcelain)" ]; then
  echo "ABORT worktree is dirty:"
  git status --porcelain | head -n 20
  exit 3
fi
HEAD_SHA="$(git rev-parse --short=7 HEAD)"
LOG="$SCRATCH/build-$HEAD_SHA.log"

for tag in "stage2-d0-$HEAD_SHA" "stage2-agent-$HEAD_SHA"; do
  if out="$(crane digest --insecure "$REPO:$tag" 2>&1)"; then
    echo "ABORT $REPO:$tag already exists ($out); refusing to overwrite a tag"
    exit 4
  fi
  case "$out" in
    *MANIFEST_UNKNOWN*|*NOT_FOUND*|*"not found"*) echo "ok: $tag is not in Harbor yet" ;;
    *) echo "ABORT cannot confirm $tag is absent from Harbor: $out"; exit 5 ;;
  esac
done

# name dir -> "name=oci-layout:///abs/dir@sha256:..." (digest from the layout index).
base_ref() {
  local digest
  digest="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1] + "/index.json"))["manifests"][0]["digest"])' "$BASES/$2")" || return 1
  printf '%s=oci-layout://%s@%s' "$1" "$BASES/$2" "$digest"
}
NODE24="$(base_ref node:24.13.0-bookworm-slim node-24.13.0-bookworm-slim)" || { echo "ABORT missing node 24 base"; exit 6; }
NODE22="$(base_ref node:22.21.1-bookworm-slim node-22.21.1-bookworm-slim)" || { echo "ABORT missing node 22 base"; exit 6; }
PY312="$(base_ref python:3.12.13-slim-bookworm python-3.12.13-slim-bookworm)" || { echo "ABORT missing python base"; exit 6; }

echo "building head=$HEAD_SHA, log: $LOG"
{
  echo "build start $(date -u +%H:%M:%S) head=$HEAD_SHA dirty=0"
  $PY scripts/build_stage2_image.py \
    --builder ischaos-builder \
    --bladeai-repo "$BLADEAI_REPO" \
    --agent-base-context "$NODE24" \
    --agent-base-context "$NODE22" \
    --agent-base-context "$PY312" \
    --agent-build-proxy http://host.docker.internal:7897 \
    --metadata "$SCRATCH/build-$HEAD_SHA-image.json" \
    --render-dir "$SCRATCH/build-$HEAD_SHA-rendered"
  rc=$?
  echo "build end $(date -u +%H:%M:%S) head=$HEAD_SHA dirty_after=$(git status --porcelain | wc -l | tr -d ' ')"
  echo "BUILD_EXIT=$rc"
} >>"$LOG" 2>&1
tail -n 3 "$LOG"
