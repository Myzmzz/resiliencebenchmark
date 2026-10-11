"""按独立槽位接续长矩阵；可接管已存在的本轮独立进程，不重跑有效结果。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .common import CASES,MODELS,dump,utc,Ledger
from .manage import ROOT,REPO,control,stop_agent


def alive(record):
    p=subprocess.run(['ps','-p',str(record['pid']),'-o','command='],text=True,capture_output=True,timeout=3)
    return p.returncode==0 and str(record['run'])in p.stdout and 'bladeai_direct_eval.driver'in p.stdout


def completed():
    result={}
    for model in MODELS:
        for case in CASES:
            for run in sorted((ROOT/'models'/model/case).glob('attempt-*')):
                if not run.is_dir()or(run/'invalid-attempt.json').exists():continue
                if not(run/'driver-result.json').exists()or not(run/'cleanup.json').exists():continue
                driver=json.loads((run/'driver-result.json').read_text());cleanup=json.loads((run/'cleanup.json').read_text())
                extra=run/'business-http-evidence.json';business=cleanup.get('business_recovered')or(extra.exists()and json.loads(extra.read_text()).get('application_http_healthy'))
                if driver['outcome']not in {'finished','budget_exhausted'}:continue
                if cleanup.get('objects_absent')and cleanup.get('cpu_recovered')and business:
                    assert(model,case)not in result,'存在重复有效格'
                    result[(model,case)]={'model':model,'case':case,'run':str(run),'outcome':driver['outcome']}
    return result


def choose_work(slot, done, active):
    reserved = set(done) | {(r['model'], r['case']) for r in active}
    preferred = MODELS[slot-1]
    for model in [preferred] + [m for m in MODELS if m != preferred]:
        case = next((c for c in CASES if (model, c) not in reserved), None)
        if case is not None:
            return model, case
    return None


def launch(slot,model,case,namespace):
    parent=ROOT/'models'/model/case
    nums=[int(p.name.split('-')[-1])for p in parent.glob('attempt-*')if p.is_dir()]
    run=parent/f'attempt-{max(nums,default=0)+1:02}'
    stop_agent(slot)
    path=ROOT/'runtime'/f'supervisor-s{slot:02}-{int(time.time())}.log'
    with path.open('w')as log:
        proc=subprocess.Popen([sys.executable,'-m','bladeai_direct_eval.driver','--slot',str(slot),'--model',model,'--case',case,'--namespace',namespace,'--run',str(run)],cwd=REPO,env={**os.environ,'PYTHONPATH':str(REPO/'scripts')+':'+str(REPO)},stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    return {'slot':slot,'model':model,'case':case,'namespace':namespace,'run':str(run),'pid':proc.pid,'log':str(path),'started_at':utc()}


def main():
    legacy=json.loads((ROOT/'campaign-status.json').read_text())
    if not legacy.get('finished_at'):raise RuntimeError('旧批次尚未退出，不能接管')
    lock=ROOT/'supervisor.pid'
    if lock.exists():
        old=int(lock.read_text());p=subprocess.run(['ps','-p',str(old),'-o','command='],capture_output=True,text=True)
        if p.returncode==0 and 'bladeai_direct_eval.supervise'in p.stdout:raise RuntimeError('已有监督进程')
    lock.write_text(str(os.getpid()))
    active={}
    prior=ROOT/'supervisor-status.json'
    candidates=json.loads(prior.read_text()).get('active',[])if prior.exists()else[]
    individual=ROOT/'individual-dispatch.json'
    if individual.exists():candidates+=json.loads(individual.read_text())['runs']
    for r in candidates:
        if alive(r):active[r['slot']]=r
    log=Ledger(ROOT/'supervisor-timeline.jsonl');attention=[]
    log.add('supervisor_started',adopted=list(active.values()))
    try:
        while True:
            done=completed()
            for slot,r in list(active.items()):
                if alive(r):continue
                active.pop(slot)
                if(r['model'],r['case'])not in done:
                    issue={'at':utc(),'reason':'运行结束但缺少可判定结果或恢复核验','run':r};attention.append(issue);log.add('attention_required',**issue)
                else:
                    log.add('cell_completed',**r);print(f'完成 {r["model"]} × {r["case"]}',flush=True)
            if len(done)==72:
                dump(ROOT/'supervisor-status.json',{'at':utc(),'completed_cells':72,'expected_cells':72,'active':[],'attention':attention,'complete':True,'results':list(done.values())});break
            if not attention and not(ROOT/'PAUSE_DISPATCH').exists():
                occupied={r['namespace']for r in active.values()}
                for slot in range(1, len(MODELS)+1):
                    if slot in active:continue
                    work = choose_work(slot, done, active.values())
                    if work is None:continue
                    model, case = work
                    state=control(slot)
                    if not state.get('observer_done_at')or not state.get('cleanup_verified'):
                        attention.append({'at':utc(),'reason':'槽位恢复门禁未通过','slot':slot});break
                    namespace=next((f'otel-demo-{i:02}'for i in range(1,5)if f'otel-demo-{i:02}'not in occupied),None)
                    if namespace is None:break
                    r=launch(slot,model,case,namespace);active[slot]=r;occupied.add(namespace);log.add('cell_started',**r)
                    print(f'开始 {model} × {case}，{namespace}',flush=True)
            dump(ROOT/'supervisor-status.json',{'at':utc(),'completed_cells':len(done),'expected_cells':72,'active':list(active.values()),'attention':attention,'complete':False,'results':list(done.values())})
            if attention and not active:break
            if(ROOT/'PAUSE_DISPATCH').exists()and not active:break
            time.sleep(5)
    finally:
        lock.unlink(missing_ok=True)


if __name__=='__main__':main()
