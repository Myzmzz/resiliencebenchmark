# 第二套环境实测参照状态（2026-09-12）

准备方案（[stage2-env3-preparation-plan-20260912.md](stage2-env3-preparation-plan-20260912.md)）
第 2.3 节列了本次最大的风险：**可观测栈、Coroot、ChaosBlade、cgroup 包装四项，
仓库里没有可复现的安装资产，只有"它应该长这样"的描述**。

启动语说目标状态参照第二套环境的实测状态。第二套环境是可达的，所以这件事不用等——
本文是 2026-09-12 从 `62.234.93.223` **只读**导出的结果。

**只读**：全程 `kubectl get` / `helm list`，没有 apply、没有 patch、没有 delete，
没有读取任何 Secret 的内容。导出时该集群正在被使用（当天 19:02–19:22 有另一条分支
在建 5 个 `otel-demo-0x` release），所以格外只看不碰。

---

## 0. 最重要的一条：镜像几乎全部来自旧集群的 Harbor

| 组件 | 镜像来源 |
|---|---|
| 可观测栈（jaeger / kube-state-metrics / loki / otel-collector / promtail） | **`1.94.151.57:85/train-ticket/*`** |
| Coroot（coroot / cluster-agent / node-agent / clickhouse ×2 / prometheus / kube-state-metrics / operator） | **`1.94.151.57:85/observe/*`** |
| ChaosBlade operator | **`1.94.151.57:85/ischaos/chaosblade-operator:1.8.0`** |
| ChaosBlade tool | `ghcr.m.daocloud.io/chaosblade-io/chaosblade-tool:1.8.0` |
| Chaos Mesh（4 个镜像） | `ghcr.io/chaos-mesh/*:v2.8.0` |
| prometheus / node-exporter | `quay.io/prometheus/*`（按 digest，无 tag） |
| OTel Demo | 见 `environment/kubernetes/otel-demo/values.yaml` 的 `HARBOR_REGISTRY` 占位符 |

Coroot 的 CR 里还带 `pullSecrets: [{name: harbor-registry}]`。

**这对第三套环境意味着**：`1.94.151.57:85` 是**旧集群上的 HTTP 明文 Harbor**。
第三套环境（124.16.138.x，CSTNET 网段）大概率路由不到它。
所以 P5「构建并推镜像」的范围远不止平台自己那两个镜像——
**上面这十几个第三方镜像也得一并搬到公开镜像仓库**，否则装不起来。

这条在原方案里被低估了，现在要算进工作量。

---

## 1. cgroup 包装：解开了

操作手册只说「不要动它」，没说它是什么。实测导出：

```sh
#!/bin/sh
exec nsenter -t 1 -C -- /opt/chaosblade/blade.real "$@"
```

`chaosblade-tool` 在自己的 cgroup 命名空间里，`blade` 直接跑够不到被测 Pod 的 cgroup，
`pod-cpu` / `pod-mem` 会**报成功但什么都没压到**。包装让它先 `nsenter` 回 PID 1 的
cgroup 命名空间（`-C`）。依赖 `hostPID: true`。

完整的挂载方式、命令覆盖、以及 DaemonSet 的其余前提（privileged / hostPID /
hostNetwork / 十来个 hostPath）已经写成仓库资产：

- [deploy/chaosblade/cgroupns-wrapper.yaml](../../deploy/chaosblade/cgroupns-wrapper.yaml)
- [deploy/chaosblade/README.md](../../deploy/chaosblade/README.md)

**这项从"待你补"变成"已查实且已入库"。**

---

## 2. 各组件的实测版本与管理方式

| 组件 | 版本 | Helm 管吗 | 命名空间 |
|---|---|---|---|
| Chaos Mesh | chart **2.8.0** / app 2.8.0 | ✅ `helm -n chaos-mesh` | `chaos-mesh` |
| Coroot operator | chart **0.8.2** / app 1.8.2 | ✅ `helm -n coroot` | `coroot` |
| OTel Demo | chart **0.40.5** / app 2.2.0 | ✅ | `otel-demo`（另有 `-01`…`-05` 五个副本） |
| **可观测栈** | 见下 | ❌ **不是 Helm** | `observability` |
| **ChaosBlade** | operator 1.8.0 + tool 1.8.0 | ❌ **不是 Helm** | `default` |

