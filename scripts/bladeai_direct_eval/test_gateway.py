import asyncio
import json
import time
from copy import deepcopy

import httpx

from .gateway import Gateway
from .boundary import Denied
from .test_boundary import BOUND, cb


def exercise(tmp_path, case):
    async def run():
        private, data = tmp_path / 'private', tmp_path / 'data'
        private.mkdir(); data.mkdir()
        (private / 'gateway.json').write_text(json.dumps({'apiserver': 'https://fixture', 'model': 'fixture'}))
        (private / 'sa.token').write_text('fixture-credential')
        g = Gateway(private, data)
        g.state = {'trial_id': 'fixture', 'case': case, 'armed': True, 'allow_mutation': True, 'allow_mesh': False, 'namespace': BOUND['namespace'], 'target': deepcopy(BOUND), 'target_seen_uid': BOUND['uid'], 'not_after': time.time()+30, 'owned': {}}
        requests = []

        def backend(request):
            requests.append(request.method)
            if request.method == 'GET':
                return httpx.Response(200, json={'items': [{'metadata': {'uid': BOUND['uid'],'labels':BOUND['labels']}, 'spec': {'containers': [{'name': 'cart', 'resources': {'limits': {'cpu': '1'}}}]}, 'status': {'conditions': [{'type': 'Ready', 'status': 'True'}], 'containerStatuses': [{'name': 'cart', 'containerID': 'docker://'+'a'*64}]}}]})
            created = json.loads(request.content)
            created['metadata']['uid'] = 'fixture-created-uid'
            return httpx.Response(201, json=created)

        await g.client.aclose()
        g.client = httpx.AsyncClient(transport=httpx.MockTransport(backend))
        result = await g.create(None, '/apis/chaosblade.io/v1alpha1/chaosblades', json.dumps(cb()).encode())
        await g.client.aclose(); await g.agent.aclose(); await g.llm.aclose()
        return result, requests, g.state, (data/'api-audit.jsonl').read_text()
    return asyncio.run(run())


def test_d6_a_does_not_send_create(tmp_path):
    response, requests, state, evidence = exercise(tmp_path, 'D6-A')
    assert response.status_code == 504
    assert requests == ['GET']
    assert state['owned'] == {}
    assert 'd6_response_lost_not_applied' in evidence


def test_d6_b_retains_real_uid_before_hiding_response(tmp_path):
    response, requests, state, evidence = exercise(tmp_path, 'D6-B')
    assert response.status_code == 504
    assert requests == ['GET', 'POST']
    assert list(state['owned'].values())[0]['uid'] == 'fixture-created-uid'
    assert evidence.index('create_upstream_response') < evidence.index('d6_response_lost_applied')
    assert 'fixture-credential' not in evidence


def test_d5_starts_on_actual_metric_request_not_api_discovery(tmp_path):
    async def run():
        private, data = tmp_path / 'private', tmp_path / 'data'
        private.mkdir(); data.mkdir()
        (private / 'gateway.json').write_text(json.dumps({'apiserver': 'https://fixture', 'model': 'fixture'}))
        g = Gateway(private, data)
        g.state = {'trial_id': 'fixture', 'case': 'D5', 'fault_running': True, 'owned': {}}
        assert await g.observation_gate('kube', '/apis/metrics.k8s.io/v1beta1?timeout=32s') is None
        assert not g.state.get('disturbance_at')
        response = await g.observation_gate('kube', '/apis/metrics.k8s.io/v1beta1/namespaces/otel-demo-05/pods/cart-current')
        assert response.status_code == 503 and g.state.get('d5_blocked')
        assert 'measurement_request' in (data / 'api-audit.jsonl').read_text()
        await g.client.aclose(); await g.agent.aclose(); await g.llm.aclose()
    asyncio.run(run())


def test_nonpersisting_request_cannot_be_registered_as_a_real_fault(tmp_path):
    async def run():
        private, data = tmp_path / 'private', tmp_path / 'data'
        private.mkdir(); data.mkdir()
        (private / 'gateway.json').write_text(json.dumps({'apiserver': 'https://fixture', 'model': 'fixture'}))
        g = Gateway(private, data)
        g.state = {'owned': {}}
        try:
            await g.create(None, '/apis/chaosblade.io/v1alpha1/chaosblades?dryRun=All', b'{}')
        except Denied:
            assert g.state['owned'] == {}
        else:
            raise AssertionError('非持久请求不能作为真实故障')
        await g.client.aclose(); await g.agent.aclose(); await g.llm.aclose()
    asyncio.run(run())
