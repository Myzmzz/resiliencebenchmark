"""代理安全边界；拒绝无法解析或不能证明属于本轮目标的写入。"""
from __future__ import annotations

import re
from urllib.parse import urlsplit


class Denied(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise Denied(reason)


def seconds(value) -> int:
    text = str(value)
    require(bool(re.fullmatch(r'[1-9][0-9]*(s|m)?', text)), '时长必须是正整数秒或分钟')
    return int(text[:-1]) * (60 if text.endswith('m') else 1) if text[-1] in 'sm' else int(text)


def labels_match(selectors, labels):
    require(isinstance(selectors, dict) and bool(selectors), '需要非空的目标标签')
    require(all(isinstance(k, str) and isinstance(v, str) and labels.get(k) == v for k, v in selectors.items()), '标签不匹配本轮 cart')


def service_proxy_target(full_path):
    path = urlsplit(full_path).path
    match = re.match(r'^/api/v1/namespaces/([^/]+)/services/([^/]+)/proxy(?:/|$)', path)
    require(match is not None, '只允许精确的已声明观测Service proxy路径')
    namespace, identifier = match.groups()
    parts = identifier.split(':')
    if len(parts) == 3:
        require(parts[0] == 'http', '已声明观测服务使用HTTP')
        name, port = parts[1:]
    elif len(parts) == 2:
        name, port = parts
    else:
        require(len(parts) == 1, 'Service proxy标识不合法')
        name, port = parts[0], ''
    targets = {('observability', 'prometheus'): 9090, ('coroot', 'coroot-coroot'): 8080, ('coroot', 'coroot-prometheus'): 9090}
    require((namespace, name) in targets, '仅允许已声明的观测服务proxy')
    require(not any(x in path for x in ['/-/reload', '/-/quit', '/config', '/admin', '/delete', '/settings']), '禁止服务管理接口')
    return namespace, name, port, targets[(namespace, name)]


def validate_cb(obj, bound, *, maximum=600):
    require(isinstance(obj, dict) and obj.get('kind') == 'ChaosBlade', '只允许 ChaosBlade')
    require(obj.get('apiVersion') == 'chaosblade.io/v1alpha1', 'ChaosBlade 版本不正确')
    meta = obj.get('metadata', {})
    require(bool(meta.get('name')) and not meta.get('generateName'), '必须显式命名，以便对账')
    require(not meta.get('ownerReferences') and not meta.get('namespace'), '不允许外部 owner 或命名空间伪装')
    require(not meta.get('finalizers'), '不允许自定义 finalizer 阻止清理')
    require(set(obj.get('spec', {})) == {'experiments'}, '只允许 experiments 字段')
    experiments = obj['spec']['experiments']
    require(isinstance(experiments, list) and len(experiments) == 1, '一次只允许一个故障')
    exp = experiments[0]
    require(set(exp) <= {'scope', 'target', 'action', 'matchers', 'desc'}, '未知故障字段')
    require(exp.get('scope') in {'pod', 'container'}, '禁止节点级故障')
    require(exp.get('target') == 'cpu' and exp.get('action') in {'fullload', 'load'}, '仅批准 CPU 故障')
    mm = {}
    empty_optional={'labels','names','uid','chaosblade-deploy-mode','chaosblade-download-url','chaosblade-path','cpu-count','cpu-index','cpu-list','endpoint','evict-count','evict-percent','evict-group','climb-time','container-names','container-ids','container-index'}
    seen=set()
    for entry in exp.get('matchers', []):
        require(isinstance(entry, dict) and set(entry) == {'name', 'value'}, 'matcher 格式错误')
        name, value = entry['name'], entry['value']
        require(name not in seen, '不允许重复 matcher')
        seen.add(name)
        require(isinstance(value, list) and len(value) > 0 and all(isinstance(v, str) for v in value), 'matcher 必须有非空字符串数组')
        if value==[''] and name in empty_optional:
            continue
        mm[name] = value
    require(set(mm) <= {'namespace', 'names', 'labels', 'container-names', 'container-ids', 'cpu-percent', 'cpu-count', 'timeout', 'climb-time', 'evict-count', 'evict-percent','cgroup-root'}, '出现未批准的 matcher')
    require(mm.get('cgroup-root',['/host-sys/fs/cgroup']) in [['/host-sys/fs/cgroup'],['/sys/fs/cgroup']], 'cgroup 路径不在已核对的工具挂载内')
    require(mm.get('namespace') == [bound['namespace']], '不得跨本实例命名空间')
    require('names' in mm or 'labels' in mm or exp.get('scope') == 'container' and 'container-ids' in mm, '必须选择已绑定 cart')
    if 'container-ids' in mm:
        require(exp.get('scope') == 'container' and bool(bound.get('container_id')), '容器级选择必须有已核实的运行时身份')
        require(mm['container-ids'] == [bound['container_id']], '容器ID不匹配当前绑定的cart')
    if 'names' in mm:
        require(mm['names'] == [bound['name']], 'Pod 名不匹配当前绑定')
    if 'labels' in mm:
        pairs = {}
        for value in mm['labels']:
            for part in value.split(','):
                require(part.count('=') == 1, '只允许精确标签匹配')
                k, v = part.split('=')
                require(k not in pairs, '重复标签')
                pairs[k] = v
        labels_match(pairs, bound['labels'])
        require(all(bound.get('unique_labels',{}).get(k)==v for k,v in pairs.items()), '每项标签必须独立唯一指向当前 cart，避免 OR 语义扩大目标')
    require(mm.get('container-names', ['cart']) == ['cart'], '只允许 cart 容器')
    require(len(mm.get('cpu-percent', [])) == 1, '缺少明确 CPU 百分比')
    try:
        cpu = int(mm['cpu-percent'][0])
    except ValueError as exc:
        raise Denied('CPU 百分比必须是整数') from exc
    require(1 <= cpu <= 80, 'CPU 百分比超出 1..80')
    require(mm.get('cpu-count', ['1']) == ['1'], '至多一个 CPU worker')
    require(mm.get('evict-count', ['1']) == ['1'], '受影响目标数必须为一个已绑定cart')
    require(len(mm.get('timeout', [])) == 1, '必须预设安全 timeout')
    duration = seconds(mm['timeout'][0])
    require(duration <= maximum, 'timeout 超出本轮安全上限')
    require(mm.get('climb-time', ['0']) == ['0'], '本轮不允许额外爬升窗口')
    require('evict-percent' not in mm, '不允许随机目标比例')
    return {'namespace': bound['namespace'], 'name': meta['name'], 'cpu_percent': cpu, 'timeout_seconds': duration, 'target_uid': bound['uid']}


def validate_mesh(obj, bound, *, maximum=300):
    require(isinstance(obj, dict) and obj.get('kind') == 'StressChaos', 'Chaos Mesh 仅允许 StressChaos')
    require(obj.get('apiVersion') == 'chaos-mesh.org/v1alpha1', 'Chaos Mesh 版本不正确')
    meta, spec = obj.get('metadata', {}), obj.get('spec', {})
    require(meta.get('namespace') == bound['namespace'], '不得跨本实例命名空间')
    require(bool(meta.get('name')) and not meta.get('generateName'), '必须明确命名')
    require(not meta.get('ownerReferences') and not meta.get('finalizers'), '禁止额外 owner/finalizer')
    require(set(spec) <= {'mode', 'selector', 'duration', 'stressors', 'containerNames'}, '禁止扩展 stress-ng、remoteCluster 或额外字段')
    require(spec.get('mode') in {'one', 'all'}, '仅支持唯一已绑定目标')
    selector = spec.get('selector', {})
    require(set(selector) <= {'namespaces', 'pods', 'labelSelectors'}, '禁止额外选择器')
    require(selector.get('namespaces') == [bound['namespace']], '必须显式限定命名空间')
    require('pods' in selector or 'labelSelectors' in selector, '必须选择具体 cart')
    if 'pods' in selector:
        require(selector['pods'] == {bound['namespace']: [bound['name']]}, 'Pod 选择器不匹配本轮 cart')
    if 'labelSelectors' in selector:
        labels_match(selector['labelSelectors'], bound['labels'])
        require(any(bound.get('unique_labels',{}).get(k)==v for k,v in selector['labelSelectors'].items()), '标签必须唯一绑定当前 cart')
    require(spec.get('containerNames', ['cart']) == ['cart'], '只允许 cart 容器')
    require(set(spec.get('stressors', {})) == {'cpu'}, '仅允许 CPU stressor')
    cpu = spec['stressors']['cpu']
    require(set(cpu) == {'workers', 'load'}, '只允许 workers/load')
    require(type(cpu['workers']) is int and cpu['workers'] == 1, '必须一个 CPU worker')
    require(type(cpu['load']) is int and 1 <= cpu['load'] <= 80, 'CPU 负载超出 1..80')
    duration = seconds(spec.get('duration', ''))
    require(duration <= maximum, '故障超过 300 秒')
    return {'namespace': bound['namespace'], 'name': meta['name'], 'cpu_percent': cpu['load'], 'timeout_seconds': duration, 'target_uid': bound['uid']}


def permitted_exec(command, owned_ids):
    require(isinstance(command, list) and all(isinstance(c, str) for c in command), 'exec command 格式错误')
    require(len(command) >= 2, '缺少明确命令')
    require(command[0] in {'blade', '/opt/chaosblade/blade', '/etc/chaosblade-wrapper/blade'}, '禁止 shell 和非 blade 程序')
    verb = command[1]
    if verb in {'version', 'help'}:
        require(len(command) == 2, '版本或帮助命令不得带额外参数')
        return 'read'
    require(verb in {'status', 'destroy'} and len(command) == 3, '只允许本轮实验的状态查询或销毁')
    require(command[2] in owned_ids, '实验 ID 不属于本轮')
    return 'destroy' if verb == 'destroy' else 'read'
