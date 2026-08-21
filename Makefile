.PHONY: sync validate test test-mcp dry-run qualify probe-models-dry render-sock-shop materialize-sources verify-sources mirror-sock-shop-dry deploy-deepseek-dry

sync:
	uv sync --extra test

validate:
	uv run python scripts/benchmark_prepare.py validate-repo --repo .

test:
	uv run pytest

test-mcp:
	uv run pytest tests/test_http_runtime.py tests/test_k8s_ro_mcp.py tests/test_telemetry_ro_mcp.py tests/test_source_ro_mcp.py tests/test_chaos_control_mcp.py

dry-run:
	uv run python scripts/benchmark_prepare.py dry-run --repo .

probe-models-dry:
	uv run python scripts/probe_models.py --models-config harness/models.yaml --dry-run

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

deploy-deepseek-dry:
	uv run python scripts/deploy_deepseek_harness.py

qualify:
	@test -n "$(KUBECONFIG_PATH)" || (echo "KUBECONFIG_PATH is required" >&2; exit 2)
	uv run python scripts/benchmark_prepare.py qualify-cluster \
		--kubeconfig "$(KUBECONFIG_PATH)" \
		--namespace train-ticket \
		--namespace sock-shop \
		--namespace otel-demo \
		--observability-namespace observability \
		--output artifacts/qualification/cluster-readonly.json
