.PHONY: sync validate validate-workloads test test-mcp dry-run qualify inventory-images qualify-mcp-dry qualify-remote-dry probe-models-dry render-sock-shop materialize-sources verify-sources mirror-sock-shop-dry build-train-ticket-workload-dry deploy-mcp-dry activate-mcp-episode-dry deploy-deepseek-dry trial-dry

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

trial-dry:
	uv run python scripts/run_harness_trial.py --harness codex --model gpt-5.6

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
