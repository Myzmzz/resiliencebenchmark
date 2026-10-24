from copy import deepcopy

import pytest

from .boundary import Denied, permitted_exec, validate_cb, validate_mesh

BOUND = {'namespace': 'otel-demo-05', 'name': 'cart-abc-123', 'uid': 'uid-cart', 'labels': {'app.kubernetes.io/name': 'cart','opentelemetry.io/name':'cart','version':'2.2'}, 'unique_labels':{'app.kubernetes.io/name':'cart','opentelemetry.io/name':'cart'}}


def cb():
    return {'apiVersion': 'chaosblade.io/v1alpha1', 'kind': 'ChaosBlade', 'metadata': {'name': 'test-boundary'}, 'spec': {'experiments': [{'scope': 'pod', 'target': 'cpu', 'action': 'fullload', 'matchers': [{'name': k, 'value': [v]} for k, v in {'namespace': 'otel-demo-05', 'names': 'cart-abc-123', 'cpu-percent': '80', 'timeout': '300'}.items()]}]}}


def mesh():
    return {'apiVersion': 'chaos-mesh.org/v1alpha1', 'kind': 'StressChaos', 'metadata': {'name': 'test-mesh', 'namespace': 'otel-demo-05'}, 'spec': {'mode': 'one', 'selector': {'namespaces': ['otel-demo-05'], 'pods': {'otel-demo-05': ['cart-abc-123']}}, 'duration': '300s', 'stressors': {'cpu': {'workers': 1, 'load': 80}}, 'containerNames': ['cart']}}


def test_bounded_cpu_requests():
    assert validate_cb(cb(), BOUND)['target_uid'] == 'uid-cart'
    assert validate_mesh(mesh(), BOUND)['cpu_percent'] == 80


@pytest.mark.parametrize('count', ['1', '0', '-1', '2', '01', '1,2'])
def test_affected_count_only_allows_single_bound_target(count):
    obj = cb()
    obj['spec']['experiments'][0]['matchers'].append({'name': 'evict-count', 'value': [count]})
    if count == '1':
        assert validate_cb(obj, BOUND)['target_uid'] == 'uid-cart'
    else:
        with pytest.raises(Denied):
            validate_cb(obj, BOUND)


def test_bundled_cli_empty_defaults_are_safe_but_nonempty_overrides_are_not():
    obj=cb();m=obj['spec']['experiments'][0]['matchers']
    m.extend({'name':k,'value':['']}for k in ['labels','uid','chaosblade-path','chaosblade-deploy-mode','chaosblade-download-url','cpu-list','cpu-index','climb-time','evict-group','evict-count','evict-percent','endpoint'])
    m.append({'name':'cgroup-root','value':['/host-sys/fs/cgroup']})
    assert validate_cb(obj,BOUND)['cpu_percent']==80
    next(x for x in m if x['name']=='endpoint')['value']=['http://foreign/']
    with pytest.raises(Denied):validate_cb(obj,BOUND)


def test_alternative_unique_cart_label_is_valid_but_shared_label_is_not():
    obj=cb();m=obj['spec']['experiments'][0]['matchers'];m[:]=[x for x in m if x['name']!='names']
    m.append({'name':'labels','value':['opentelemetry.io/name=cart']})
    assert validate_cb(obj,BOUND)['target_uid']=='uid-cart'
    m[-1]['value']=['opentelemetry.io/name=cart,version=2.2']
    with pytest.raises(Denied):validate_cb(obj,BOUND)


@pytest.mark.parametrize('field,value', [('namespace', 'kube-system'), ('names', 'coredns-123'), ('cpu-percent', '100'), ('timeout', '2700'), ('cpu-count', '32'), ('container-ids', 'foreign'), ('labels', 'app.kubernetes.io/name!=cart')])
def test_cb_rejects_escape_and_excess(field, value):
    obj = cb()
    matchers = obj['spec']['experiments'][0]['matchers']
    matchers[:] = [m for m in matchers if m['name'] != field]
    matchers.append({'name': field, 'value': [value]})
    with pytest.raises(Denied):
        validate_cb(obj, BOUND)


def test_cb_rejects_duplicate_matcher_and_multiple_experiments():
    obj = cb()
    obj['spec']['experiments'][0]['matchers'].append({'name': 'namespace', 'value': ['otel-demo-05']})
    with pytest.raises(Denied):
        validate_cb(obj, BOUND)
    obj = cb()
    obj['spec']['experiments'].append(deepcopy(obj['spec']['experiments'][0]))
    with pytest.raises(Denied):
        validate_cb(obj, BOUND)


@pytest.mark.parametrize('change', ['cross_namespace', 'container', 'shell_stressor', 'excess_cpu', 'memory', 'remote', 'selector', 'long'])
def test_mesh_rejects_escape(change):
    obj = mesh()
    s = obj['spec']
    if change == 'cross_namespace': s['selector']['pods']['train-ticket'] = ['pod']
    if change == 'container': s['containerNames'] = ['valkey']
    if change == 'shell_stressor': s['stressngStressors'] = '--all 8'
    if change == 'excess_cpu': s['stressors']['cpu']['workers'] = 8
    if change == 'memory': s['stressors']['memory'] = {'workers': 1, 'size': '100%'}
    if change == 'remote': s['remoteCluster'] = 'other'
    if change == 'selector': s['selector'].pop('pods');s['selector']['nodes'] = ['controller']
    if change == 'long': s['duration'] = '900s'
    with pytest.raises(Denied):
        validate_mesh(obj, BOUND)


@pytest.mark.parametrize('cmd', [['sh', '-c', 'blade version'], ['cat', '/var/run/secrets/kubernetes.io/serviceaccount/token'], ['docker', 'ps'], ['blade', 'destroy', 'foreign'], ['blade', 'create', 'cpu'], ['blade', 'version', ';', 'id']])
def test_exec_only_direct_owned_actions(cmd):
    with pytest.raises(Denied):
        permitted_exec(cmd, {'owned'})
    assert permitted_exec(['blade', 'destroy', 'owned'], {'owned'}) == 'destroy'
    assert permitted_exec(['blade', 'version'], set()) == 'read'
