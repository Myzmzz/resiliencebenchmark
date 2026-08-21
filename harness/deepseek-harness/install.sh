#!/usr/bin/env bash
set -euo pipefail

readonly DSH_PACKAGE='@deepseek-ai/dsh@0.1.0-rc.7'
readonly DSH_EXPECTED_INTEGRITY='sha512-ZceDCJ8FAywih+USW/OMk9jEhunlvJBGEz4kqrhau23hPzbciOazZrywH0nBRsaalSeAJ1JGBmjtw4OSjToStw=='
readonly DSH_LOCK_SHA256='3fd8d9fe3f91cc780d70dc443977edf077e054c756c1eb248b63fe2e64ad9f72'
readonly DSH_INSTALL_ROOT='/opt/resiliencebenchmark/deepseek-harness'
readonly DSH_NODE_BINARY="$DSH_INSTALL_ROOT/bin/node"
readonly DSH_BINARY="$DSH_INSTALL_ROOT/bin/dsh"
readonly DSH_STATE_ROOT='/var/lib/resiliencebenchmark'
readonly DSH_DEPENDENCY_TREE_FILE="$DSH_STATE_ROOT/deepseek-harness-dependency-tree.json"
readonly DSH_RUN_USER='resbench'
readonly DSH_MIN_AVAILABLE_MEMORY_KIB=4194304
readonly DSH_NODE_MAX_OLD_SPACE_MIB=2048
LOCK_DIR=''

usage() {
  echo 'usage: install.sh --lock-dir <runtime-lock-directory>' >&2
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --lock-dir)
      [[ "$#" -ge 2 ]] || { usage; exit 2; }
      LOCK_DIR="$2"
      shift 2
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

if [[ "$(id -u)" -ne 0 ]]; then
  echo 'install.sh must run as root on the authorized benchmark host' >&2
  exit 2
fi

for command_name in node npm getent groupadd useradd install mktemp sha256sum awk nice; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "required command is missing: $command_name" >&2
    exit 2
  }
done

[[ -n "$LOCK_DIR" && -f "$LOCK_DIR/package.json" && -f "$LOCK_DIR/package-lock.json" ]] || {
  echo 'complete runtime lock directory is required' >&2
  exit 2
}
actual_lock_sha256="$(sha256sum "$LOCK_DIR/package-lock.json" | awk '{print $1}')"
[[ "$actual_lock_sha256" == "$DSH_LOCK_SHA256" ]] || {
  echo 'runtime package-lock SHA-256 does not match the repository pin' >&2
  exit 1
}

node - "$LOCK_DIR/package-lock.json" "$DSH_EXPECTED_INTEGRITY" <<'NODE'
const fs = require('fs')
const lock = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'))
const expectedIntegrity = process.argv[3]
const top = lock.packages?.['node_modules/@deepseek-ai/dsh']
if (!top || top.version !== '0.1.0-rc.7' || top.integrity !== expectedIntegrity) {
  throw new Error('top-level DSH lock entry does not match the repository pin')
}
for (const [path, info] of Object.entries(lock.packages || {})) {
  if (/(^|\/)node_modules\/@deepseek-ai\/dsh(?:$|[^/]+$)/.test(path) && info.version !== '0.1.0-rc.7') {
    throw new Error('runtime lock contains a non-rc.7 DSH package')
  }
  if (path && info.resolved && !info.link && !info.integrity) {
    throw new Error('runtime lock contains a resolved package without integrity')
  }
}
NODE

node_major="$(node -p 'Number(process.versions.node.split(".")[0])')"
node_minor="$(node -p 'Number(process.versions.node.split(".")[1])')"
if (( node_major < 24 )) && ! (( node_major == 22 && node_minor >= 19 )); then
  echo "Node.js 22.19+ or 24+ is required; found $(node --version)" >&2
  exit 2
fi

available_memory_kib="$(awk '/^MemAvailable:/ {print $2; exit}' /proc/meminfo)"
if [[ -z "$available_memory_kib" || "$available_memory_kib" -lt "$DSH_MIN_AVAILABLE_MEMORY_KIB" ]]; then
  echo 'at least 4 GiB MemAvailable is required before DeepSeek Harness installation' >&2
  exit 2
fi
export NODE_OPTIONS="--max-old-space-size=$DSH_NODE_MAX_OLD_SPACE_MIB"

