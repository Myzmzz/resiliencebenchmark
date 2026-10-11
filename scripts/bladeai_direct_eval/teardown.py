"""72格完成后回收本轮临时身份与本地实例；不删除任何业务或共享组件。"""
from __future__ import annotations

import json
import shutil

from .admin import AdminAPI
from .common import Ledger, dump, kjson, utc
from .manage import ROOT, control, docker, slot_dir, stop_agent


def object_path(obj):
    resources = {'ServiceAccount': 'serviceaccounts', 'Role': 'roles', 'RoleBinding': 'rolebindings',
                 'ClusterRole': 'clusterroles', 'ClusterRoleBinding': 'clusterrolebindings'}
    group = '/api/v1' if obj['kind'] == 'ServiceAccount' else '/apis/rbac.authorization.k8s.io/v1'
    namespace = obj['metadata'].get('namespace')
    return group + (f'/namespaces/{namespace}' if namespace else '') + '/' + resources[obj['kind']] + '/' + obj['metadata']['name']


def assert_owned(obj, expected):
    assert obj['kind'] == expected['kind']
    assert obj['metadata']['name'] == expected['metadata']['name']
    assert obj['metadata'].get('namespace') == expected['metadata'].get('namespace')
    assert obj['metadata'].get('labels', {}).get('resbench-eval') == 'bladeai-072-direct-20260922'
    for key in ('rules', 'roleRef', 'subjects', 'automountServiceAccountToken'):
        if key in expected: assert obj.get(key) == expected[key], f'{obj["metadata"]["name"]} 的 {key} 已变化'


def assert_instance_mounts(info, directory, volume):
    mounts = {m['Destination']: m for m in info.get('Mounts', [])}
    for name in ('private', 'data'):
        mount = mounts['/' + name]
        source = mount['Source']
        # Docker Desktop重启容器后可能返回Linux VM内的宿主挂载地址。
        if source.startswith('/host_mnt/Users/'):
            source = source[len('/host_mnt'):]
        assert mount['Type'] == 'bind' and source == str(directory / name), '本地容器归属不匹配'
    assert mounts['/sockets']['Type'] == 'volume' and mounts['/sockets']['Name'] == volume, 'socket卷归属不匹配'


def main():
    status = json.loads((ROOT / 'supervisor-status.json').read_text())
    assert status.get('complete') and status['completed_cells'] == 72 and not status['active'], '矩阵仍在执行，禁止回收'
    assert not status['attention'], '存在未解决的执行问题'
    score = json.loads((ROOT / 'scoring-status.json').read_text())
    assert score['scored_cells'] == 72, '人工核验尚未完成'
    audit = Ledger(ROOT / 'teardown.jsonl')
    admin = AdminAPI()
    try:
        original = json.loads((ROOT / 'preflight/20260922T224723Z/guards.json').read_text())['data']['items']
        current = kjson('get', 'validatingadmissionpolicies,validatingadmissionpolicybindings')['items']
        indexed = {(x['kind'], x['metadata']['name']): x for x in current}
        verified = []
        for old in original:
            key = old['kind'], old['metadata']['name']
            if not old['metadata']['name'].startswith('resbench-'): continue
            now = indexed[key]
            assert now['metadata']['uid'] == old['metadata']['uid'] and now['spec'] == old['spec'], '原有准入防护发生变化'
            verified.append({'kind': key[0], 'name': key[1], 'uid': now['metadata']['uid'], 'unchanged': True})
        assert len([x for x in verified if x['kind'] == 'ValidatingAdmissionPolicy']) == 3
        dump(ROOT / 'admission-policies-preserved.json', {'at': utc(), 'objects': verified})
        for slot in range(1, 5):
            state = control(slot)
            assert state.get('observer_done_at') and state.get('cleanup_verified'), '槽位清理尚未确认'
            for path, owned in state.get('owned', {}).items():
                response = admin.client.get(path)
                assert response.status_code == 404, f'尚有对象 {path}；不能回收观察与身份'
            stop_agent(slot)
            name = f'resbench-b072-gateway-s{slot:02}'
            info = json.loads(docker('inspect', name))[0]
            assert_instance_mounts(info, slot_dir(slot), f'resbench-b072-s{slot:02}-sockets')
            docker('stop', '-t', '5', name)
            docker('rm', name)
            docker('volume', 'rm', f'resbench-b072-s{slot:02}-sockets')
            audit.add('local_instance_removed', slot=slot)
        expected = json.loads((ROOT / 'setup/rbac-applied.json').read_text())['objects']
        order = {'RoleBinding': 0, 'ClusterRoleBinding': 0, 'ServiceAccount': 1, 'Role': 2, 'ClusterRole': 2}
        for obj in sorted(expected, key=lambda x: order[x['kind']]):
            path = object_path(obj)
            response = admin.client.get(path)
            if response.status_code == 404:
                audit.add('temporary_identity_already_absent', kind=obj['kind'], path=path)
                continue
            response.raise_for_status()
            actual = response.json()
            assert_owned(actual, obj)
            result = admin.delete_owned(path, actual['metadata']['uid'])
            assert admin.client.get(path).status_code == 404, f'临时身份未删除 {path}'
            audit.add('temporary_identity_removed', kind=obj['kind'], path=path, uid=actual['metadata']['uid'], status=result['status'])
        for slot in range(1, 5):
            private = slot_dir(slot) / 'private'
            assert private.parent == ROOT / 'runtime' / f's{slot:02}'
            shutil.rmtree(private)
            audit.add('temporary_credentials_removed', slot=slot)
        dump(ROOT / 'teardown-result.json', {'at': utc(), 'complete': True, 'temporary_identities': len(expected),
                                           'local_instances_removed': 4, 'credentials_removed': True,
                                           'admission_policies_unchanged': True, 'business_workloads_preserved': True})
        print('本轮实例、临时SA/RBAC与暂存凭据已回收；原有准入策略和共享业务保留。')
    finally:
        admin.close()


if __name__ == '__main__':
    main()
