"""从现有 Prometheus 历史数据补采真正的 /api/cart 业务响应，不改变被测流量。"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .admin import AdminAPI
from .common import dump,utc,kjson
from .manage import ROOT


def collect(run,api):
    manifest=json.loads((run/'manifest.json').read_text())
    cleanup=json.loads((run/'cleanup.json').read_text())
    ns=manifest['target']['namespace']
    start=dt.datetime.fromisoformat(manifest['at']).timestamp()
    end=dt.datetime.fromisoformat(cleanup['at']).timestamp()
    query=f'sum by (k8s_pod_uid,method,status) (rate(app_frontend_requests_total{{k8s_namespace_name="{ns}",service_name="frontend",target="/api/cart"}}[2m]))'
    data=api.get('/api/v1/namespaces/observability/services/http:prometheus:9090/proxy/api/v1/query_range',params={'query':query,'start':start,'end':end,'step':20})
    rows=data.get('data',{}).get('result',[])
    last=[]
    for row in rows:
        if row.get('values'):last.append({'labels':row['metric'],'sample':row['values'][-1]})
    rps=sum(float(x['sample'][1])for x in last)
    bad=sum(float(x['sample'][1])for x in last if not x['labels'].get('status','').startswith('2'))
    result={'collected_at':utc(),'source':'现有业务请求计数器的历史查询；没有新增业务请求或更改模型可用工具','measurement_scope':{'namespace':ns,'service':'frontend','target':'/api/cart','methods':['GET','POST'],'downstream_target':manifest['target']},'source_time_range':{'start':start,'end':end,'step_seconds':20},'query':query,'data':data,'last_observed_rps':rps,'last_observed_non_2xx_rps':bad,'application_http_healthy':bool(last)and rps>0 and bad==0,'identity_note':'指标Pod UID属于frontend调用入口；Cart Pod UID单独记录，不混为同一对象。'}
    dump(run/'business-http-evidence.json',result)
    return {'model':manifest['model'],'case':manifest['case'],'rps':rps,'non_2xx_rps':bad,'healthy':result['application_http_healthy']}


def main():
    api=AdminAPI()
    try:
        profile=kjson('get','configmap','load-generator-resbench-cart-workload','-n','otel-demo-01')
        dump(ROOT/'setup/cart-workload-profile.json',{'at':utc(),'name':profile['metadata']['name'],'resourceVersion':profile['metadata']['resourceVersion'],'data':profile['data']})
        for run in sorted((ROOT/'models').glob('*/*/attempt-*')):
            if not run.is_dir()or not(run/'cleanup.json').exists()or not(run/'manifest.json').exists():continue
            if(run/'business-http-evidence.json').exists():continue
            print(json.dumps(collect(run,api),ensure_ascii=False),flush=True)
    finally:api.close()


if __name__=='__main__':main()
