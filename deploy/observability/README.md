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

## 与平台的关系

平台通过 MCP `telemetry_ro` 读这套栈（Prometheus / Loki / Jaeger 三个查询接口），
端点由 Episode 的运行时环境变量给出，见 `environment/shared/observability.yaml`。
