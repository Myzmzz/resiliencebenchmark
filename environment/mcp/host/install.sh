#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/opt/resiliencebenchmark/repo"
EXPECTED_HEAD=""
MATERIALIZE_SOURCES=false
UNIT_DIR="/etc/systemd/system"
ENV_DIR="/etc/resiliencebenchmark/mcp"
KUBECONFIG_DIR="/etc/resiliencebenchmark/kubeconfigs"
STATE_DIR="/var/lib/resiliencebenchmark"
SOURCE_ROOT="/opt/resiliencebenchmark/sources"
SOURCE_STATE_DIR="$STATE_DIR/source"
SOURCE_MANIFEST="$SOURCE_STATE_DIR/source-materialization.json"
ACTIVE_LEDGER_DIR="/var/lib/resiliencebenchmark/chaos-control/active"
BASELINE_LEDGER_DIR="/var/lib/resiliencebenchmark/chaos-control/baseline"

usage() {
  printf 'usage: %s [--repo /opt/resiliencebenchmark/repo] [--head <expected-git-head>] [--materialize-sources]\n' "$0" >&2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      REPO_DIR="$2"
      shift 2
      ;;
    --head)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      EXPECTED_HEAD="$2"
      shift 2
      ;;
    --materialize-sources)
      MATERIALIZE_SOURCES=true
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { printf 'install.sh must run as root\n' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf 'python3 is required\n' >&2; exit 1; }
command -v uv >/dev/null 2>&1 || { printf 'uv is required\n' >&2; exit 1; }
command -v systemctl >/dev/null 2>&1 || { printf 'systemd systemctl is required\n' >&2; exit 1; }
command -v getent >/dev/null 2>&1 || { printf 'getent is required\n' >&2; exit 1; }
command -v groupadd >/dev/null 2>&1 || { printf 'groupadd is required\n' >&2; exit 1; }
command -v useradd >/dev/null 2>&1 || { printf 'useradd is required\n' >&2; exit 1; }
if [ "$MATERIALIZE_SOURCES" = true ]; then
  command -v runuser >/dev/null 2>&1 || { printf 'runuser is required for source materialization\n' >&2; exit 1; }
  command -v git >/dev/null 2>&1 || { printf 'git is required for source materialization\n' >&2; exit 1; }
fi

python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10 or newer is required")
PY

[ -f "$REPO_DIR/pyproject.toml" ] || { printf 'repository path is missing pyproject.toml\n' >&2; exit 1; }
[ -f "$REPO_DIR/.resbench-head" ] || { printf 'repository path is missing .resbench-head\n' >&2; exit 1; }
HOST_SRC_DIR="$REPO_DIR/environment/mcp/host"
[ -d "$HOST_SRC_DIR/systemd" ] || { printf 'host systemd directory is missing\n' >&2; exit 1; }
[ -d "$HOST_SRC_DIR/env" ] || { printf 'host env example directory is missing\n' >&2; exit 1; }

if [ -n "$EXPECTED_HEAD" ]; then
  ACTUAL_HEAD="$(tr -d '\n' < "$REPO_DIR/.resbench-head")"
  [ "$ACTUAL_HEAD" = "$EXPECTED_HEAD" ] || { printf 'repository HEAD mismatch\n' >&2; exit 1; }
fi

for service_identity in \
  'k8s_ro:resbench-k8s-ro' \
  'telemetry_ro:resbench-telemetry-ro' \
  'source_ro:resbench-source-ro' \
  'chaos_control:resbench-chaos-control'; do
  service_name="${service_identity%%:*}"
  run_user="${service_identity#*:}"
  if ! getent group "$run_user" >/dev/null 2>&1; then
    groupadd --system "$run_user"
  fi
  if ! id -u "$run_user" >/dev/null 2>&1; then
    useradd --system --gid "$run_user" --home-dir "$STATE_DIR/$service_name" --shell /usr/sbin/nologin "$run_user"
  fi
done

install -d -m 0751 -o root -g root "$ENV_DIR" "$KUBECONFIG_DIR"
install -d -m 0755 -o root -g root "$STATE_DIR"
install -d -m 0750 -o resbench-source-ro -g resbench-source-ro "$SOURCE_STATE_DIR" "$SOURCE_ROOT"
install -d -m 0700 -o resbench-chaos-control -g resbench-chaos-control "$ACTIVE_LEDGER_DIR"
install -d -m 0700 -o resbench-chaos-control -g resbench-chaos-control "$BASELINE_LEDGER_DIR"

uv --directory "$REPO_DIR" sync --locked --extra test

if [ "$MATERIALIZE_SOURCES" = true ]; then
  SOURCE_ARGS=(
    "$REPO_DIR/.venv/bin/python"
    "$REPO_DIR/scripts/materialize_sources.py"
    --lockfile "$REPO_DIR/environment/shared/source-locks.yaml"
    --destination "$SOURCE_ROOT"
    --output "$SOURCE_MANIFEST"
  )
  if [ -n "$(find "$SOURCE_ROOT" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
    SOURCE_ARGS+=(--verify-existing)
  fi
  runuser -u resbench-source-ro -- "${SOURCE_ARGS[@]}"
fi

for name in k8s_ro telemetry_ro source_ro chaos_control; do
  example_src="$HOST_SRC_DIR/env/${name}.env.example"
  example_dst="$ENV_DIR/${name}.env.example"
  env_dst="$ENV_DIR/${name}.env"
  install -m 0640 "$example_src" "$example_dst"
  if [ ! -e "$env_dst" ]; then
    printf 'runtime env not created: %s\n' "$env_dst" >&2
  fi
done

for unit in "$HOST_SRC_DIR"/systemd/*.service; do
  install -m 0644 "$unit" "$UNIT_DIR/$(basename "$unit")"
done

systemctl daemon-reload

printf 'MCP host units installed. Runtime env files were not created or overwritten; services were not enabled or started.\n'
