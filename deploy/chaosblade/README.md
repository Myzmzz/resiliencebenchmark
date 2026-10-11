# ChaosBlade：CPU/内存注入所依赖的 cgroup 命名空间包装

第二套环境的操作手册只写了一句「CPU、内存故障依赖 ConfigMap
`chaosblade-cgroupns-wrapper` 的包装，**不要动它**」，没说它是什么，仓库里也没有。
2026-09-12 从第二套环境实测导出，记录于此，供第三套环境复现。

## 它解决什么

`chaosblade-tool` 跑在自己的容器里，有自己的 cgroup 命名空间。`blade` 直接执行时
看到的是容器自己的 cgroup，**够不到被测 Pod 的 cgroup**——`pod-cpu` / `pod-mem`
这类实验会报成功却什么都没压到。

包装脚本就两行，让 `blade` 先回到 PID 1 的 cgroup 命名空间：

```sh
#!/bin/sh
exec nsenter -t 1 -C -- /opt/chaosblade/blade.real "$@"
```

## 怎么生效

`cgroupns-wrapper.yaml` 提供 ConfigMap（`defaultMode: 0755`）。
`chaosblade-tool` DaemonSet 需要两处改动：

1. 挂载（`subPath` 只取 `blade` 这一个键）：

   ```yaml
   volumeMounts:
     - name: cgroupns-wrapper
       mountPath: /etc/chaosblade-wrapper/blade
       subPath: blade
   volumes:
     - name: cgroupns-wrapper
       configMap:
         name: chaosblade-cgroupns-wrapper
         defaultMode: 493        # 0755
   ```

2. 覆盖容器命令，把原始 `blade` 挪成 `blade.real` 再用包装顶替它：

   ```yaml
   command: ["sh", "-c"]
   args:
     - |
       set -eu
       cp /opt/chaosblade/blade /opt/chaosblade/blade.real
       cp /etc/chaosblade-wrapper/blade /opt/chaosblade/blade
       chmod 0755 /opt/chaosblade/blade /opt/chaosblade/blade.real
       exec tail -f /dev/null
   ```

`tail -f /dev/null` 是原样保留的：这个容器本来就不跑主进程，实际注入由
operator 通过 `kubectl exec` 进来执行 `blade`。

## `reference-install.yaml` 里有什么

七个对象，**顺序有讲究**——前五个是先决条件，operator 一起来就要用：

| # | 对象 | 作用 |
|---|---|---|
| 1 | CRD `chaosblades.chaosblade.io` | **平台就是靠这个 CRD 下发和查询故障**（`stage2_service/runtime_adapters.py:75`、`mcp_servers/chaos_core/backends/chaosblade.py`） |
| 2 | ServiceAccount `chaosblade` | operator 的身份 |
| 3 | ClusterRole `chaosblade` | |
| 4 | ClusterRoleBinding `chaosblade` | |
| 5 | Service `chaosblade-webhook-server` | operator 的准入 webhook |
| 6 | Deployment `chaosblade-operator` | |
| 7 | DaemonSet `chaosblade-tool` | 要配 `cgroupns-wrapper.yaml` 一起用 |

> 首次导出时只收了 6 和 7，漏了前五个先决对象——是拿真集群做
> `kubectl apply --dry-run=server` 时发现的。补齐后七个对象在 k8s 1.29 上全部通过。

平台侧另有 `resbench-stage2-executor-chaosblade` / `-finalizer-chaosblade` 两个
ClusterRole，那是 `deploy/stage2/execution-identities.yaml` 的内容，不在本文件里。

## 第二套环境的其余前提（一并记录）

`chaosblade-tool` DaemonSet 是 `privileged: true`、`hostPID: true`、`hostNetwork: true`，
并挂了宿主的 `/var/run/docker.sock`、`/var/lib/docker`、`/etc/docker`、
`/run/containerd`、`/var/lib/containerd`、`/etc/containerd`、`/var/run/netns`、
`/sys`（挂到 `/host-sys`）、`/etc/hosts`、`/var/log/audit`，
以及 `/var/run/chaosblade.dat`（`FileOrCreate`）当实验账本。

