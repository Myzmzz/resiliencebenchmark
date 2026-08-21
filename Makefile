.PHONY: validate test dry-run qualify probe-models-dry render-sock-shop materialize-sources verify-sources mirror-sock-shop-dry

validate:
	python3 scripts/benchmark_prepare.py validate-repo --repo .

test:
	python3 -m pytest

dry-run:
	python3 scripts/benchmark_prepare.py dry-run --repo .

probe-models-dry:
	python3 scripts/probe_models.py --models-config harness/models.yaml --dry-run

render-sock-shop:
	python3 scripts/render_sock_shop.py --config environment/kubernetes/sock-shop/render-config.yaml

materialize-sources:
	@test -n "$(SOURCE_DESTINATION)" || (echo "SOURCE_DESTINATION is required" >&2; exit 2)
	python3 scripts/materialize_sources.py --lockfile environment/shared/source-locks.yaml --destination "$(SOURCE_DESTINATION)"

verify-sources:
	@test -n "$(SOURCE_DESTINATION)" || (echo "SOURCE_DESTINATION is required" >&2; exit 2)
	python3 scripts/materialize_sources.py --lockfile environment/shared/source-locks.yaml --destination "$(SOURCE_DESTINATION)" --verify-existing

mirror-sock-shop-dry:
	python3 scripts/mirror_images.py --config environment/kubernetes/sock-shop/render-config.yaml

qualify:
	@test -n "$(KUBECONFIG_PATH)" || (echo "KUBECONFIG_PATH is required" >&2; exit 2)
	python3 scripts/benchmark_prepare.py qualify-cluster \
		--kubeconfig "$(KUBECONFIG_PATH)" \
		--namespace train-ticket \
		--namespace sock-shop \
		--namespace otel-demo \
		--observability-namespace observability \
		--output artifacts/qualification/cluster-readonly.json
