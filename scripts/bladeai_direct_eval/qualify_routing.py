"""隔离临时客户端真实读取三个观测通道；不调用模型或创建故障。"""
from __future__ import annotations

import json
import time
import urllib.parse

from .common import IMAGE, dump, target, utc
from .manage import ROOT, REPO, PROTOCOL_REVISION, docker, slot_dir


def main():
    stamp = str(int(time.time()))
    directory = ROOT / 'runtime' / ('routing-qualification-' + stamp)
    directory.mkdir(mode=0o700)
    bound = target('otel-demo-05')
    dump(directory / 'state.json', {'trial_id': 'readonly-routing-' + stamp, 'case': 'ROUTING_QUALIFICATION',
                                  'namespace': bound['namespace'], 'target': bound, 'armed': False, 'owned': {},
                                  'fault_running': False, 'recovering': False, 'request_actor': 'qualification_driver'})
    volume = 'resbench-b072-routing-' + stamp
    gateway_name = 'resbench-b072-routing-gateway-' + stamp
    gateway_created = False
    docker('volume', 'create', '--label', 'resbench-eval=bladeai-072-direct-20260922', volume)
    try:
        docker('run', '--rm', '--platform', 'linux/amd64', '--network', 'none', '--user', '0', '-v', volume + ':/sockets',
               '--entrypoint', '/app/.venv/bin/python', IMAGE, '-c',
               'import os;os.makedirs("/sockets/gateway");os.makedirs("/sockets/agent");os.chown("/sockets/agent",10001,10001)')
        docker('run', '-d', '--name', gateway_name, '--platform', 'linux/amd64', '--user', '0', '--cap-drop=ALL',
               '--security-opt=no-new-privileges', '--memory', '512m', '-v', str(REPO / 'scripts/bladeai_direct_eval') + ':/opt/direct/bladeai_direct_eval:ro',
               '-v', str(slot_dir(2) / 'private') + ':/private:ro', '-v', str(directory) + ':/data', '-v', volume + ':/sockets',
               '-e', 'PYTHONPATH=/opt/direct', '--entrypoint', '/app/.venv/bin/python', IMAGE, '-m', 'bladeai_direct_eval.gateway')
        gateway_created = True
        ns = bound['namespace']
        business_query = f'sum(rate(app_frontend_requests_total{{k8s_namespace_name="{ns}",service_name="frontend",target="/api/cart",status="200"}}[2m]))'
        coroot_query = f'rate(container_resources_cpu_usage_seconds_total{{app_id="/k8s/{ns}/cart"}}[1m])'
        paths = {
            'metrics': f'/apis/metrics.k8s.io/v1beta1/namespaces/{ns}/pods/{bound["name"]}',
            'prometheus': '/api/v1/namespaces/observability/services/http:prometheus:9090/proxy/api/v1/query?' + urllib.parse.urlencode({'query': business_query}),
            'coroot': '/api/v1/namespaces/coroot/services/http:coroot-prometheus:9090/proxy/api/v1/query?' + urllib.parse.urlencode({'query': coroot_query}),
            'coroot_bare': '/api/v1/namespaces/coroot/services/coroot-prometheus:9090/proxy/api/v1/query?' + urllib.parse.urlencode({'query': coroot_query}),
            'coroot_named_port': '/api/v1/namespaces/coroot/services/coroot-prometheus:http/proxy/api/v1/query?' + urllib.parse.urlencode({'query': coroot_query}),
            'coroot_default_port': '/api/v1/namespaces/coroot/services/coroot-prometheus/proxy/api/v1/query?' + urllib.parse.urlencode({'query': coroot_query}),
            'outside_scope': '/api/v1/namespaces/kube-system/pods',
        }
        code = '''import subprocess,time,json,httpx
bridge=subprocess.Popen(["/app/.venv/bin/python","-m","bladeai_direct_eval.bridge"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
try:
 for attempt in range(30):
  try:
   if httpx.get("http://127.0.0.1:18080/version",timeout=1,trust_env=False).status_code==200:break
  except Exception:pass
  time.sleep(.5)
 base=["/eval/kubectl","--kubeconfig=/dev/null","--server=http://127.0.0.1:18080","--request-timeout=15s"]
 result=[]
 for channel,path in PATHS.items():
  command=base+["get","--raw="+path]
  r=subprocess.run(command,capture_output=True,text=True,timeout=20)
  result.append({"channel":channel,"path":path,"exit_code":r.returncode,"stdout":r.stdout,"stderr":r.stderr})
 print(json.dumps(result))
finally:bridge.terminate();bridge.wait(timeout=5)
'''.replace('PATHS', repr(paths))
        output = docker('run', '--rm', '--platform', 'linux/amd64', '--network', 'none', '--user', '10001:10001',
                        '--cap-drop=ALL', '--memory', '512m', '-v', str(REPO / 'scripts/bladeai_direct_eval') + ':/opt/direct/bladeai_direct_eval:ro',
                        '-v', str(ROOT / 'setup/kubectl-linux-amd64') + ':/eval/kubectl:ro', '-v', volume + ':/sockets',
                        '-e', 'PYTHONPATH=/opt/direct', '--entrypoint', '/app/.venv/bin/python', IMAGE, '-c', code, timeout=90)
        rows = json.loads(output)
        dump(directory / 'requests.json', rows)
        for row in rows:
            if row['channel'] == 'outside_scope':
                assert row['exit_code'] != 0 and 'Forbidden' in row['stderr'], row
                continue
            if row['channel'] == 'coroot_default_port':
                # 当前Service使用命名端口；Kubernetes原生无端口地址不能解析到端点。
                assert row['exit_code'] != 0 and 'no endpoints available' in row['stderr'], row
                row['qualified_observation_entry'] = False
                continue
            assert row['exit_code'] == 0, row
            row['qualified_observation_entry'] = True
            data = json.loads(row['stdout'])
            if row['channel'] == 'metrics': assert data.get('containers')
            else: assert data.get('data', {}).get('result'), row
        result = {'at': utc(), 'passed': True, 'rows': rows,
                                                               'scope': '真实kubectl经隔离桥与专用资格代理读取otel-demo-05；没有向模型提供诊断，没有创建故障',
                                                               'protocol_revision': PROTOCOL_REVISION}
        dump(directory / 'result.json', result)
        dump(ROOT / 'setup/native-routing-qualification.json', result)
        print('原生kubectl实际读取三个观测通道成功；范围外只读请求仍被拒绝。')
    finally:
        if gateway_created:
            docker('stop', '-t', '2', gateway_name)
            docker('rm', gateway_name)
        docker('volume', 'rm', volume)


if __name__ == '__main__':
    main()
