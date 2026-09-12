# 第三套环境盘点结果（2026-09-12）

对象：`124.16.138.60/61/62`（`otcaix-60/61/62`）
方式：`tools/env3/inventory.sh` 在三台上各跑一次（**纯只读**，不装不改不启服务），
`tools/env3/gap_report.py` 出差距表。原始输出 12 章 + 35 条 `FACT`/台。

---

## 0. 先纠正两件事

### 0.1 环境**不是空的**

启动语说"这套环境基本是空的：没有 MCP 服务、没有可观测栈、没有 ChaosBlade"。
实测：**集群已经用了 183 天**，OTel Demo、Coroot、Chaos Mesh 都在跑，
而且还有**另一个项目**（`aiops` 命名空间，181 天）。

准确的说法是：**缺的是可观测栈、ChaosBlade 和平台本体**，其余都已就位。

### 0.2 连不上是本机 Clash TUN 劫持，不是对端防火墙

这台 Mac 跑着 Clash Verge 的 TUN 模式（`utun4` = `198.18.0.1`，路由把 `0.0.0.0/0`
拆成 `1`、`2/7`、`4/6` 压过默认路由），所有 TCP 被送到美国出口（`12.104.14.23`），
那里到 CSTNET 不通，于是连接在 SSH banner 之前就断。

**绕过办法：把源地址绑到物理网卡**（macOS 的 `IP_BOUND_IF`，不改任何系统设置）：

```bash
ssh -b 133.133.134.28 zhengmingzhuo@124.16.138.60     # 133.133.134.28 = en0 真实地址
```

绑上之后一切正常。本文所有数据都是这样采的。**后续操作都要带 `-b <en0 地址>`。**

---

## 1. 三台机器

| | `otcaix-60` | `otcaix-61` | `otcaix-62` |
|---|---|---|---|
| 角色 | **control-plane** | worker | worker |
| OS | Ubuntu 24.04.2 | Ubuntu 24.04.3 | Ubuntu 24.04.2 |
| 内核 | 6.8.0-100 | 6.8.0-94 | 6.8.0-100 |
| Docker | **29.3.1** | **29.0.0** | **28.3.2** |
| CPU | 64 | 64 | 64 |
| 可分配内存 | ~251 GiB | **~125 GiB** | ~251 GiB |
| cgroup | v2 | v2 | v2 |
| swap | **开着** | **开着** | **开着** |
| 时间同步 | 已同步 | **未同步** | 已同步 |
| `kubectl` 可用 | ✅ | ❌（无 kubeconfig） | ❌（无 kubeconfig） |

资源**远超需要**（第二套环境每台 32 核 / 123 GiB；这里 64 核 / 125–251 GiB）。

## 2. 集群

| 项 | 实测 | 第二套环境 |
|---|---|---|
| Kubernetes | **v1.29.15** | v1.31.14 |
| 容器运行时 | **Docker**（符合 agent-exec 的前提） | Docker |
| CNI | **Cilium 1.16.6**（helm，kube-system） | — |
| 存储类 | **`nfs-client`（default）**，nfs-subdir-external-provisioner，Retain | openebs / nfs-client |
| 集群年龄 | 183 天 | 51 天 |

出网全通：`registry.k8s.io`、`ghcr.io`、`quay.io` 都返回 401（=可达）。
**旧集群 Harbor `1.94.151.57:85` 也可达**（401）——这点很重要，见第 5 节。

## 3. 已有组件

| 组件 | 实测 | 与第二套环境比 |
|---|---|---|
| **OTel Demo** | helm `opentelemetry-demo-0.40.5` / app 2.2.0，**23 个 Deployment 全部就绪**，182 天，revision 4 | **chart 版本完全一致**，就绪数也一致 |
| **Coroot** | helm `coroot-operator-0.8.2`；coroot **v4.0.1** + clickhouse + 自带 prometheus v2.53.5，182 天 | operator 版本一致 |
| **Chaos Mesh** | helm chart **`chaos-mesh-0.0.0`**，镜像 `chaos-mesh:v20260330-fix`、`chaos-dashboard:v20260306-nomongo-v2`、`chaos-coredns:v0.2.6`，144 天 | ⚠️ **不是官方 2.7.3/2.8.0，是打过补丁的自定义构建** |
| **Cilium** | 1.16.6 | — |
| ⚠️ **`aiops`** | `isaiops-be/fe/gateway/problems` 四个 Deployment，181 天 | **别人的项目，不要碰** |

