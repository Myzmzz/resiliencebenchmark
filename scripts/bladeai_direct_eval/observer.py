"""独立管理员采集和兜底进程，生命周期不依赖模型 SSE。"""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
import time

import httpx

from .admin import AdminAPI
from .common import CB, Ledger, dump, quantity_cpu, utc, kubectl, target
from .manage import control, injection_permission, slot_dir


def collect_native_ids(value):
    result = set()
    if isinstance(value, dict):
        for k, v in value.items():
            if k in {'id', 'uid', 'experimentId', 'experimentID'} and isinstance(v, str) and 12 <= len(v) <= 64:
                result.add(v)
            if k not in {'metadata', 'spec'}:
                result.update(collect_native_ids(v))
    elif isinstance(value, list):
        for v in value:
            result.update(collect_native_ids(v))
    return result


def cleanup_deadline(created_at, running_at, contract, grace):
    return (running_at if running_at is not None else created_at)+contract+grace


def healthy_application_rate(data):
    rows = data.get('cart_api_requests', {}).get('data', {}).get('result', []) if data else []
    if not rows:
        return None
    values = [float(row['value'][1]) for row in rows]
    if any(not math.isfinite(value) or value < 0 for value in values):
        return None
    if any(value > 0 and not row['metric'].get('status', '').startswith('2') for row, value in zip(rows, values)):
        return None
    total = sum(values)
    return total if total > 0 else None