if ! getent group "$DSH_RUN_USER" >/dev/null; then
  groupadd --system "$DSH_RUN_USER"
fi
if ! getent passwd "$DSH_RUN_USER" >/dev/null; then
  useradd --system --gid "$DSH_RUN_USER" --home-dir "$DSH_STATE_ROOT" --create-home --shell /usr/sbin/nologin "$DSH_RUN_USER"
fi

install -d -o root -g root -m 0755 "$DSH_INSTALL_ROOT"
install -d -o root -g root -m 0755 "$DSH_INSTALL_ROOT/bin"
install -d -o "$DSH_RUN_USER" -g "$DSH_RUN_USER" -m 0750 "$DSH_STATE_ROOT" "$DSH_STATE_ROOT/trials"
install -o root -g root -m 0644 "$LOCK_DIR/package.json" "$DSH_INSTALL_ROOT/package.json"
install -o root -g root -m 0644 "$LOCK_DIR/package-lock.json" "$DSH_INSTALL_ROOT/package-lock.json"

if command -v ionice >/dev/null 2>&1; then
  ionice -c 2 -n 7 nice -n 10 npm ci --prefix "$DSH_INSTALL_ROOT" --omit=dev --ignore-scripts --no-audit --no-fund
else
  nice -n 10 npm ci --prefix "$DSH_INSTALL_ROOT" --omit=dev --ignore-scripts --no-audit --no-fund
fi

readonly dsh_package_entry="$DSH_INSTALL_ROOT/node_modules/@deepseek-ai/dsh/lib/bin.js"
if [[ ! -f "$dsh_package_entry" ]]; then
  echo "DeepSeek Harness package entry was not installed" >&2
  exit 1
fi
install -o root -g root -m 0755 "$(readlink -f "$(command -v node)")" "$DSH_NODE_BINARY"
wrapper_tmp="$(mktemp "$DSH_INSTALL_ROOT/.dsh-wrapper.XXXXXX")"
trap 'rm -f -- "$wrapper_tmp"' EXIT
printf '%s\n' \
  '#!/bin/sh' \
  'exec /opt/resiliencebenchmark/deepseek-harness/bin/node /opt/resiliencebenchmark/deepseek-harness/node_modules/@deepseek-ai/dsh/lib/bin.js "$@"' \
  >"$wrapper_tmp"
install -o root -g root -m 0755 "$wrapper_tmp" "$DSH_BINARY"
rm -f -- "$wrapper_tmp"
trap - EXIT

installed_version="$(npm --prefix "$DSH_INSTALL_ROOT" list @deepseek-ai/dsh --depth=0 --json | node -e 'let s="";process.stdin.on("data",d=>s+=d);process.stdin.on("end",()=>{const x=JSON.parse(s);process.stdout.write(x.dependencies["@deepseek-ai/dsh"].version)})')"
if [[ "$installed_version" != '0.1.0-rc.7' ]]; then
  echo "unexpected installed version: $installed_version" >&2
  exit 1
fi

dependency_tree_tmp="$(mktemp "$DSH_STATE_ROOT/.deepseek-harness-dependency-tree.XXXXXX")"
trap 'rm -f -- "$dependency_tree_tmp"' EXIT
npm --prefix "$DSH_INSTALL_ROOT" list --all --json >"$dependency_tree_tmp"
node - "$dependency_tree_tmp" <<'NODE'
const fs = require('fs')
const tree = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'))
function verify(dependencies) {
  for (const [name, info] of Object.entries(dependencies || {})) {
    if ((name === '@deepseek-ai/dsh' || name.startsWith('@deepseek-ai/dsh-')) && info.version !== '0.1.0-rc.7') {
      throw new Error('installed dependency tree contains a non-rc.7 DSH package')
    }
    verify(info.dependencies)
  }
}
verify(tree.dependencies)
NODE
install -o root -g root -m 0644 "$dependency_tree_tmp" "$DSH_DEPENDENCY_TREE_FILE"
rm -f -- "$dependency_tree_tmp"
trap - EXIT

echo "DeepSeek Harness installed: $DSH_BINARY ($installed_version)"
echo "Installed dependency tree recorded for matrix-freeze review."
echo 'No provider credential or shared Web Host was configured.'
