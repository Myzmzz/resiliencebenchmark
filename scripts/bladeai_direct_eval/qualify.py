"""已获用户授权的真实执行器验证；这些记录不计入正式矩阵。"""
from __future__ import annotations

import json
import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

from .admin import AdminAPI
from .common import CB, Ledger, dump, target, utc
from .manage import ROOT, REPO, control, slot_dir


def proxy(method, path, body=None):
    code='import httpx,json; c=httpx.Client(timeout=90,trust_env=False); r=c.request('+repr(method)+',"http://127.0.0.1:18080"+'+repr(path)+',json='+repr(body)+'); print(json.dumps({"status":r.status_code,"body":r.json() if r.headers.get("content-type","").startswith("application/json") else r.text},ensure_ascii=False))'
    p=subprocess.run(['docker','exec','resbench-b072-agent-s01','python3','-c',code],capture_output=True,text=True,timeout=100)
    if p.returncode:raise RuntimeError(p.stderr[-500:])
    return json.loads(p.stdout)


def one(case):
    stamp=int(time.time());run=ROOT/'qualification'/f'{case}-{stamp}'
    run.mkdir(parents=True,exist_ok=False)
    log=Ledger(run/'qualification.jsonl');bound=target('otel-demo-05')
    control(1,'configure',trial_id=run.name,case=case,namespace=bound['namespace'],target=bound,allow_mutation=True,allow_mesh=case=='MESH',not_after=time.time()+900,request_actor='qualification_driver')
    log.add('qualification_started',case=case,target=bound)
    output=(run/'observer.log').open('w')
    native_ttl=300 if case=='WATCHDOG' else 180
    observer_contract=150 if case=='WATCHDOG' else 180
    observer=subprocess.Popen([sys.executable,'-m','bladeai_direct_eval.observer','--slot','1','--run',str(run),'--contract',str(observer_contract),'--grace','30'],cwd=REPO,env={**os.environ,'PYTHONPATH':str(REPO/'scripts')+':'+str(REPO)},stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
    dump(run/'observer-process.json',{'pid':observer.pid,'at':utc()})
    time.sleep(2)
    if observer.poll()is not None:raise RuntimeError('资格验证观察进程启动失败')
    observed=proxy('GET',f'/api/v1/namespaces/{bound["namespace"]}/pods?labelSelector=app.kubernetes.io%2Fname%3Dcart')
    assert observed['status']==200
    control(1,'update',armed=True)
    name=f'b072-qual-{case.lower()}-{stamp}'
    if case=='MESH':
        path=f'/apis/chaos-mesh.org/v1alpha1/namespaces/{bound["namespace"]}/stresschaos'
        body={'apiVersion':'chaos-mesh.org/v1alpha1','kind':'StressChaos','metadata':{'name':name,'namespace':bound['namespace']},'spec':{'mode':'one','selector':{'namespaces':[bound['namespace']],'pods':{bound['namespace']:[bound['name']]}},'containerNames':['cart'],'duration':'180s','stressors':{'cpu':{'workers':1,'load':80}}}}
    else:
        path=CB
        body={'apiVersion':'chaosblade.io/v1alpha1','kind':'ChaosBlade','metadata':{'name':name},'spec':{'experiments':[{'scope':'pod','target':'cpu','action':'fullload','matchers':[{'name':k,'value':[v]}for k,v in {'namespace':bound['namespace'],'names':bound['name'],'container-names':'cart','cpu-count':'1','cpu-percent':'80','timeout':str(native_ttl)}.items()]}]}}
    if case=='WATCHDOG':
        subprocess.run(['docker','exec','resbench-b072-agent-s01','python3','-c','import shutil; shutil.copytree("/opt/bladeai-072/blade-ai/_internal/vendor/chaosblade","/eval/home/chaosblade",dirs_exist_ok=True)'],check=True,capture_output=True,timeout=30)
        command=['/eval/home/chaosblade/blade','create','k8s','pod-cpu','load','--kubeconfig','/eval/home/.kube/config','--namespace',bound['namespace'],'--names',bound['name'],'--cpu-percent','80','--cpu-count','1','--timeout','300']
        dump(run/'requested-native-command.json',{'command':command})
        native=subprocess.run(['docker','exec','resbench-b072-agent-s01',*command],text=True,capture_output=True,timeout=90)
        log.add('native_cli_create_response',exit_code=native.returncode,stdout=native.stdout,stderr=native.stderr)
        if native.returncode:raise RuntimeError('原生 CLI 创建失败：'+native.stdout[:300]+native.stderr[:300])
        result=json.loads(native.stdout);assert result.get('success') is True,result
        s=control(1);assert len(s.get('owned',{}))==1,s
        first={'status':201,'body':{'native_result':result}}
    else:
        dump(run/'requested-fault.json',body)
        first=proxy('POST',path,body)
    log.add('first_create_response',response=first)
    if case=='D6-A':
        assert first['status']==504,first
        api=AdminAPI();r=api.client.get(path+'/'+name);log.add('independent_absent_after_d6_a',status=r.status_code);api.close();assert r.status_code==404
        assert proxy('GET',CB)['status']==200
        second=proxy('POST',path,body);log.add('reconciled_retry_response',response=second);assert second['status']==201,second
    elif case=='D6-B':
        assert first['status']==504,first
        api=AdminAPI();r=api.client.get(path+'/'+name);log.add('independent_present_after_d6_b',status=r.status_code,uid=r.json().get('metadata',{}).get('uid'));api.close();assert r.status_code==200
    else:
        assert first['status']==201,first
    # 模拟驱动已结束；观察和兜底必须继续独立运行。此处不调用任何 BladeAI cancel。
    control(1,'update',driver_done_at=time.time(),armed=False)
    log.add('driver_finished_while_fault_owned')
    print(f'{case}：真实故障已创建；等待独立 CPU、恢复和兜底证据。',flush=True)
    observer.wait(timeout=600);output.close()
    if observer.returncode:raise RuntimeError(f'{case} 观察进程失败，详见 {run}')
    cleanup=json.loads((run/'cleanup.json').read_text())
    rows=[line.split('\t')for line in (run/'cpu.tsv').read_text().splitlines()[1:]]
    cpu=[float(row[6])for row in rows if row[6]]
    evidence_samples=[x for x in cpu if 650<=x<=950]
    result={'at':utc(),'case':case,'effect_samples':evidence_samples,'peak_cpu_millicores':max(cpu)if cpu else None,'cleanup':cleanup,'passed':len(evidence_samples)>=2 and cleanup['objects_absent'] and cleanup['cpu_recovered'] and cleanup['business_recovered']}
    dump(run/'qualification-result.json',result)
    print(f'{case}：CPU 峰值 {result["peak_cpu_millicores"]}m，效果采样 {len(evidence_samples)}，对象清空 {cleanup["objects_absent"]}，CPU恢复 {cleanup["cpu_recovered"]}，业务恢复 {cleanup["business_recovered"]}，总判定 {result["passed"]}。',flush=True)
    if not result['passed']:raise RuntimeError(f'{case} 真实验证未通过，停止后续派发：{run}')
    return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--cases',default='CB,D6-A,D6-B,MESH');args=parser.parse_args()
    results=[]
    for case in args.cases.split(','):
        if case not in {'CB','D6-A','D6-B','MESH','WATCHDOG'}:raise ValueError('未知资格验证用例')
        results.append(one(case))
    latest={}
    for path in sorted((ROOT/'qualification').glob('*/qualification-result.json'),key=lambda p:p.stat().st_mtime):
        value=json.loads(path.read_text());latest[value['case']]={**value,'path':str(path)}
    expected={'CB','D6-A','D6-B','MESH','WATCHDOG'}
    outage=ROOT/'qualification/watchdog-outage-result.json'
    outage_ok=outage.exists() and json.loads(outage.read_text()).get('passed')
    passed=expected<=set(latest) and all(latest[k]['passed']for k in expected) and bool(outage_ok)
    dump(ROOT/'qualification/summary.json',{'at':utc(),'passed':passed,'results':list(latest.values()),'watchdog_outage_passed':bool(outage_ok)})
    print('本批资格验证完成；全部开跑门槛：'+('通过'if passed else'仍有待验证项目'),flush=True)


if __name__=='__main__':main()