Chaos Mesh 和 Coroot 的镜像也都来自 `1.94.151.57:85`。

## 4. 缺的（这才是要装的）

| # | 缺什么 | 用哪份资产 |
|---|---|---|
| 1 | **可观测栈**：`observability` 命名空间整个不存在。Prometheus / Loki / Jaeger / otel-collector / kube-state-metrics / node-exporter / promtail **全无**（只有 Coroot 自带的 prometheus） | [deploy/observability/reference-stack.yaml](../../deploy/observability/reference-stack.yaml) |
| 2 | **ChaosBlade**：无 CRD、无 operator、无 tool DaemonSet、节点上无 `blade` | [deploy/chaosblade/reference-install.yaml](../../deploy/chaosblade/reference-install.yaml) |
| 3 | **ChaosBlade cgroup 包装** | [deploy/chaosblade/cgroupns-wrapper.yaml](../../deploy/chaosblade/cgroupns-wrapper.yaml) |
| 4 | **平台本体**：`resiliencebenchmark-system` 不存在 | `deploy/stage2/` + env3 专属 overlay |
| 5 | **AppArmor profile `resbench-agent-runtime`** —— 模块已加载（155 profiles / 60 enforce），但没有这一条 | [deploy/stage2/apparmor/](../../deploy/stage2/apparmor/) |

MCP 八个服务不用单独装（由 stage2 Pod 内的 `McpSupervisor` 现起现停）。

## 5. 差距与风险

### 阻塞级 1 项

**Kubernetes v1.29.15**，低于第二套环境的 v1.31.14。
平台资产在旧集群 v1.28 和新集群 v1.31 上都验过，1.29 居中，**大概率可用**，
但 `deploy/stage2/README.md` 里那些针对 1.28 的兼容处理需要复核一遍。
**要不要为此升级集群，是你的决定**——它是共享集群，`aiops` 也在上面。

### 需要你拍板的 3 项

1. **Chaos Mesh 是自定义构建**（chart `0.0.0`，镜像打过补丁：`nomongo`、`fix`）。
   仓库里 `deploy/chaos-mesh/values-old-cluster.yaml` 是官方 2.7.3 的配置，对不上。
   **而且它可能是 `aiops` 那个项目在用。** 三个选择：沿用现有的（要先验 D8 需要的
   `NetworkChaos`/`PodChaos`/`StressChaos` 三类能不能用）、并排装一套我们自己的、
   或者先问清楚它归谁。
2. **共享集群**：`aiops` 是别人的项目。`chaos-mesh`、`coroot`、`otel-demo` 是不是也归他们、
   我们能不能改，需要确认。**尤其 OTel Demo——如果它是别人在用的，我们注故障会影响他们。**
3. **swap 三台全开着**。kubelet 默认要求关闭，但集群已经跑了 183 天，
   说明要么配了 `failSwapOn: false`，要么另有安排。动它要重启 kubelet，**先问**。

### 提醒级

- **三台 Docker 版本不一致**（29.3.1 / 29.0.0 / 28.3.2）。agent-exec 依赖 Docker 的
  cgroup 命名空间行为，装 AppArmor profile 和跑平台的那台要单独验。
- **`otcaix-61` 时间未同步**。证据窗口与指标对齐依赖它。
- **61 / 62 上 `zhengmingzhuo` 没有 kubeconfig**，只有 60 能用 `kubectl`。
- **旧 Harbor 可达**是好消息：`1.94.151.57:85` 上那十几个第三方镜像**不必搬**，
  可以直接拉（需要 pull secret）。这把原方案里 P5 的工作量降回到只构建平台那两个镜像。

## 6. 盘点脚本的一处修正

首次跑完发现所有组件都报"已有"，包括实际不存在的 `observability`。
原因是存在性判断写成了 `kubectl ... | head -1 >/dev/null && echo present`，
取的是 `head` 的退出码，**永远成功**。已改为直接取 `kubectl` 自身的退出码并重跑。

本文数据来自修正后的第二次盘点。
