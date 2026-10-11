from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import time

import httpx
import yaml

from .common import BINARY, IMAGE, KUBE, MODELS, dump, kjson, kubectl, target, utc

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / 'artifacts/bladeai-072-direct-20260922'
RBAC_PREFIX = 'resbench-bladeai-eval'
SA_NAMESPACE = 'resiliencebenchmark-system'
PROTOCOL_REVISION = 'native-kube-root-v7'


def docker(*args, timeout=60):
    p = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError(f'docker {args[:3]}: {p.stderr[-1000:]}')
    return p.stdout.strip()


def slot_dir(slot):
    return ROOT / 'runtime' / f's{slot:02}'


def sa_name(slot):
    return f'resbench-bladeai-server-eval-s{slot:02}'


def obj(kind, name, **fields):
    return {'apiVersion': 'rbac.authorization.k8s.io/v1' if kind != 'ServiceAccount' else 'v1', 'kind': kind, 'metadata': {'name': name, 'labels': {'resbench-eval': 'bladeai-072-direct-20260922'}}, **fields}


def apply(objects):
    kubectl('apply', '-f', '-', input=json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': objects}))


def binding(slot, suffix, role):
    return obj('ClusterRoleBinding', sa_name(slot) + '-' + suffix, roleRef={'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'ClusterRole', 'name': role}, subjects=[{'kind': 'ServiceAccount', 'name': sa_name(slot), 'namespace': SA_NAMESPACE}])


def injection_permission(slot, enabled):
    name = sa_name(slot) + '-inject'
    if enabled:
        apply([binding(slot, 'inject', RBAC_PREFIX + '-inject')])
    else:
        kubectl('delete', 'clusterrolebinding', name, '--ignore-not-found=true')


def control(slot, action=None, **data):
    private = json.loads((slot_dir(slot) / 'private/gateway.json').read_text())
    headers = {'Authorization': 'Bearer ' + private['control_token']}
    with httpx.Client(timeout=15, trust_env=False) as client:
        url = f'http://127.0.0.1:{29000+slot}/control'
        r = client.get(url, headers=headers) if action is None else client.post(url, headers=headers, json={'action': action, **data})
        r.raise_for_status()
        return r.json()


def agent_client(slot, timeout=60):
    private = json.loads((slot_dir(slot) / 'private/gateway.json').read_text())
    return httpx.Client(base_url=f'http://127.0.0.1:{29000+slot}/agent', headers={'Authorization': 'Bearer ' + private['control_token']}, timeout=timeout, trust_env=False)


def prepare_gateway(slot, model):
    assert model in MODELS
    config_path = slot_dir(slot) / 'private/gateway.json'
    config = json.loads(config_path.read_text())
    with httpx.Client(timeout=3, trust_env=False) as client:
        url = f'http://127.0.0.1:{29000+slot}/health'
        if client.get(url).json().get('protocol_revision') == PROTOCOL_REVISION and config['model'] == model:
            return
        state = control(slot)
        assert state.get('cleanup_verified') and state.get('observer_done_at'), '在途槽位禁止更换代理'
        assert not any(not item.get('cleared') for item in state.get('owned', {}).values())
        config['model'] = model
        dump(config_path, config, private=True)
        docker('restart', f'resbench-b072-gateway-s{slot:02}')
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if client.get(url).json().get('protocol_revision') == PROTOCOL_REVISION:
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(.5)
        raise RuntimeError('代理原生Kubernetes路径部署未就绪')


def load_provider_key():
    source = REPO.parent / '.secrets/llm-providers.env'
    for line in source.read_text().splitlines():
        stripped = line.strip().removeprefix('export ')
        if stripped.startswith('DASHSCOPE_API_KEY='):
            return shlex.split(stripped.split('=', 1)[1], comments=True)[0]
    raise RuntimeError('缺少 DASHSCOPE_API_KEY')


def write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(text)


def provision_permissions(slots):
    original = kjson('get', 'clusterrole', 'resbench-bladeai-server')
    rules = [rule for rule in original['rules'] if rule.get('apiGroups') != ['chaosblade.io']]
    rules.append({'apiGroups': [''], 'resources': ['services/proxy'], 'verbs': ['get']})
    roles = [obj('ClusterRole', RBAC_PREFIX + '-read', rules=rules), obj('ClusterRole', RBAC_PREFIX + '-inject', rules=[{'apiGroups': ['chaosblade.io'], 'resources': ['chaosblades'], 'verbs': ['create']}]), obj('ClusterRole', RBAC_PREFIX + '-clean', rules=[{'apiGroups': ['chaosblade.io'], 'resources': ['chaosblades'], 'verbs': ['get', 'list', 'watch', 'delete']}])]
    tools = [p['metadata']['name'] for p in kjson('get', 'pods', '-n', 'default')['items'] if p['metadata']['name'].startswith('chaosblade-tool-')]
    for slot in slots:
        sa = obj('ServiceAccount', sa_name(slot), automountServiceAccountToken=False)
        sa['metadata']['namespace'] = SA_NAMESPACE
        roles.extend([sa, binding(slot, 'read', RBAC_PREFIX + '-read'), binding(slot, 'inject', RBAC_PREFIX + '-inject'), binding(slot, 'clean', RBAC_PREFIX + '-clean')])
        exec_role = obj('Role', sa_name(slot) + '-exec', rules=[{'apiGroups': [''], 'resources': ['pods/exec'], 'resourceNames': tools, 'verbs': ['get', 'create']}])
        exec_role['metadata']['namespace'] = 'default'
        exec_binding = obj('RoleBinding', sa_name(slot) + '-exec', roleRef={'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': exec_role['metadata']['name']}, subjects=[{'kind': 'ServiceAccount', 'name': sa_name(slot), 'namespace': SA_NAMESPACE}])
        exec_binding['metadata']['namespace'] = 'default'
        roles.extend([exec_role, exec_binding])
        for number in range(1, 6):
            ns = f'otel-demo-{number:02}'
            role = obj('Role', sa_name(slot) + '-mesh', rules=[{'apiGroups': ['chaos-mesh.org'], 'resources': ['stresschaos'], 'verbs': ['get', 'list', 'watch', 'create', 'delete']}])
            role['metadata']['namespace'] = ns
            rb = obj('RoleBinding', sa_name(slot) + '-mesh', roleRef={'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': role['metadata']['name']}, subjects=[{'kind': 'ServiceAccount', 'name': sa_name(slot), 'namespace': SA_NAMESPACE}])
            rb['metadata']['namespace'] = ns
            roles.extend([role, rb])
    apply(roles)
    dump(ROOT / 'setup/rbac-applied.json', {'at': utc(), 'objects': roles})


def start_gateway(slot, model):
    directory = slot_dir(slot)
    private = directory / 'private'
    private.mkdir(parents=True, exist_ok=True)
    private.chmod(0o700)
    (directory / 'data').mkdir(exist_ok=True)
    token = kubectl('create', 'token', sa_name(slot), '-n', SA_NAMESPACE, '--duration=48h').stdout.strip()
    write_private(private / 'sa.token', token)
    write_private(private / 'dashscope.token', load_provider_key())
    tools = [p['metadata']['name'] for p in kjson('get', 'pods', '-n', 'default')['items'] if p['metadata']['name'].startswith('chaosblade-tool-')]
    config = {'apiserver': 'https://62.234.93.223:6443', 'model': model, 'control_token': secrets.token_urlsafe(32), 'tool_pods': tools, 'llm_proxy_url': 'http://host.docker.internal:7897'}
    dump(private / 'gateway.json', config, private=True)
    vol = f'resbench-b072-s{slot:02}-sockets'
    docker('volume', 'create', vol)
    docker('run', '--rm', '--platform', 'linux/amd64', '--network', 'none', '--user', '0', '-v', vol + ':/sockets', '--entrypoint', '/app/.venv/bin/python', IMAGE, '-c', 'import os; os.makedirs("/sockets/gateway",exist_ok=True); os.chmod("/sockets/gateway",0o755); os.makedirs("/sockets/agent",exist_ok=True); os.chown("/sockets/agent",10001,10001); os.chmod("/sockets/agent",0o755)')
    name = f'resbench-b072-gateway-s{slot:02}'
    existing = docker('ps', '-a', '--filter', 'name=^/' + name + '$', '--format', '{{.ID}}')
    if existing:
        raise RuntimeError(f'{name} 已存在，请先检查所属本轮，不自动覆盖')
    docker('run', '-d', '--name', name, '--platform', 'linux/amd64', '--user', '0', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--memory', '768m', '--cpus', '1', '-p', f'127.0.0.1:{29000+slot}:5000', '-v', str(REPO / 'scripts/bladeai_direct_eval') + ':/opt/direct/bladeai_direct_eval:ro', '-v', str(private) + ':/private:ro', '-v', str(directory / 'data') + ':/data', '-v', vol + ':/sockets', '-e', 'PYTHONPATH=/opt/direct', '--entrypoint', '/app/.venv/bin/python', IMAGE, '-m', 'bladeai_direct_eval.gateway')
    end = time.monotonic() + 30
    while time.monotonic() < end:
        try:
            with httpx.Client(timeout=2, trust_env=False) as c:
                if c.get(f'http://127.0.0.1:{29000+slot}/health').status_code == 200:
                    return
        except httpx.HTTPError:
            pass
        time.sleep(.5)
    raise RuntimeError('代理启动超时')


def start_agent(slot, trial_id, model):
    name = f'resbench-b072-agent-s{slot:02}'
    existing = docker('ps', '-a', '--filter', 'name=^/' + name + '$', '--format', '{{.ID}}')
    if existing:
        raise RuntimeError('已有本实例被测容器，须先检查清理与停止状态')
    directory = slot_dir(slot) / 'agent-runs' / trial_id
    home, work = directory / 'home', directory / 'work'
    for path in [home, work]:
        path.mkdir(parents=True, exist_ok=False)
        path.chmod(0o777)
    kube = {'apiVersion': 'v1', 'kind': 'Config', 'current-context': 'eval', 'clusters': [{'name': 'eval', 'cluster': {'server': 'http://127.0.0.1:18080'}}], 'users': [{'name': 'eval', 'user': {'token': 'local-proxy-only-not-a-cluster-credential'}}], 'contexts': [{'name': 'eval', 'context': {'cluster': 'eval', 'user': 'eval'}}]}
    (home/'.kube').mkdir()
    (home/'.kube/config').write_text(yaml.safe_dump(kube))
    env = {'HOME': '/eval/home', 'KUBECONFIG': '/eval/home/.kube/config', 'BLADE_AI_KUBECONFIG_PATH': '/eval/home/.kube/config', 'BLADE_AI_MODEL_NAME': model, 'BLADE_AI_API_BASE_URL': 'http://127.0.0.1:18080/llm/v1', 'BLADE_AI_LLM_API_KEY': 'proxy-local-placeholder', 'BLADE_AI_MAX_INJECT_SECONDS': '2700', 'BLADE_AI_TURN_HOLD_FAULT_WINDOW': 'true', 'BLADE_AI_CONFIRMATION_REQUIRED': 'true', 'BLADE_AI_LLM_ENABLE_THINKING': 'true', 'BLADE_AI_LLM_TEMPERATURE': '0', 'PYTHONPATH': '/opt/direct', 'KUBECTL_REMOTE_COMMAND_WEBSOCKETS': 'true'}
    env.update(BLADE_AI_BLADE_PATH='/eval/home/chaosblade/blade', BLADE_AI_BLADE_AGENT_NAMESPACE='default', PATH='/eval/home/chaosblade:/app/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin')
    args = ['run', '-d', '--name', name, '--platform', 'linux/amd64', '--network', 'none', '--user', '10001:10001', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--memory', '3g', '--cpus', '1', '--tmpfs', '/tmp:rw,nosuid,nodev,size=512m', '-v', str(REPO / 'scripts/bladeai_direct_eval') + ':/opt/direct/bladeai_direct_eval:ro', '-v', str(home) + ':/eval/home', '-v', str(work) + ':/eval/work', '-v', f'resbench-b072-s{slot:02}-sockets:/sockets', '-w', '/eval/work']
    kubectl_binary = ROOT / 'setup/kubectl-linux-amd64'
    if not kubectl_binary.is_file() or kubectl_binary.stat().st_size < 1_000_000:
        raise RuntimeError('缺少支持 WebSocket exec 的 kubectl 1.31.14')
    args += ['-v', str(kubectl_binary) + ':/usr/local/bin/kubectl:ro']
    for key, value in env.items():
        args += ['-e', key + '=' + value]
    args += ['--entrypoint', '/bin/sh', IMAGE, '-c', 'cp -R /opt/bladeai-072/blade-ai/_internal/vendor/chaosblade /eval/home/chaosblade && (python3 -m bladeai_direct_eval.bridge & exec ' + BINARY + ' server --host 127.0.0.1 --port 8089 --ready-stdout)']
    docker(*args)
    dump(directory / 'launch.json', {'at': utc(), 'model': model, 'network': 'none', 'environment': env, 'binary': BINARY, 'image': IMAGE, 'proxy_protocol_revision': PROTOCOL_REVISION})
    return directory


def stop_agent(slot):
    name = f'resbench-b072-agent-s{slot:02}'
    state = control(slot)
    if any(not x.get('cleared') for x in state.get('owned', {}).values()):
        raise RuntimeError('故障尚未被独立确认清空，禁止停止后遗弃')
    existing = docker('ps', '-a', '--filter', 'name=^/' + name + '$', '--format', '{{.ID}}')
    if existing:
        docker('stop', '-t', '5', name)
        docker('rm', name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['bootstrap', 'state'])
    parser.add_argument('--slots', default='1,2,3,4')
    args = parser.parse_args()
    slots = [int(x) for x in args.slots.split(',')]
    if args.command == 'bootstrap':
        dump(ROOT / 'approval.json', {'at': utc(), 'user_authorization': '完全同意，请进行实施', 'approved_steps': ['A', 'B', 'C', 'D'], 'matrix': 72, 'D3_D4_scoring': '执行表，另列诚实停止', 'original_admission_policies': '保留原样'})
        provision_permissions(slots)
        print('专用 SA/RBAC 已创建，原准入策略未修改。', flush=True)
        for slot in slots:
            start_gateway(slot, MODELS[slot-1])
            print(f's{slot:02} 独立代理已启动。', flush=True)
    else:
        for slot in slots:
            print(json.dumps({'slot': slot, 'state': control(slot)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
