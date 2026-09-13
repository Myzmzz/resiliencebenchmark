# 可观测栈：参照安装

仓库里 `environment/observability/` 那 7 个文件是**往一套已有安装上打的补丁**
（PVC、OTLP receiver、promtail readiness、collector routing 等），不是从零安装的清单。
这套栈在第二套环境里**也不是 Helm 管的**——是裸的 Deployment / DaemonSet / Service /
ConfigMap 对象。所以在一套新环境上，原本没有任何东西可以照着装。

`reference-stack.yaml` 是 2026-09-12 从第二套环境**只读**导出的 21 个对象，
去掉了 `status`、`resourceVersion`、`uid`、`managedFields`、
`last-applied-configuration`、Service 的 `clusterIP` 等集群特有字段。

## 内容

| 类型 | 名称 | 镜像 |
|---|---|---|
| Deployment | `jaeger` | `train-ticket/jaeger-all-in-one:1.57` |
| Deployment | `kube-state-metrics` | `train-ticket/kube-state-metrics:v2.12.0` |
| Deployment | `loki` | `train-ticket/loki:2.9.8` |
| Deployment | `otel-collector` | `train-ticket/otel-collector-contrib:0.102.1` |
| Deployment | `prometheus` | `quay.io/prometheus/prometheus`（按 digest） |
| DaemonSet | `node-exporter` | `quay.io/prometheus/node-exporter`（按 digest） |
| DaemonSet | `promtail` | `train-ticket/promtail:2.9.8` |

外加 6 个 Service、4 个 ConfigMap（loki / otel-collector / prometheus / promtail 的配置）、
1 个 PVC（`prometheus-tsdb`）、3 个 ServiceAccount。

## ⚠️ 不能直接 apply

新环境至少要改三处：

1. **`nodeSelector: {kubernetes.io/hostname: vm-0-10-ubuntu}`** —— 五个 Deployment 上都有，
   换成本环境的节点名
2. **镜像 `1.94.151.57:85/train-ticket/*`** —— 那是**旧集群上的 HTTP 明文 Harbor**。
   一套路由不到它的环境必须先把这些镜像搬到自己的仓库
3. **`PersistentVolumeClaim/prometheus-tsdb` 的 `storageClassName: nfs-client`** ——
   换成本环境的存储类

另外 `promtail-config` 里写死了三个被测系统的日志路径
（`/var/log/pods/train-ticket_*`、`sock-shop_*`、`otel-demo_*`），
只跑 otel-demo 的话可以只留一条。

装完之后，`environment/observability/` 里那些补丁才是"往上打"的对象。

## 第三套环境的实装记录（2026-09-12）

按上面三条都改了：

1. **去掉了 `nodeSelector`** —— 不是换节点名而是直接删。可观测栈不需要 AppArmor
   （那是 `agent-runtime` 才要的），交给调度器选更稳。**没改这一条会让五个 Deployment 全部 Pending**，
   我第一次 apply 就踩了。
2. 镜像沿用 `1.94.151.57:85/train-ticket/*` —— 该集群节点的 `/etc/docker/daemon.json`
   里本来就有 `insecure-registries: ["1.94.151.57:85"]`，直接能拉。
3. `prometheus-tsdb` 的 `storageClassName` 保持 `nfs-client` —— 正好是该集群唯一的默认存储类。

另外加了 [env3-resource-limits.yaml](env3-resource-limits.yaml)：六个组件原本全无
requests/limits，jaeger 在第二套环境实测涨到 18.4 GiB。这是共享集群，不能这么放。
jaeger 除内存 limit 外还给了 `MEMORY_MAX_TRACES=100000`——**只加 limit 会变成周期性
OOMKill，治标不治本**，根因是 `SPAN_STORAGE_TYPE=memory` 不限条数。

### 让 otel-demo 的信号流进来

装好栈只是第一步，otel-demo 默认只往 Coroot 发。按
`environment/observability/otel-demo-collector-routing.yaml` 改了 otel-demo 的
`otel-collector` ConfigMap（**改前已备份到节点上 `~/resbench-backups/`**）：

- `otlp/jaeger` 端点 `jaeger:4317`（该服务不存在）→ `jaeger-query.observability.svc.cluster.local:4317`
- `otlphttp/prometheus` 端点 `prometheus:9090`（同样不存在）→ `prometheus.observability.svc.cluster.local:9090/api/v1/otlp`
- 这两个导出原本**定义了却没接进任何 pipeline**，现在分别接进 traces 和 metrics
- **`otlphttp/coroot` 原样保留** —— `aiops` 命名空间的 isaiops 靠它取数，不能断

验收：Jaeger 收到 18 个服务（cart / frontend / checkout / payment …）；
Prometheus 有 `k8s_pod_phase{job="otel-demo/cart"}`；Loki 有 `{namespace="otel-demo"}` 的流。
collector 日志里这两个新导出零报错（另有两条 `otlphttp/coroot` 400 与 kubeletstats
证书缺 IP SAN 的报错，**都是改动前就存在的**，见 `environment/observability/kubelet-certificate-expiry.yaml`）。

## 与平台的关系

平台通过 MCP `telemetry_ro` 读这套栈（Prometheus / Loki / Jaeger 三个查询接口），
端点由 Episode 的运行时环境变量给出，见 `environment/shared/observability.yaml`。
