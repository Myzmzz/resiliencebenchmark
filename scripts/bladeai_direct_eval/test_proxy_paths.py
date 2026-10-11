import pytest
from .boundary import Denied, service_proxy_target


@pytest.mark.parametrize('identifier,port', [('http:coroot-prometheus:9090','9090'),('coroot-prometheus:9090','9090'),('coroot-prometheus:http','http'),('coroot-prometheus','')])
def test_equivalent_declared_service_proxy_identifiers(identifier, port):
    assert service_proxy_target('/api/v1/namespaces/coroot/services/'+identifier+'/proxy/api/v1/query?query=up') == ('coroot','coroot-prometheus',port,9090)


@pytest.mark.parametrize('path', ['/api/v1/nodes/node/proxy/stats?x=/services/http:prometheus:9090/proxy', '/api/v1/namespaces/other/services/prometheus:9090/proxy/', '/api/v1/namespaces/coroot/services/coroot-clickhouse:8123/proxy/', '/api/v1/namespaces/observability/services/prometheus:9090/proxy/api/v1/admin/tsdb/delete_series'])
def test_proxy_scope_cannot_be_expanded_with_aliases_or_query_text(path):
    with pytest.raises(Denied): service_proxy_target(path)