两项"不是 Helm"的，只能靠导出对象清单来复现——**这是第三套环境还缺的东西**（见第 4 节）。

### 可观测栈的实际对象

| 类型 | 名称 | 镜像 |
|---|---|---|
| Deployment | `jaeger` | `train-ticket/jaeger-all-in-one:1.57` |
| Deployment | `kube-state-metrics` | `train-ticket/kube-state-metrics:v2.12.0` |
| Deployment | `loki` | `train-ticket/loki:2.9.8` |
| Deployment | `otel-collector` | `train-ticket/otel-collector-contrib:0.102.1` |
| Deployment | `prometheus` | `quay.io/prometheus/prometheus`（digest） |
| DaemonSet | `node-exporter` | `quay.io/prometheus/node-exporter`（digest） |
| DaemonSet | `promtail` | `train-ticket/promtail:2.9.8` |

配置走 ConfigMap。promtail 的抓取配置里写死了三个被测系统的日志路径
（`/var/log/pods/train-ticket_*`、`sock-shop_*`、`otel-demo_*`）——
第三套环境如果只跑 otel-demo，可以只留一条，但**得知道这里有写死的东西**。

仓库里 `environment/observability/` 那 7 个文件是**往已有安装上打的补丁**
（PVC、OTLP receiver、promtail readiness、collector routing 等），**不是从零安装的清单**。
这一点原方案的判断是对的，现在有实测佐证。

### Coroot 的匿名只读是怎么开的

Coroot CR 的 spec 里：

```yaml
authAnonymousRole: Viewer
authBootstrapAdminPasswordSecret:
  name: coroot-admin
  key: password
cacheTTL: 3d
```

`authAnonymousRole: Viewer` 就是平台那两个环境变量
（`RESBENCH_COROOT_ALLOW_ANONYMOUS_READ=true`）能生效的前提。
**第三套环境装 Coroot 时必须一并设这个**，否则平台读不到。

---

## 3. 集群底座（供第三套环境对照）

| 节点 | 角色 | 运行时 | OS / 内核 |
|---|---|---|---|
| `vm-0-13-ubuntu` | control-plane | **docker://29.6.2** | Ubuntu 26.04 LTS / 7.0.0-28-generic |
| `vm-0-10-ubuntu` | worker | **docker://29.6.1** | Ubuntu 26.04 LTS / 7.0.0-14-generic |

Kubernetes **v1.31.14**。**容器运行时确实是 Docker**，证实了准备方案 2.1 里
「agent-exec 的 cgroup 方案依赖 Docker 行为」那条前提——
**第三套环境如果是 containerd，这块要重新验证**。

存储类是 openebs（`openebs` 前缀的 4 个镜像在集群里）。

---

## 4. 还缺什么

查实之后，准备方案第 4 节那张"需要你给的东西"表可以更新：

| 原条目 | 现状 |
|---|---|
| ~~可观测栈 chart 版本与 values~~ | **部分解决**：知道了它不是 Helm、是哪 7 个对象、用什么镜像。**还需要导出对象清单与 ConfigMap 才能复现** |
| ~~Coroot chart 版本与 values~~ | **解决**：operator chart 0.8.2，匿名只读靠 CR 的 `authAnonymousRole: Viewer` |
| ~~ChaosBlade chart/清单来源~~ | **部分解决**：operator 1.8.0 + tool 1.8.0，不是 Helm。**还需要导出两个对象的清单** |
| ~~`chaosblade-cgroupns-wrapper` 内容~~ | **✅ 完全解决，已入库** |
| **新增** | **十几个第三方镜像要从旧 Harbor 搬到公开仓库**，第三套环境大概率路由不到 `1.94.151.57:85` |

**要不要我把那几份对象清单也导出来放进仓库**（可观测栈 7 个对象 + ChaosBlade 2 个对象
+ 相关 ConfigMap），你说一声。导出要做脱敏（去掉 `status`、`resourceVersion`、
节点名之类的集群特有字段），而且会是几百行 YAML，所以先问。

---

## 5. 与红线的关系

整改说明第 12 节的红线是「**只动新环境**……不碰老集群」，指的是**部署和测试**。
本文全部是 `kubectl get` / `helm list` 级别的只读查询，没有任何写操作，
也没有读取 Secret 内容。启动语明确把第二套环境的实测状态定为目标状态参照，
读它正是为了做这件事。
