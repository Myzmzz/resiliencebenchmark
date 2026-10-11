from __future__ import annotations

import concurrent.futures
import argparse
import json
import os
import queue
from pathlib import Path
import shutil
import threading
import time

from .common import CASES, MODELS, Ledger, dump, kjson, utc
from .driver import Driver
from .manage import ROOT, REPO, control, stop_agent


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--resume',action='store_true');args=parser.parse_args()
    qualification=json.loads((ROOT/'qualification/summary.json').read_text())
    if not qualification.get('passed'):
        raise RuntimeError('真实执行器资格验证未通过，禁止启动正式矩阵')
    if not json.loads((ROOT/'qualification/watchdog-outage-result.json').read_text()).get('passed'):
        raise RuntimeError('代理退出后的真实兜底清理仍未验证，禁止启动矩阵')
    if (ROOT/'campaign-status.json').exists() and not args.resume:
        raise RuntimeError('已有正式矩阵状态；先核对已有记录，不自动重复执行')
    guards=kjson('get','validatingadmissionpolicies,validatingadmissionpolicybindings')['items']
    expected={'resbench-chaosblade-replica-scope','resbench-bladeai-exec-scope','resbench-bladeai-chaosmesh-scope'}
    assert expected<={x['metadata']['name']for x in guards if x['kind']=='ValidatingAdmissionPolicy'}
    assert all('Deny'in x['spec']['validationActions']for x in guards if x['kind']=='ValidatingAdmissionPolicyBinding'and x['metadata']['name']in expected)
    if kjson('get','chaosblades')['items']:
        raise RuntimeError('开跑前仍有 ChaosBlade 对象，先核对归属')
    stamp=str(int(time.time()))
    snapshot=ROOT/('implementation-at-campaign-start' if not args.resume else 'implementation-at-resume-'+stamp)
    shutil.copytree(REPO/'scripts/bladeai_direct_eval',snapshot,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    prior=json.loads((ROOT/'campaign-status.json').read_text())if args.resume else None
    if prior:dump(ROOT/'campaign-history'/('before-resume-'+stamp+'.json'),prior)
    results=[]
    for model in MODELS:
        for case in CASES:
            cell=ROOT/'models'/model/case
            for run in sorted(cell.glob('attempt-*')):
                if not run.is_dir() or (run/'invalid-attempt.json').exists():continue
                if (run/'driver-result.json').exists() and (run/'cleanup.json').exists():
                    d=json.loads((run/'driver-result.json').read_text());cleanup=json.loads((run/'cleanup.json').read_text())
                    supplement=run/'business-http-evidence.json'
                    business_ok=cleanup.get('business_recovered') or supplement.exists()and json.loads(supplement.read_text()).get('application_http_healthy')
                    if cleanup.get('objects_absent') and cleanup.get('cpu_recovered') and business_ok:results.append({'case':case,'model':model,'outcome':d['outcome'],'path':str(run)})
    assert len({(x['model'],x['case'])for x in results})==len(results),'同一格已有多个未标无效的结果'
    status={'started_at':prior['started_at']if prior else utc(),'resumed_at':utc()if prior else None,'expected_cells':72,'completed_cells':len(results),'active':{},'results':results,'errors':[]}
    completed={(x['model'],x['case'])for x in results}
    lock=threading.Lock();halt=threading.Event();log=Ledger(ROOT/'campaign-timeline.jsonl')
    namespaces=queue.Queue()
    for number in range(1,5):namespaces.put(number)
    dump(ROOT/'campaign-status.json',status)

    def update():
        dump(ROOT/'campaign-status.json',status)

    def worker(slot,model):
        state=control(slot)
        if state.get('owned') and not state.get('cleanup_verified'):
            raise RuntimeError('实例仍未完成恢复核验')
        stop_agent(slot)
        for index,case in enumerate(CASES):
            if (model,case)in completed:continue
            if halt.is_set() or (ROOT/'PAUSE_DISPATCH').exists():return
            namespace_number=namespaces.get()
            namespace=f'otel-demo-{namespace_number:02}'
            cell=ROOT/'models'/model/case
            attempts=[int(p.name.split('-')[-1])for p in cell.glob('attempt-*')if p.is_dir()]
            run=cell/f'attempt-{max(attempts,default=0)+1:02}'
            with lock:
                status['active'][model]={'case':case,'namespace':namespace,'at':utc(),'path':str(run)};update()
            log.add('cell_started',model=model,case=case,slot=slot,namespace=namespace)
            print(f'开始 {model} × {case}，目标 {namespace}',flush=True)
            try:
                result=Driver(slot,case,model,namespace,run).execute()
            except Exception as exc:
                halt.set()
                error={'at':utc(),'model':model,'case':case,'error':type(exc).__name__,'detail':str(exc),'run':str(run)}
                with lock:status['errors'].append(error);update()
                log.add('campaign_safety_or_runtime_stop',**error)
                print(f'暂停新派发：{model} × {case}，{exc}',flush=True)
                return
            with lock:
                status['results'].append(result);status['completed_cells']+=1;status['active'].pop(model,None);update()
            log.add('cell_completed',**result)
            print(f'完成 {model} × {case}：{result["outcome"]}（待人工评分）',flush=True)
            namespaces.put(namespace_number)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4)as executor:
        futures=[executor.submit(worker,i,m)for i,m in enumerate(MODELS,1)]
        for future in concurrent.futures.as_completed(futures):future.result()
    status['finished_at']=utc();status['execution_complete']=status['completed_cells']==72
    dump(ROOT/'campaign-status.json',status)
    print(json.dumps({'completed':status['completed_cells'],'expected':72,'errors':status['errors']},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
