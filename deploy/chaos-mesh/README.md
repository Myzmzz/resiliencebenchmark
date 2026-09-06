# Chaos Mesh：旧集群环境准备

目标仅为 `/Users/mymz/.kube/coroot-config`、context
`kubernetes-admin@kubernetes`。不在新集群安装或测试。

固定官方 Chart `2.7.3`。官方支持表将 2.7 对应到 Kubernetes 1.28，
2.8 对应到 1.30 及以上。旧节点实际运行 Docker（26.1.3/28.1.1），
所以配置 `/var/run/docker.sock`；不能根据同时存在的 containerd socket
推断 Kubernetes 使用 containerd。

`values-old-cluster.yaml` 限定 Controller 的目标命名空间为 `otel-demo`，
控制面放在 `tcse-v100-03`，daemon 只放在三个既有测试节点。
不安装 Dashboard、DNS Server、BPF 故障组件或额外 Prometheus，
保留 daemon mTLS，关闭 profiling/chaosctl 服务与 hostNetwork 故障。

首次直接引用 GHCR 的实际安装等待超时（1/3 daemon 就绪，Controller 未启动），
资源保留，未重建业务负载。已将官方 `linux/amd64` 2.7.3 两个镜像原样发布到
现有 Harbor `observe` 项目；values 固定使用这些镜像。不是更换引擎版本或
改用自定义旧镜像。后续更新只针对本次 `chaos-mesh` release。

## 三类评测入口与上游内部组件的区别

Benchmark 只注册并授权 `NetworkChaos`、`PodChaos`、`StressChaos`；
执行/清理身份由 `deploy/stage2/execution-identities.yaml` 分开授予。
被测 Agent 不获得 Kubernetes 凭据或任何直接 CRD 权限。

官方 Chart 会安装其完整 CRD 定义，并包含 NetworkChaos 所需的
PodNetworkChaos 等内部结构；不能把“三类评测入口”描述为“集群里仅有
三个 CRD”。保留上游内置控制器注册，但不因此向 Benchmark 授予其他类型。
`2.7.3` 的 `controllers/common/fx.go` 使用 `<kind>-records` 作为启动名，
遇到未启用的实现时会从整个 bootstrap 循环返回，因此不能通过填三个工具名
来可靠裁剪控制器。这里不修改第三方引擎源码；权限边界由命名空间、MCP
允许操作和执行身份 RBAC 实现。Webhook 配置仅注册三种公开评测资源。

## 安装与验证

命名空间模式下，2.7.3仍会注册全局RemoteCluster缓存；官方Chart未给它
集群级list/watch，实测导致Controller约两分钟后退出。额外清单
`controller-bootstrap-rbac.yaml`仅补全局RemoteCluster只读权限及
`chaos-mesh`命名空间内的events create/patch，不增加其他命名空间的故障写权限。
应在首次启动前应用；已安装环境只需补该清单，缓存会自行恢复，不必重启节点。

安装属于已批准的环境准备步骤，可能因镜像下载超过五分钟。先确认没有
已有同名 release、活跃试验或故障残留，再执行真正的安装：

```bash
kubectl --kubeconfig /Users/mymz/.kube/coroot-config \
  --context kubernetes-admin@kubernetes apply \
  -f deploy/chaos-mesh/namespace.yaml \
  -f deploy/chaos-mesh/controller-bootstrap-rbac.yaml
helm install chaos-mesh chaos-mesh \
  --repo https://charts.chaos-mesh.org --version 2.7.3 \
  --kubeconfig /Users/mymz/.kube/coroot-config \
  --kube-context kubernetes-admin@kubernetes \
  --namespace chaos-mesh --create-namespace \
  --values deploy/chaos-mesh/values-old-cluster.yaml \
  --wait --timeout 10m
```

安装成功后分别记录 release、运行镜像、Controller/daemon 的实际状态、
权限检查和故障清单。Ready 只代表组件就绪，不代表注入能力或恢复验证通过。
金丝雀故障须单独串行执行，验证实际效果、对象清理与业务恢复；安装不创建
任何实验对象，不修改 OTel Demo 的 values、资源额度或业务配置。

参考：[官方支持版本](https://chaos-mesh.org/supported-releases/)、
[固定版本 Chart](https://github.com/chaos-mesh/chaos-mesh/tree/v2.7.3/helm/chaos-mesh)、
[控制器注册实现](https://github.com/chaos-mesh/chaos-mesh/blob/v2.7.3/controllers/common/fx.go)。
