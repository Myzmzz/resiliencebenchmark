"""全部有效轮次结束后的独立只读核查；不删除任何对象。"""
from __future__ import annotations

import argparse
import json

from .admin import AdminAPI
from .common import CB, dump, kjson, quantity_cpu, target, utc
from .manage import ROOT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='final-environment-check.json')
    args = parser.parse_args()
    status = json.loads((ROOT / 'supervisor-status.json').read_text())
    assert status.get('complete') and status['completed_cells'] == 72 and not status['active']
    matrix = json.loads((ROOT / 'audit-matrix.json').read_text())
    assert len(matrix['facts']) == 72
    api = AdminAPI()
    result = {'at': utc(), 'source': '独立cluster-admin通道的实际只读核查', 'fault_objects': [], 'replicas': []}
    try:
        for fact in matrix['facts']:
            for obj in fact['actual_creates']:
                base = CB if obj['kind'] == 'ChaosBlade' else f'/apis/chaos-mesh.org/v1alpha1/namespaces/{fact["namespace"]}/stresschaos'
                path = base + '/' + obj['name']
                response = api.client.get(path)
                result['fault_objects'].append({'path': path, 'original_uid': obj['uid'], 'current_status': response.status_code})
        for number in range(1, 6):
            namespace = f'otel-demo-{number:02}'
            bound = target(namespace)
            metrics = api.get(f'/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods/{bound["name"]}')
            cart_metrics = [x for x in metrics['containers'] if x['name'] == 'cart']
            assert len(cart_metrics) == 1, '缺少唯一cart指标，不能当成零CPU'
            cpu = quantity_cpu(cart_metrics[0]['usage']['cpu'])
            query = f'sum by (method,status) (rate(app_frontend_requests_total{{k8s_namespace_name="{namespace}",service_name="frontend",target="/api/cart"}}[2m]))'
            data = api.get('/api/v1/namespaces/observability/services/http:prometheus:9090/proxy/api/v1/query', params={'query': query})
            values = data.get('data', {}).get('result', [])
            rps = sum(float(x['value'][1]) for x in values)
            errors = sum(float(x['value'][1]) for x in values if not x['metric'].get('status', '').startswith('2'))
            result['replicas'].append({'target': bound, 'ready': True, 'cpu_m': cpu,
                                       'metric_timestamp': metrics['timestamp'], 'metric_window': metrics['window'],
                                       'http_query': query, 'http_data': data, 'rps': rps, 'non_2xx_rps': errors,
                                       'healthy': 0 <= cpu < 50 and rps > 0 and errors == 0})
        result['nodes'] = [{'name': n['metadata']['name'], 'ready': any(c['type'] == 'Ready' and c['status'] == 'True' for c in n['status']['conditions'])}
                           for n in kjson('get', 'nodes')['items']]
        result['passed'] = (all(x['current_status'] == 404 for x in result['fault_objects'])
                            and all(x['healthy'] for x in result['replicas'])
                            and all(x['ready'] for x in result['nodes']))
        result['boundary'] = 'CPU单次新查询与既有连续采样相互补充；业务核查覆盖现有购物车GET/POST的HTTP结果，不等于全部业务语义或其它共享应用的功能验收。'
        dump(ROOT / args.output, result)
        assert result['passed'], '最终独立状态核查未通过，禁止据此宣称完成回收'
        print(f'核查通过：{len(result["fault_objects"])}个本轮实际故障对象均不存在；5个副本cart CPU低于50m、购物车请求有流量且无非2xx；节点均Ready。')
    finally:
        api.close()


if __name__ == '__main__':
    main()
