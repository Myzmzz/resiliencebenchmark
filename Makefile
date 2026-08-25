.PHONY: sync validate validate-workloads test test-mcp dry-run qualify inventory-images qualify-mcp-dry qualify-remote-dry probe-models-dry render-sock-shop materialize-sources verify-sources mirror-sock-shop-dry build-train-ticket-workload-dry deploy-mcp-dry activate-mcp-episode-dry deploy-deepseek-dry deploy-app-dry reset-episode-dry trial-dry
.PHONY: resilience-agent-test resilience-agent-smoke-offline resilience-agent-configure-key resilience-agent-check-model
.PHONY: backend-dev frontend-dev frontend-build test-backend test-frontend

sync:
	uv sync --extra test

validate:
	uv run python scripts/benchmark_prepare.py validate-repo --repo .
	uv run python scripts/validate_workload_profiles.py

validate-workloads:
	uv run python scripts/validate_workload_profiles.py

test:
	uv run pytest

test-mcp:
	uv run pytest tests/test_http_runtime.py tests/test_k8s_ro_mcp.py tests/test_telemetry_ro_mcp.py tests/test_source_ro_mcp.py tests/test_chaos_control_mcp.py tests/test_qualify_mcp_endpoints.py tests/test_deploy_mcp_host.py tests/test_activate_mcp_episode.py tests/test_bladeai_mcp_template.py tests/test_harness_mcp_templates.py

dry-run:
	uv run python scripts/benchmark_prepare.py dry-run --repo .

probe-models-dry:
	uv run python scripts/probe_models.py --models-config harness/models.yaml --dry-run

qualify-mcp-dry:
	uv run python scripts/qualify_mcp_endpoints.py

qualify-remote-dry:
	uv run python scripts/qualify_remote_preparation.py

render-sock-shop:
	uv run python scripts/render_sock_shop.py --config environment/kubernetes/sock-shop/render-config.yaml

materialize-sources:
	@test -n "$(SOURCE_DESTINATION)" || (echo "SOURCE_DESTINATION is required" >&2; exit 2)
	uv run python scripts/materialize_sources.py --lockfile environment/shared/source-locks.yaml --destination "$(SOURCE_DESTINATION)"

verify-sources:
	@test -n "$(SOURCE_DESTINATION)" || (echo "SOURCE_DESTINATION is required" >&2; exit 2)
	uv run python scripts/materialize_sources.py --lockfile environment/shared/source-locks.yaml --destination "$(SOURCE_DESTINATION)" --verify-existing

mirror-sock-shop-dry:
	uv run python scripts/mirror_images.py --config environment/kubernetes/sock-shop/render-config.yaml

build-train-ticket-workload-dry:
	uv run python scripts/build_train_ticket_workload.py

deploy-mcp-dry:
	uv run python scripts/deploy_mcp_host.py

activate-mcp-episode-dry:
	uv run python scripts/activate_mcp_episode.py --episode tasks/examples/public/episode.timeout-missing.v0.1.yaml

deploy-deepseek-dry:
	uv run python scripts/deploy_deepseek_harness.py

deploy-app-dry:
	uv run python scripts/deploy_application.py --application otel-demo --mode activate

reset-episode-dry:
	uv run python scripts/reset_episode.py --application otel-demo --cleanup-handle cleanup-example-episode

trial-dry:
	uv run python scripts/run_harness_trial.py --harness codex --model gpt-5.6

resilience-agent-test:
	uv run pytest tests/test_resilience_agent.py tests/test_resilience_model_agent.py

resilience-agent-smoke-offline:
	uv run python -m resilience_agent run \
		--project resilience_agent/examples/minimal \
		--context resilience_agent/examples/minimal/system-context.yaml \
		--output-dir artifacts/resilience-agent-minimal-offline \
		--reasoning-mode deterministic

resilience-agent-configure-key:
	@security add-generic-password -U -a "$${USER}" -s resilience-agent-llm -w
	@echo "Stored resilience Agent credential in macOS Keychain service: resilience-agent-llm"

resilience-agent-check-model:
	@uv run python -c 'import json; from pathlib import Path; from resilience_agent.model_client import load_model_config; c=load_model_config(Path("resilience_agent/config/model.yaml")); print(json.dumps(c.public_dict(), ensure_ascii=False, indent=2))'

qualify:
	@test -n "$(KUBECONFIG_PATH)" || (echo "KUBECONFIG_PATH is required" >&2; exit 2)
	uv run python scripts/benchmark_prepare.py qualify-cluster \
		--kubeconfig "$(KUBECONFIG_PATH)" \
		--namespace train-ticket \
		--namespace sock-shop \
		--namespace otel-demo \
		--observability-namespace observability \
		--output artifacts/qualification/cluster-readonly.json

inventory-images:
	@test -n "$(KUBECONFIG_PATH)" || (echo "KUBECONFIG_PATH is required" >&2; exit 2)
	uv run python scripts/inventory_runtime_images.py \
		--kubeconfig "$(KUBECONFIG_PATH)" \
		--namespace train-ticket \
		--namespace sock-shop \
		--namespace otel-demo \
		--output artifacts/qualification/runtime-images.json

backend-dev:
	uv run uvicorn backend.main:app --reload --port 8000

frontend-dev:
	cd frontend && pnpm dev

frontend-build:
	cd frontend && pnpm build

test-backend:
	uv run pytest tests/test_health.py tests/test_config.py tests/test_common_models.py \
		tests/test_infrastructure_models.py tests/test_infrastructure_api.py \
		tests/test_environment_status_parser.py tests/test_integration_m2.py \
		tests/test_applications_api.py tests/test_episodes_api.py tests/test_experiment_api.py \
		tests/test_harnesses_api.py tests/test_mcp_tools_api.py tests/test_models_api.py \
		tests/test_observability_api.py

test-frontend:
	cd frontend && pnpm vitest run
