"""仅在资格验证中停止本轮代理，证明独立看门狗仍能完成定向清理。"""
import json
import argparse
import subprocess
import time

from .admin import AdminAPI
from .common import Ledger, dump, utc
from .manage import ROOT, slot_dir, control


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--case',default='D6-A');parser.add_argument('--label',default='gateway-outage');args=parser.parse_args()
    log=Ledger(ROOT/'qualification'/(args.label+'.jsonl'))
    end=time.monotonic()+900
    state_path=slot_dir(1)/'data/state.json'
    while time.monotonic()<end:
        state=json.loads(state_path.read_text())
        active=[(p,v)for p,v in state.get('owned',{}).items()if not v.get('cleared')]
        if state.get('request_actor')=='qualification_driver' and state.get('case')==args.case and len(active)==1 and 30<=time.time()-active[0][1]['created_at']<=120:
            break
        time.sleep(2)
    else:
        raise RuntimeError('未遇到指定资格验证检查点；没有停止任何代理')
    path,owned=active[0];api=AdminAPI();actual=api.get(path)
    assert actual['metadata']['uid']==owned['uid']
    run=ROOT/'qualification'/state['trial_id']
    heartbeat_before=json.loads((run/'observer-heartbeat.json').read_text())
    stopped=time.time();log.add('qualification_gateway_stopping',path=path,uid=owned['uid'],case=state['case'])
    subprocess.run(['docker','stop','-t','1','resbench-b072-gateway-s01'],check=True,capture_output=True,timeout=15)
    print('资格验证：已停止本轮 s01 代理，独立观察/兜底继续运行。',flush=True)
    absent=False
    try:
        end=time.monotonic()+260
        while time.monotonic()<end:
            response=api.client.get(path)
            if response.status_code==404:
                absent=True;break
            time.sleep(2)
        heartbeat_after=json.loads((run/'observer-heartbeat.json').read_text())
        timeline=[json.loads(line)for line in(run/'timeline.jsonl').read_text().splitlines()]
        cleanup=[e for e in timeline if e['event']=='external_cleanup_requested' and e.get('path')==path and e['epoch']>=stopped]
        cpu=[json.loads(line)for line in(run/'ground-truth.jsonl').read_text().splitlines()if line.strip()]
        samples=[e for e in cpu if e['event']=='cpu_sample' and e['epoch']>=stopped]
        result={'at':utc(),'passed':absent and bool(cleanup) and heartbeat_after['epoch']>heartbeat_before['epoch'] and len(samples)>=2,'object_absent':absent,'external_cleanup_requests':cleanup,'cpu_samples_during_outage':len(samples),'heartbeat_before':heartbeat_before,'heartbeat_after':heartbeat_after,'source':'资格验证，不是模型动作'}
        dump(ROOT/'qualification'/(args.label+'-result.json'),result)
        log.add('independent_watchdog_after_proxy_exit',**result)
    finally:
        subprocess.run(['docker','start','resbench-b072-gateway-s01'],check=True,capture_output=True,timeout=15)
        time.sleep(2)
        if absent:
            latest=control(1)
            assert latest['trial_id']==state['trial_id']
            latest['owned'][path].update(cleared=True,cleared_at=time.time())
            control(1,'update',owned=latest['owned'],armed=False)
        log.add('qualification_gateway_restarted')
        api.close()
    print('代理退出后的独立清理验证：'+('通过'if result['passed']else'未通过'),flush=True)
    if not result['passed']:raise RuntimeError('独立兜底验证未通过')


if __name__=='__main__':main()
