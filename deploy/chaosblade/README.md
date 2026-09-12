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