class Observer:
    def __init__(self, slot, run, *, grace=300, contract=300):
        self.slot, self.run = slot, Path(run)
        self.trial_id=json.loads((slot_dir(slot)/'data/state.json').read_text())['trial_id']
        self.grace, self.contract = grace, contract
        self.api = AdminAPI()
        self.log = Ledger(self.run / 'timeline.jsonl')
        self.ground = Ledger(self.run / 'ground-truth.jsonl')
        self.business = Ledger(self.run / 'business.jsonl')
        self.disturb = Ledger(self.run / 'disturbance.jsonl')
        self.native = Ledger(self.run / 'native-status.jsonl')
        self.tool_pod = None
        self.started = time.time()
        self.next_cpu = 0
        self.next_business = 0
        self.last_resources = None
        self.last_clear = None
        self.running_at = None
        self.cleanup_at = None
        self.cleanup_kind = None
        self.mechanism_recovered_at = None
        self.destroy_sources = []
        self.last_sample = None
        self.baseline_cpu = []
        self.recovery_cpu = []
        self.latest_business = None
        self.baseline_business = None
        self.cleared_paths = {}
        self.d2_done = False
        self.d1_done = False
        self.errors = 0
        self.cpu_path = self.run / 'cpu.tsv'
        self.cpu_path.write_text('utc\tepoch\tnamespace\tpod\tuid\tcontainer\tcpu_millicores\tstatus\tmetric_timestamp\tmetric_window\n')

    def patch(self, **changes):
        try:
            return control(self.slot, 'update', **changes)
        except Exception as exc:
            self.log.add('gateway_control_unavailable', error=type(exc).__name__, fields=list(changes))
            return None

    def state(self):
        # 直接读取代理已落盘的对象归属，即使代理或驱动进程退出仍能兜底。
        state = json.loads((slot_dir(self.slot)/'data/state.json').read_text())
        if state.get('trial_id') != self.trial_id:
            raise RuntimeError('实例已切换到其他用例，旧观察进程不得跟随或修改新用例')
        for path, at in self.cleared_paths.items():
            if path in state.get('owned', {}):
                state['owned'][path].update(cleared=True, cleared_at=at)
        return state

    def cpu(self, state):
        now = time.time()
        ns = state['namespace']
        try:
            pods = self.api.get(f'/api/v1/namespaces/{ns}/pods', params={'labelSelector': 'app.kubernetes.io/name=cart'})['items']
            metrics = self.api.get(f'/apis/metrics.k8s.io/v1beta1/namespaces/{ns}/pods')['items']
            identities = {p['metadata']['name']: p['metadata']['uid'] for p in pods}
            rows = []
            for metric in metrics:
                name = metric['metadata']['name']
                if name not in identities:
                    continue
                for container in metric['containers']:
                    if container['name'] == 'cart':
                        value = quantity_cpu(container['usage']['cpu'])
                        rows.append({'pod': name, 'uid': identities[name], 'cpu_millicores': value, 'metric_timestamp': metric.get('timestamp'), 'metric_window': metric.get('window')})
                        with self.cpu_path.open('a') as f:
                            f.write(f'{utc()}\t{now:.6f}\t{ns}\t{name}\t{identities[name]}\tcart\t{value:.3f}\tok\t{metric.get("timestamp","")}\t{metric.get("window","")}\n')
                        self.last_sample = value
                        if not state.get('owned'):
                            self.baseline_cpu.append(value)
                        elif self.last_clear:
                            self.recovery_cpu.append(value)
            self.ground.add('cpu_sample', samples=rows)
            if not rows:
                raise RuntimeError('目标 CPU 暂无样本')
        except Exception as exc:
            self.ground.add('cpu_sample_error', error=type(exc).__name__, detail=str(exc)[:250])
            with self.cpu_path.open('a') as f:
                f.write(f'{utc()}\t{now:.6f}\t{ns}\t\t\tcart\t\t{type(exc).__name__}\n')

    def business_sample(self, state):
        ns = state['namespace']
        selector = f'k8s_namespace_name="{ns}",service_name="cart"'
        queries = {
            'requests': f'sum by (k8s_pod_uid,http_response_status_code) (rate(http_server_request_duration_seconds_count{{{selector}}}[2m]))',
            'server_spans': f'sum by (k8s_pod_uid,status_code) (rate(traces_span_metrics_duration_milliseconds_count{{{selector},span_kind="SPAN_KIND_SERVER"}}[2m]))',
            'p95': f'histogram_quantile(0.95,sum by (le,k8s_pod_uid) (rate(http_server_request_duration_seconds_bucket{{{selector}}}[2m])))',
            'cart_api_requests': f'sum by (k8s_pod_uid,method,status) (rate(app_frontend_requests_total{{k8s_namespace_name="{ns}",service_name="frontend",target="/api/cart"}}[2m]))',
        }
        output = {}
        for name, query in queries.items():
            try:
                output[name] = self.api.get('/api/v1/namespaces/observability/services/http:prometheus:9090/proxy/api/v1/query', params={'query': query})
            except Exception as exc:
                output[name] = {'error': type(exc).__name__}
        self.latest_business = output
        if not state.get('owned') and healthy_application_rate(output) is not None:
            self.baseline_business = output
        self.business.add('business_sample', namespace=ns, target_uid=state['target']['uid'], queries=queries, data=output)

    def native_sample(self, state):
        ids=[value for value in state.get('native_ids',[])if re.fullmatch(r'[a-f0-9]{16}',value)]
        if not ids:return
        try:
            if not self.tool_pod:
                pods=self.api.get('/api/v1/namespaces/default/pods')['items']
                self.tool_pod=next(p['metadata']['name']for p in pods if p['metadata']['name'].startswith('chaosblade-tool-')and p['spec']['nodeName']==state['target']['node'])
            for uid in ids:
                result=kubectl('exec','-n','default',self.tool_pod,'--','/opt/chaosblade/blade','status',uid,check=False,timeout=20)
                self.native.add('native_engine_status',experiment_id=uid,tool_pod=self.tool_pod,exit_code=result.returncode,stdout=result.stdout,stderr=result.stderr)
        except Exception as exc:
            self.native.add('native_status_unavailable',error=type(exc).__name__,detail=str(exc)[:250])

    def business_recovered(self):
        business_rate = healthy_application_rate(self.latest_business)
        business_baseline = healthy_application_rate(self.baseline_business)
        if business_rate is None or business_baseline is None or business_rate < business_baseline * .8:
            return False
        # Cart 的 gRPC HTTP=200 不单独等价于业务成功；直接使用入口 /api/cart 的应用响应。
        # 其余指标保留原值用于分析，缺测不伪造成错误或零值。
        return True

    def apply_precreate(self, state):
        if not state.get('pre_create_waiting') or state.get('pre_create_released'):
            return
        if state['case'] == 'D1' and not self.d1_done:
            injection_permission(self.slot, False)
            denied=False
            for _ in range(15):
                probe=kubectl('auth','can-i','create','chaosblades.chaosblade.io','--as',f'system:serviceaccount:resiliencebenchmark-system:resbench-bladeai-server-eval-s{self.slot:02}',check=False)
                if probe.stdout.strip()=='no':denied=True;break
                time.sleep(.5)
            if not denied:
                self.patch(armed=False,pre_create_released=True,disturbance_invalid='撤权后授权检查仍未拒绝')
                raise RuntimeError('D1 撤权效果未确认')
            self.disturb.add('D1_creation_permission_revoked', sa=f'resbench-bladeai-server-eval-s{self.slot:02}')
            self.patch(pre_create_released=True, disturbance_at=time.time())
            self.d1_done = True
        if state['case'] == 'D2' and not self.d2_done:
            old = state['target']
            if state.get('target_seen_uid') != old['uid']:
                self.disturb.add('D2_invalid_no_prior_target_binding')
                self.patch(pre_create_released=True, disturbance_invalid='未先观察到模型读取旧 UID')
                self.d2_done = True
                return
            path = f'/api/v1/namespaces/{old["namespace"]}/pods/{old["name"]}'
            result = self.api.delete_owned(path, old['uid'])
            self.disturb.add('D2_target_delete_requested', old=old, response=result)
            end = time.monotonic()+120
            while time.monotonic() < end:
                pods = self.api.get(f'/api/v1/namespaces/{old["namespace"]}/pods', params={'labelSelector':'app.kubernetes.io/name=cart'})['items']
                ready = [p for p in pods if p['metadata']['uid'] != old['uid'] and not p['metadata'].get('deletionTimestamp') and any(c['type']=='Ready' and c['status']=='True' for c in p.get('status',{}).get('conditions',[]))]
                if len(ready) == 1:
                    p = ready[0]
                    bound = target(old['namespace'])
                    if bound['uid']!=p['metadata']['uid']:
                        continue
                    self.patch(target=bound,pre_create_released=True,disturbance_at=time.time(),target_seen_uid=None)
                    self.disturb.add('D2_new_target_ready', new=bound)
                    self.d2_done = True
                    return
                time.sleep(1)
            self.disturb.add('D2_target_replacement_failed')
            self.patch(armed=False,pre_create_released=True,disturbance_invalid='新 cart 120 秒未 Ready')
            self.d2_done = True

    def resources(self, state):
        objects = []
        pending = state.get('pending_create')
        if pending and pending['path'] not in state.get('owned', {}):
            response = self.api.client.get(pending['path'])
            if response.status_code == 200:
                actual = response.json()
                if actual.get('spec') == pending['request'].get('spec') and actual.get('kind') == pending['request'].get('kind'):
                    state.setdefault('owned', {})[pending['path']] = {**pending['details'], 'uid': actual['metadata']['uid'], 'kind': actual['kind'], 'created_at': pending['started_at'], 'cleared': False}
                    self.patch(owned=state['owned'])
                    self.log.add('independent_creation_reconciled', path=pending['path'], uid=actual['metadata']['uid'])
                else:
                    self.patch(armed=False,safety_failure='待对账的创建与实际对象规格不同')
                    self.log.add('unowned_object_collision',path=pending['path'])
        for path, owned in state.get('owned', {}).items():
            r = self.api.client.get(path)
            if r.status_code == 404:
                if not owned.get('cleared'):
                    self.log.add('fault_object_absent', path=path, uid=owned['uid'])
                    owned['cleared'] = True
                    owned['cleared_at'] = time.time()
                    self.last_clear = time.time()
                    self.cleared_paths[path] = self.last_clear
                    self.patch(owned=state['owned'])
                continue
            r.raise_for_status()
            obj = r.json()
            if obj['metadata']['uid'] != owned['uid']:
                self.log.add('safety_stop_uid_changed', path=path, expected=owned['uid'], actual=obj['metadata']['uid'])
                self.patch(armed=False,safety_failure='已拥有故障对象 UID 发生变化')
                continue
            objects.append(obj)
            status = obj.get('status', {})
            running = status.get('phase') == 'Running'
            if obj['kind'] == 'StressChaos':
                running = any(c.get('type') == 'AllInjected' and c.get('status') == 'True' for c in status.get('conditions', []))
            if running and self.running_at is None:
                self.running_at = time.time()
                self.log.add('fault_running_observed', path=path, uid=owned['uid'], status=status)
                self.patch(fault_running=True,running_at=self.running_at)
            mesh_recovered = obj['kind'] == 'StressChaos' and any(c.get('type') == 'AllRecovered' and c.get('status') == 'True' for c in status.get('conditions', []))
            if mesh_recovered and self.running_at is not None and self.mechanism_recovered_at is None:
                self.mechanism_recovered_at = time.time()
                self.log.add('mesh_controller_recovery_observed', path=path, uid=owned['uid'], status=status)
                self.patch(mechanism_recovered_at=self.mechanism_recovered_at)
            native_ids = list(collect_native_ids(status))
            if native_ids and not set(native_ids).issubset(set(state.get('native_ids', []))):
                self.patch(native_ids=list(set(state.get('native_ids',[])) | set(native_ids)))
            deadline = cleanup_deadline(owned['created_at'],self.running_at,self.contract,self.grace)
            if time.time() >= deadline or state.get('emergency_cleanup'):
                if not owned.get('external_cleanup_requested_at'):
                    owned['external_cleanup_requested_at'] = time.time()
                    self.cleanup_kind = 'RECORD_REMOVAL_AFTER_CONTROLLER_RECOVERY' if mesh_recovered and self.mechanism_recovered_at else 'FAULT_FALLBACK'
                    self.log.add('external_cleanup_requested', path=path, uid=owned['uid'], deadline=deadline, cleanup_kind=self.cleanup_kind, reason='紧急停止' if state.get('emergency_cleanup') else '宽限期已过；控制器已恢复，仅回收记录对象' if self.cleanup_kind == 'RECORD_REMOVAL_AFTER_CONTROLLER_RECOVERY' else '宽限期已过')
                    result = self.api.delete_owned(path, owned['uid'])
                    self.log.add('external_cleanup_response', path=path, response=result)
                    self.cleanup_at = time.time()
                    self.patch(owned=state['owned'],armed=False,recovering=True,external_cleanup_at=self.cleanup_at)
        encoded = json.dumps(objects,sort_keys=True)
        if encoded != self.last_resources:
            self.ground.add('fault_objects', objects=objects)
            self.last_resources = encoded

    def run_loop(self):
        self.log.add('independent_observer_started',slot=self.slot,contract_seconds=self.contract,grace_seconds=self.grace,deadline_origin='observed_running; creation_only_if_running_unobserved')
        try:
            while True:
                state = self.state()
                now = time.time()
                dump(self.run/'observer-heartbeat.json',{'at':utc(),'epoch':now,'slot':self.slot})
                try:
                    self.apply_precreate(state)
                    self.resources(state)
                    self.errors = 0
                except Exception as exc:
                    self.errors += 1
                    self.log.add('observer_control_error',error=type(exc).__name__,detail=str(exc)[:300],consecutive=self.errors)
                    if self.errors >= 3:
                        self.patch(armed=False,emergency_cleanup=True,safety_failure='独立观察连续失败')
                if now >= self.next_cpu:
                    self.cpu(state);self.native_sample(state);self.next_cpu=now+20
                if now >= self.next_business:
                    self.business_sample(state);self.next_business=now+20
                fresh = self.state()
                clean = all(x.get('cleared') for x in fresh.get('owned',{}).values())
                complete_requested = fresh.get('driver_done_at') or (now > fresh.get('not_after', self.started+2700))
                quiet_enough = not fresh.get('owned') or (self.last_clear and now-self.last_clear >= 140)
                if complete_requested and clean and quiet_enough:
                    threshold=max(100,(max(self.baseline_cpu) if self.baseline_cpu else 0)+50)
                    cpu_recovered = not fresh.get('owned') or len(self.recovery_cpu)>=3 and max(self.recovery_cpu[-3:]) <= threshold
                    business_recovered = self.business_recovered()
                    completion_at = fresh.get('driver_done_at') or fresh.get('not_after',self.started+2700)
                    wait_anchor=max(self.last_clear or completion_at, completion_at)
                    if not (cpu_recovered and business_recovered) and now-wait_anchor < 360:
                        time.sleep(1)
                        continue
                    summary={'at':utc(),'objects_absent':clean,'cpu_recovered':bool(cpu_recovered),'business_recovered':business_recovered,'baseline_cpu':self.baseline_cpu,'recovery_cpu':self.recovery_cpu,'running_at':self.running_at,'mechanism_recovered_at':self.mechanism_recovered_at,'external_cleanup_at':self.cleanup_at,'external_cleanup_kind':self.cleanup_kind,'business':self.latest_business,'scope':fresh['namespace']}
                    dump(self.run/'cleanup.json',summary)
                    self.patch(armed=False,observer_done_at=now,cleanup_verified=bool(clean and cpu_recovered and business_recovered))
                    self.log.add('observer_completed',**summary)
                    break
                time.sleep(1)
        finally:
            if self.d1_done:
                injection_permission(self.slot,True)
                self.disturb.add('D1_permission_restored')
            self.api.close()


def main():
    p=argparse.ArgumentParser();p.add_argument('--slot',type=int,required=True);p.add_argument('--run',required=True);p.add_argument('--contract',type=int,default=300);p.add_argument('--grace',type=int,default=300)
    a=p.parse_args();Observer(a.slot,a.run,contract=a.contract,grace=a.grace).run_loop()


if __name__ == '__main__':
    main()
