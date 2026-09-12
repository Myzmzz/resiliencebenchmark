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

## 4.5 确认前的只读深挖（2026-09-12 追加）

为了让第 5 节那几个拍板项好定，又做了一轮只读核查。

### 4.5.1 Chaos Mesh：D8 要的三类**全在**，但这是个大幅扩展的分支

| 项 | 实测 |
|---|---|
| `networkchaos` / `podchaos` / `stresschaos` | **三类 CRD 全部存在**（外加 `NetworkChaos` 内部要的 `podnetworkchaos`） |
| 当前活跃实验 | **三类都是 0**（没人正在用） |
| `chaos-daemon` | **3/3 就绪**，覆盖三台节点，144 天 |
| CRD 总数 | **43 个** —— 官方 2.7/2.8 没有的就有 `bladechaos`、`nginxchaos`、`systemdchaos`、`stracechaos`、`redischaos`，以及 **23 个 `jvm*chaos`**（clickhouse / druid / dubbo / elasticsearch / hbase / mongodb / redis / zookeeper …） |

**这不是官方版本打了个补丁，是一个专门扩展过的分支。**

### 4.5.2 ⚠️ `bladechaos` 不能替代 ChaosBlade，而且可能**冲突**

这个分支的 `bladechaos.chaos-mesh.org` CRD 字段是
`target` / `action` / `createFlags` / `prepareFlags` / `duration` / `selector`——
**就是 ChaosBlade 的命令模型包了一层**。

但平台要的是**另一个 CRD**：`chaosblades.chaosblade.io`
（[runtime_adapters.py:75](../../stage2_service/runtime_adapters.py#L75)、
`mcp_servers/chaos_core/backends/chaosblade.py`）。实测该 CRD **不存在**。

所以 **ChaosBlade 仍然必须装**，`bladechaos` 顶不了。

**至于两者会不会打架——查了，风险比一开始判断的小得多。**

`chaos-daemon` 容器里 `/usr/local/bin` 的内容是：

```
cdh  chaos-daemon  memStress  nsexec  pause  toda  tproxy
```

全是 Chaos Mesh 自己的工具（`toda` 做 IO chaos、`tproxy` 做 HTTP chaos、
`memStress` 做内存压力、`nsexec` 进命名空间）。**没有 `blade` 二进制，
也没有 `/opt/chaosblade*` 目录。** 宿主挂载只有三处：`/var/run`、`/sys`、`/lib/modules`——
**没挂 `/opt/chaosblade`**。

结论：这个分支的 `bladechaos` **不自带 blade**，多半是要求 ChaosBlade 单独安装
（chaosblade-operator 本来就是独立部署的）。**装官方 ChaosBlade 不会跟 chaos-daemon
里的二进制撞车。**

**唯一残留的重叠**：chaos-daemon 把整个 `/var/run` 挂进去了，而 ChaosBlade 的账本
就在 `/var/run/chaosblade.dat`。装完之后**验一次两边互不干扰**即可，不必事先阻塞。

（原来我把这条写成"装之前必须先问清楚"，那个判断过重了。）

### 4.5.3 业务流量**是有的**（先前的怀疑是误报）

`frontend` 近 11 小时没有日志，一度让我怀疑没有业务流量。**查实是误报**——
frontend 不记录每条请求。直接问 locust 自己：

```
state=running   用户=5   总 RPS=0.8
累计请求=125,540   失败=3,467（失败率 2.76%）
```

配置是 `LOCUST_USERS=5` / `LOCUST_SPAWN_RATE=1` / `LOCUST_AUTOSTART=true` /
`LOCUST_HOST=http://frontend-proxy:8080`。

两点要留意：

- **上次重启是 `OOMKilled`**（exitCode 137，2026-09-11T07:03），这是第 9 次重启。
  内存限额可能偏紧——第二套环境的操作手册特意说过 accounting / ad / fraud-detection
  三个的限额是环境所有者批准过的、不要擅自改，这里要不要调**得你定**。
- **基线失败率 2.76%**。做效果判定时这个底噪要算进去，不能把它当成故障引起的。

---

## 5. 差距与风险

### 阻塞级：**0 项**（k8s 版本那条是我门槛设错了）

一开始把 Kubernetes 门槛设成"≥ v1.30"（照抄第二套环境的 1.31.14），
于是 v1.29.15 被判成唯一的阻塞级差距。**核了平台清单，这个门槛是错的：**

- `deploy/stage2/stage2-integration.yaml:19` 和 `stage2.yaml:177` 用的是
  **AppArmor beta 注解** `container.apparmor.security.beta.kubernetes.io/agent-runtime`。
  那是 1.30 之前的写法，**也是 1.28/1.29 唯一接受的形式**；
  `securityContext.appArmorProfile` 字段要 1.30 才有。
- 清单里**没有任何 1.30+ 才有的特性**（无 sidecar `restartPolicy: Always`、
  无 `schedulingGates`、无 `matchLabelKeys`）。
- `apiVersion` 只有 `apps/v1`、`rbac.authorization.k8s.io/v1`、`v1`，全是老稳定版。
- `deploy/stage2/README.md` 本来就有一节 **"Kubernetes 1.28 compatibility"**。

门槛已改成**平台自己的下限 ≥ v1.28**（`tools/env3/gap_report.py`，附测试）。
**v1.29.15 合格，不需要升级集群。**

> 两条"未采集到"（CNI 配置、AppArmor profile）也不是真缺，只是那两个查询要 root：
> 已另行确认 **Cilium 1.16.6 在跑**、**AppArmor 模块已加载**（155 profiles / 60 enforce），
> 缺的只是 `resbench-agent-runtime` 这一条 profile（本来就在待装清单里）。

### 需要你拍板的 3 项

1. **Chaos Mesh 是大幅扩展的分支**（chart `0.0.0`，43 个 CRD，含 `bladechaos` 和
   23 个 `jvm*chaos`）。**D8 要的三类 CRD 全在、当前零活跃实验、daemon 3/3 就绪**（见 4.5.1），
   所以"沿用现有的"技术上可行。但仓库里 `deploy/chaos-mesh/values-old-cluster.yaml`
   是官方 2.7.3 的配置，对不上；而且**它很可能是 `aiops` 在用**。
   **4.5.2 那条冲突风险已经查清并降级**：chaos-daemon 里没有 blade 二进制、
   也没挂 `/opt/chaosblade`，装官方 ChaosBlade 不会撞车，装完验一次即可。
   所以这一项现在只剩归属问题，不再是技术阻塞。
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