`nsenter -t 1 -C` 依赖 `hostPID: true`——**少了它包装不成立**。

## 版本

| 组件 | 第二套环境实测 |
|---|---|
| chaosblade-operator | `1.8.0`（Deployment，1 副本，namespace `default`） |
| chaosblade-tool | `1.8.0`（DaemonSet，覆盖全部节点） |

两者都**不是 Helm 管的**，是直接的 Deployment/DaemonSet 对象。


## 第三套环境实装记录（2026-09-12/13）

七个对象 + cgroup 包装装完，**但清单里还差两样**，是装的时候撞出来的：

1. **`nodeSelector` 是第二套环境的节点名** —— `chaosblade-operator` 因此 Pending。
   operator 是控制器，不挑节点，直接去掉该字段。（tool 是 DaemonSet，不受影响。）
2. **缺 Secret `chaosblade-webhook-server-cert`**（`kubernetes.io/tls`，含 `ca.crt`/`tls.crt`/`tls.key`）——
   operator 挂载它，没有就一直 `FailedMount`。导出时我有意跳过所有 Secret（不搬密钥），
   **这一条是必需的**。做法是在本环境自签一张，不从别的集群搬私钥：

   ```bash
   openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -keyout ca.key -out ca.crt \
       -subj "/CN=chaosblade-webhook-ca"
   openssl req -newkey rsa:2048 -nodes -keyout tls.key -out tls.csr \
       -subj "/CN=chaosblade-webhook-server.default.svc"
   # SAN 要覆盖四种写法：<svc> / <svc>.<ns> / <svc>.<ns>.svc / <svc>.<ns>.svc.cluster.local
   openssl x509 -req -in tls.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
       -out tls.crt -days 3650 -extfile san.cnf
   kubectl -n default create secret tls chaosblade-webhook-server-cert \
       --cert=tls.crt --key=tls.key      # 再把 ca.crt 补进 data
   ```

**没装的一样**：第二套环境还有个 `MutatingWebhookConfiguration/chaosblade-operator`，
**集群级、拦截所有 Pod 的 CREATE/UPDATE**（`failurePolicy: Ignore`、`sideEffects: None`）。
第三套环境是共享集群，这种全局钩子先不装——CPU/内存/网络注入不需要它（见下方实测）。
真需要时再补，配置在 `environment/` 之外单独记。

### 装机验收：两层都验，账本不算数

按 F4/F5/F9 的教训，只看 ChaosBlade CR 的状态是不够的。对 `otel-demo/cart` 注一次
单核 CPU 满载，三层实测：

| | 注入中 | `blade destroy` 后 |
|---|---|---|
| 集群 CR | 存在 | 无 |
| 容器内 `chaos_os` 进程 | **在** | **没了** |
| cgroup CPU（5 秒采样） | **4956 ms ≈ 99% 单核** | **27 ms（基线）** |

**两个必须记住的复现：**

- **删掉 ChaosBlade CR 不会停掉原生注入**（F5）。CR 删干净、`kubectl get chaosblade -A`
  返回 `No resources found` 之后，`chaos_os` 仍在跑、CPU 仍是 99%。必须进 tool 容器
  `blade destroy <native-uid>` 才真正停。
- **`timeout` 也没兜住**：`--timeout=180` 早已过期，进程还在。
- **账本会骗人**（F9）：`blade status --type create` 里那条的 `Status` 一直是 `Success`，
  既不代表故障还在，也不代表已清除。**残留判定必须实测**（进程 + cgroup 用量）。

节点上 `/etc/docker/daemon.json` 本来就有 `insecure-registries: ["1.94.151.57:85"]`，
旧 Harbor 的镜像直接能拉。
