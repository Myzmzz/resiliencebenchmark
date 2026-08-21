#!/usr/bin/env bash
set -euo pipefail

readonly DSH_PACKAGE='@deepseek-ai/dsh@0.1.0-rc.7'
readonly DSH_EXPECTED_INTEGRITY='sha512-ZceDCJ8FAywih+USW/OMk9jEhunlvJBGEz4kqrhau23hPzbciOazZrywH0nBRsaalSeAJ1JGBmjtw4OSjToStw=='
readonly DSH_INSTALL_ROOT='/opt/resiliencebenchmark/deepseek-harness'
readonly DSH_STATE_ROOT='/var/lib/resiliencebenchmark'
readonly DSH_DEPENDENCY_TREE_FILE="$DSH_STATE_ROOT/deepseek-harness-dependency-tree.json"
readonly DSH_RUN_USER='resbench'

if [[ "$(id -u)" -ne 0 ]]; then
  echo 'install.sh must run as root on the authorized benchmark host' >&2
  exit 2
fi

for command_name in node npm getent groupadd useradd install mktemp; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "required command is missing: $command_name" >&2
    exit 2
  }
done

node_major="$(node -p 'Number(process.versions.node.split(".")[0])')"
node_minor="$(node -p 'Number(process.versions.node.split(".")[1])')"
if (( node_major < 24 )) && ! (( node_major == 22 && node_minor >= 19 )); then
  echo "Node.js 22.19+ or 24+ is required; found $(node --version)" >&2
  exit 2
fi

published_integrity="$(npm view "$DSH_PACKAGE" dist.integrity)"
if [[ "$published_integrity" != "$DSH_EXPECTED_INTEGRITY" ]]; then
  echo 'published npm integrity does not match the repository pin' >&2
  exit 1
fi

if ! getent group "$DSH_RUN_USER" >/dev/null; then
  groupadd --system "$DSH_RUN_USER"
fi
if ! getent passwd "$DSH_RUN_USER" >/dev/null; then
  useradd --system --gid "$DSH_RUN_USER" --home-dir "$DSH_STATE_ROOT" --create-home --shell /usr/sbin/nologin "$DSH_RUN_USER"
fi

install -d -o root -g root -m 0755 "$DSH_INSTALL_ROOT"
install -d -o "$DSH_RUN_USER" -g "$DSH_RUN_USER" -m 0750 "$DSH_STATE_ROOT" "$DSH_STATE_ROOT/trials"

npm install --prefix "$DSH_INSTALL_ROOT" --omit=dev --ignore-scripts "$DSH_PACKAGE"

readonly dsh_binary="$DSH_INSTALL_ROOT/node_modules/.bin/dsh"
if [[ ! -x "$dsh_binary" ]]; then
  echo "DeepSeek Harness binary was not installed at $dsh_binary" >&2
  exit 1
fi

installed_version="$(npm --prefix "$DSH_INSTALL_ROOT" list @deepseek-ai/dsh --depth=0 --json | node -e 'let s="";process.stdin.on("data",d=>s+=d);process.stdin.on("end",()=>{const x=JSON.parse(s);process.stdout.write(x.dependencies["@deepseek-ai/dsh"].version)})')"
if [[ "$installed_version" != '0.1.0-rc.7' ]]; then
  echo "unexpected installed version: $installed_version" >&2
  exit 1
fi

dependency_tree_tmp="$(mktemp "$DSH_STATE_ROOT/.deepseek-harness-dependency-tree.XXXXXX")"
trap 'rm -f -- "$dependency_tree_tmp"' EXIT
npm --prefix "$DSH_INSTALL_ROOT" list --all --json >"$dependency_tree_tmp"
install -o root -g root -m 0644 "$dependency_tree_tmp" "$DSH_DEPENDENCY_TREE_FILE"
rm -f -- "$dependency_tree_tmp"
trap - EXIT

echo "DeepSeek Harness installed: $dsh_binary ($installed_version)"
echo "Installed dependency tree recorded for matrix-freeze review."
echo 'No provider credential or shared Web Host was configured.'
