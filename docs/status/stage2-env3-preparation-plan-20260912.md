# 第三套环境准备方案（2026-09-12）

目标机器：`124.16.138.60` / `.61` / `.62`
分支：`codex/stage2-env3-bladeai-20260912`
状态：**盘点已完成（2026-09-12）**，本方案已按实测结果重写

---

## 0. 状态

**盘点已完成**（2026-09-12），结果见
[stage2-env3-inventory-20260912.md](stage2-env3-inventory-20260912.md)。

原先连不上是**本机 Clash Verge 的 TUN 模式**把所有 TCP 劫持到美国出口所致，
不是对端防火墙。绕过办法是把源地址绑到物理网卡，不必改任何系统设置：

```bash
ssh -b 133.133.134.28 zhengmingzhuo@124.16.138.60     # 133.133.134.28 = en0 真实地址
```

**后续所有对这三台的操作都要带 `-b <en0 地址>`。**

盘点与差距分析：

```bash
bash tools/env3/inventory.sh > inventory-node60.txt      # 三台各跑一次
python tools/env3/gap_report.py inventory-node*.txt      # 自动出差距表
```

- [tools/env3/inventory.sh](../../tools/env3/inventory.sh)：12 章人类可读报告 + 一段
  `FACT key=value` 机器可读事实。纯只读，拿不到的标 `unknown`，**不猜**。
- [tools/env3/gap_report.py](../../tools/env3/gap_report.py)：逐条比对目标状态，
  产出差距表与待装清单。每个数值注明出处，**"未采集到"永远不算通过**。

仍然缺的一项：**公开镜像仓库地址**（启动语里还是 `<仓库地址待填>`）。
不过范围比原计划小得多——旧 Harbor `1.94.151.57:85` 从第三套环境**可达**，
那十几个第三方镜像不必搬，只需构建平台自己的两个镜像。

## 1. 三处需要先纠正的认知

动手前先把这三条对齐，否则方案会写歪。

### 1.1 八个 MCP 服务**不是**八个要单独部署的组件

启动语把 `k8s_ro / telemetry_ro / source_ro / coroot_ro / chaos_control / chaos_mesh_control /
code_sandbox / harness_channel` 列成了待装组件。实际上在 Stage-2 流程里，
这八个由控制器 Pod 内的 `McpSupervisor` **每次试验现起现停**，
监听 Pod 内回环 18081–18088（BladeAI 走 SSE 时是 18181–18188）：

[stage2_service/mcp_supervisor.py:37-56](../../stage2_service/mcp_supervisor.py#L37)

其中 `chaos_mesh_control` 和 `code_sandbox` 是 D7/D8 替代工具，**要么都给要么都不给**
（[mcp_supervisor.py:86-94](../../stage2_service/mcp_supervisor.py#L86)）。

**所以：装好 stage2 Pod，八个 MCP 就都有了，不需要单独部署。**

`environment/mcp/host/`（systemd 那套）是另一条路线——BenchmarkFactory 时代的**主机部署**，
只有四个服务（k8s_ro、telemetry_ro、source_ro、chaos_control）+ 三个 SSE 兼容单元，
由 `scripts/deploy_mcp_host.py` 装。**Stage-2 评测不走这条路**。

### 1.2 `qualify_mcp_endpoints.py` 查的是上面那条主机路线

它只校验**四个**端点（[scripts/qualify_mcp_endpoints.py:38-83](../../scripts/qualify_mcp_endpoints.py#L38)），
读的是 `RESBENCH_K8S_MCP_URL` 等环境变量。`qualify_remote_preparation.py` 同理，
是走 SSH 校验 BenchmarkFactory 主机与 worker 的。

启动语把它列为环境验收项。**如果第三套环境只跑 Stage-2 评测，这两个脚本验的不是要用的那条链路。**
真正的验收应该是后两条：`/api/v1/stage2/options` 返回三家可跑 + 完整跑通一次 C0。

**需要你确认**：第三套环境是只跑 Stage-2，还是也要那套主机 MCP？两者工作量差别不小。

### 1.3 仓库清单里有两处是**旧集群的值**，不能照搬

| 位置 | 仓库里的值 | 说明 |
|---|---|---|
| [stage2-integration.yaml:147](../../deploy/stage2/stage2-integration.yaml#L147) | `RESBENCH_COROOT_PROJECT_ID=9auios5b` | 旧集群的 Coroot 项目。新集群是 `p1nar0hw`，第三套环境会是第三个值 |
| 全仓库 | **没有** `fsGroupChangePolicy` | 新环境操作手册第 4 节点名了这一项必须是 `OnRootMismatch`，但仓库任何清单里都没有 |
| [stage2.yaml:153](../../deploy/stage2/stage2.yaml#L153) | `storageClassName: standard` | 新集群实际用 openebs 本地卷。第三套环境得按实际存储类改 |
| [stage2-integration.yaml](../../deploy/stage2/stage2-integration.yaml) | **没有** `nodeSelector` | 新集群靠 nodeSelector 把负载钉在装了 AppArmor 的那台 |

这正是操作手册那句"**永远不要 `kubectl apply` 仓库渲染出来的清单**"的由来。

**已建好**：[deploy/stage2/env3/](../../deploy/stage2/env3/)——`values.yaml` 存这套环境的
差异值（全部是 2026-09-12 实测出来的），`render.py` 把它套到基线清单上。
不改共用清单，不碰镜像占位符（镜像仍走 `deploy_boundary.sh`）。

```bash
python deploy/stage2/env3/render.py --out /tmp/env3-stage2.yaml
python scripts/verify_stage2_deployment.py --manifest /tmp/env3-stage2.yaml \
    --coroot-project po24tcoz --require-node-selector
```

**已离线自检通过**：基线清单单独跑会爆三条（`fsGroupChangePolicy` / 项目 id / `nodeSelector`），
渲染之后全绿。10 条测试守着这个闭环，其中一条就是"基线必须爆这三条"。

---

## 2. 目标状态与装法

现状一栏等盘点。资源按仓库清单里的实际声明值填，不是估的。

### 2.1 底座

| # | 组件 | 目标 | 仓库资产 | 装哪台 | 资源 | 回滚 |
|---|---|---|---|---|---|---|
| 1 | Kubernetes | 与新环境同档（v1.31.x），1 控制面 + 2 工作节点 | **无**（仓库不装 k8s） | .60 控制面、.61/.62 工作节点（**待盘点确认角色**） | — | `kubeadm reset`（仅当是我们新装的） |
| 2 | 容器运行时 | **Docker** | 无 | 全部 | — | — |
| 3 | 存储类 | 一个默认 StorageClass，支持 `fsGroup`、ReadWriteOnce | 无 | 全部 | PVC 要 20Gi | 删 SC（无 PVC 绑定时） |
| 4 | CNI | 任一可用 | 无 | 全部 | — | — |
| 5 | cgroup v2 + AppArmor | 内核 5.12+，`/sys/fs/cgroup` 是 cgroup2fs | 无 | 跑 agent-runtime 那台 | — | — |

**关于 #2 必须是 Docker**：`deploy/stage2/README.md` 明确说明 agent-exec 守护进程依赖
"Docker 的私有 cgroup 命名空间"这一行为，以及宿主 `/proc/1/ns/cgroup` 的挂载。
`deploy/chaos-mesh/README.md` 也记了旧集群配的是 `/var/run/docker.sock`，并警告
**不能因为同时存在 containerd socket 就推断用的是 containerd**。
**如果第三套环境是 containerd，这一块要重新验证，不能假定能跑。**

**关于 #5**：节点必须装 AppArmor profile `resbench-agent-runtime`
（[deploy/stage2/apparmor/resbench-agent-runtime](../../deploy/stage2/apparmor/resbench-agent-runtime)），
放到 `/etc/apparmor.d/`，`apparmor_parser -r` 加载，确认 `/sys/kernel/security/apparmor/profiles`
里那条以 `(enforce)` 结尾。**装了不等于合格**，还要跑真实的 UID/网络/IPC 检查。
新环境就是因为只有一台装了这个 profile，才必须用 nodeSelector 钉住。

### 2.2 被测系统

| # | 组件 | 目标 | 仓库资产 | 装哪台 | 资源 | 回滚 |
|---|---|---|---|---|---|---|
| 6 | OTel Demo | Helm release `otel-demo`，chart 0.40.5 / app 2.2.0，**23 个 Deployment 全就绪** | [environment/kubernetes/otel-demo/](../../environment/kubernetes/otel-demo/)（`values.yaml` 1389 行、`deployment.yaml`、`supplemental-manifests.yaml`）；装法用 `scripts/deploy_application.py` | 工作节点（与平台同一台，见 2.4） | **内存限额合计 ≈ 7.2 GiB**（28 条限额） | `helm uninstall otel-demo -n otel-demo` |

四个运行时占位符必须给值（[runtime.env.example](../../environment/kubernetes/otel-demo/runtime.env.example)）：
`HARBOR_REGISTRY`、`OTEL_DEMO_POSTGRES_PASSWORD`、`OTEL_DEMO_OPENAI_API_KEY`、
`OTEL_DEMO_GRAFANA_ADMIN_PASSWORD`。这份 env 文件**必须 0600**，否则平台拒绝运行。

`deploy_application.py` 支持 `--server-dry-run`，**先跑 dry-run 再 `--execute`**。

### 2.3 可观测与故障注入

| # | 组件 | 目标 | 仓库资产 | 装哪台 | 回滚 |
|---|---|---|---|---|---|
| 7 | prometheus / loki / jaeger / otel-collector / kube-state-metrics / node-exporter / promtail（namespace `observability`） | 与新环境同档 | **[deploy/observability/reference-stack.yaml](../../deploy/observability/reference-stack.yaml)**（21 个对象，非 Helm）；`environment/observability/` 那 7 个是装完之后往上打的补丁 | 工作节点 | `kubectl delete -f` |
| 8 | Coroot（operator 0.8.2，服务 `coroot-coroot.coroot.svc:8080`，开匿名只读） | 同新环境 | 官方 chart `coroot-operator` **0.8.2**；匿名只读靠 CR 的 `authAnonymousRole: Viewer` | 工作节点 | `helm uninstall` |
| 9 | ChaosBlade 1.8.0（operator + tool DaemonSet，namespace `default`） | 同新环境 | **[deploy/chaosblade/reference-install.yaml](../../deploy/chaosblade/reference-install.yaml)**（非 Helm） | 全部节点（DaemonSet） | `kubectl delete -f` |
| 10 | ChaosBlade `chaosblade-cgroupns-wrapper` ConfigMap | CPU/内存注入必须靠它 | **[deploy/chaosblade/cgroupns-wrapper.yaml](../../deploy/chaosblade/cgroupns-wrapper.yaml)** | `default` | 删 ConfigMap 会让 tool DaemonSet 起不来 |
| 11 | Chaos Mesh | 官方 Chart（旧集群 2.7.3 / 新集群 2.8.0，**按 k8s 版本选**） | [deploy/chaos-mesh/](../../deploy/chaos-mesh/)：`values-old-cluster.yaml`、`controller-bootstrap-rbac.yaml`、`namespace.yaml` | 控制面 + 各节点 daemon | `helm uninstall chaos-mesh -n chaos-mesh` |

**#7–#10 原本是这次最大的风险**（仓库没有可复现的安装资产）。
**2026-09-12 已从第二套环境只读查实**，结果见
[stage2-env3-reference-state-20260912.md](stage2-env3-reference-state-20260912.md)：

| 原缺口 | 现状 |
|---|---|
| `chaosblade-cgroupns-wrapper` 是什么 | **✅ 完全解决并已入库**：[deploy/chaosblade/](../../deploy/chaosblade/)。两行 `nsenter -t 1 -C` 包装，依赖 `hostPID: true` |
| Coroot 版本与匿名只读怎么开 | **✅ 解决**：operator chart 0.8.2；匿名只读靠 CR 的 `authAnonymousRole: Viewer` |
| 可观测栈 | **✅ 解决并已入库**：[deploy/observability/](../../deploy/observability/)，21 个对象的参照清单 |
| ChaosBlade | **✅ 解决并已入库**：[deploy/chaosblade/](../../deploy/chaosblade/)，operator + tool + cgroup 包装 |

**并且查出一条原方案低估的**：这些组件的镜像**几乎全部来自旧集群的 HTTP 明文 Harbor
`1.94.151.57:85`**（可观测栈走 `train-ticket/*`、Coroot 走 `observe/*`、
ChaosBlade operator 走 `ischaos/*`）。第三套环境在 CSTNET 网段，大概率路由不到它——
**P5 的镜像搬运范围要从"平台那两个镜像"扩大到"再加十几个第三方镜像"**。

**#11 Chaos Mesh 有资产但绑死旧集群**：`values-old-cluster.yaml` 里写死了
`tcse-v100-03` 之类的节点名和 Harbor 镜像引用，第三套环境要另出一份 values。
另外它明确依赖 `/var/run/docker.sock`——见 2.1 关于运行时的那条。

### 2.4 平台本体

| # | 组件 | 目标 | 仓库资产 | 装哪台 | 资源 | 回滚 |
|---|---|---|---|---|---|---|
| 12 | Deployment `resbench-stage2-integration`（Recreate，1 副本，三容器 + 1 初始化容器） | 同新环境 | [deploy/stage2/stage2-integration.yaml](../../deploy/stage2/stage2-integration.yaml) | **必须钉在装了 AppArmor 的那台** | 见下表 | `kubectl delete deploy` / 换回上一版镜像 |
| 13 | PVC `resbench-stage2-data` | 20Gi，RWO | [stage2.yaml:145-158](../../deploy/stage2/stage2.yaml#L145) | 同上 | 20Gi | 删 PVC（**会丢运行产物**） |
| 14 | RBAC：`resbench-stage2-controller` / `-executor` / `-finalizer` 三个 SA | 同新环境 | [deploy/stage2/execution-identities.yaml](../../deploy/stage2/execution-identities.yaml)（141 行） | 集群级 | — | `kubectl delete -f` |
| 15 | Secret `resbench-stage2-runtime` / `-gateway-client` / `litellm-upstream`；ConfigMap `litellm-config` | 同新环境 | **模板在 [deploy/stage2/litellm/](../../deploy/stage2/litellm/)，值不在仓库** | 同上 | — | 删 Secret |

三个容器的资源（来自 [stage2-integration.yaml](../../deploy/stage2/stage2-integration.yaml)）：

| 容器 | requests | limits | 行号 |
|---|---|---|---|
| `litellm` | 250m / 512Mi | 1 / 2Gi | :107-112 |
| `stage2`（控制器） | **1 / 2Gi** | **4 / 8Gi** | :199-204 |
| `agent-runtime` | 250m / 512Mi | 1 / 1Gi | :252-257 |
| **合计** | **1.5 核 / 3 GiB** | **6 核 / 11 GiB** | |

**加上 OTel Demo 的 7.2 GiB 和可观测栈，承载这些的那台机器建议不低于 16 核 / 32 GiB。**
新环境每台是 32 核 / 123 GiB。**第三套环境有多少，要盘点了才知道——这是能不能照搬布局的决定性数字。**

平台运行需要模型网关上游密钥（百炼 / DeepSeek），这部分**你单独给，不进仓库**。

### 2.5 镜像

| # | 组件 | 说明 |
|---|---|---|
| 16 | 控制器镜像 `stage2-d0-<sha>` + Agent 镜像 `stage2-agent-<sha>` | 用 [tools/dx-round/build_boundary.sh](../../tools/dx-round/build_boundary.sh) 构建 |

构建前置条件（**三样仓库里没有**）：

- Docker + buildx 构建器 `ischaos-builder`
- 环境变量 `BLADEAI_REPO` 指向 ChaosBlade 上游 worktree（约 200 MB）
- 环境变量 `BASES` 指向三个离线基础镜像目录（node 24.13.0 / node 22.21.1 / python 3.12.13，约 200 MB）

脚本硬要求：**工作树干净**、**同名 tag 已存在就拒绝**。

换镜像仓库要动两处：
- `tools/dx-round/build_boundary.sh` 第 22 行把 `REPO` 写死成了旧 Harbor，**必须改**；
- `scripts/build_stage2_image.py` 有 `--repository` / `--runtime-base` 两个参数
  （[:293-294](../../scripts/build_stage2_image.py#L293)），**可以命令行覆盖，不必改代码**。

但 `DEFAULT_RUNTIME_BASE` 是按 digest 钉死的旧 Harbor 基础镜像引用
（[:32](../../scripts/build_stage2_image.py#L32)），**那个基础镜像也得先搬到新仓库**，
否则构建拉不到。

O18 的改动在这里帮了忙：现在构建前会先跑 `build_manifest()`，
Dockerfile COPY 了不存在的源会**直接失败**，不会再产出一个悄悄缺文件的镜像。

---

## 3. 安装顺序与每步验收

每一步过了才做下一步。任何一步失败就按该步的回滚列停下报告，不连着重试。

> **2026-09-12 盘点后重写。** 原计划是按"空环境从零装"写的；实测集群已用 183 天，
> OTel Demo / Coroot / Chaos Mesh 都在跑，还有别人的 `aiops` 项目。
> 现在是"**往一套在用的共享集群里补四样东西**"，做法完全不同：
> 每一步的默认动作从"装"变成"**先验，确实缺才装**"，回滚也从"卸载"变成"**只回滚我们加的**"。
> 盘点结果见 [stage2-env3-inventory-20260912.md](stage2-env3-inventory-20260912.md)。

| 阶段 | 做什么 | 验收 | 失败回滚 |
|---|---|---|---|
| ~~**P0**~~ | ~~盘点~~ | ✅ **已完成**（三台各 35 条 FACT） | — |
| ~~**P1**~~ | ~~出差距表~~ | ✅ **已完成**（阻塞级 1 项） | — |
| **P1.5** | **确认共享边界**：`otel-demo` / `coroot` / `chaos-mesh` 归谁，我们能不能改；`aiops` 确认不碰 | 你答复 | — |
| **P2** | AppArmor profile 装到**要跑 agent-runtime 的那台**（建议 `otcaix-62`，理由见下） | `aa-status` 里 `resbench-agent-runtime` 以 `(enforce)` 结尾 | `apparmor_parser -R`，不影响其它 155 个 profile |
| **P3** | ~~装 OTel Demo~~ → **只验**：23 个 Deployment 全就绪、`load-generator` 有流量、chart 是 0.40.5 | 已实测通过 | **不动它**（可能是别人的） |
| **P4a** | 装**可观测栈**（唯一真正缺的观测件），namespace `observability` | Prometheus/Loki/Jaeger 能查；promtail 抓到 `otel-demo` 日志 | `kubectl delete -f reference-stack.yaml`，只删我们建的 namespace |
| **P4b** | 装 **ChaosBlade** operator + tool + cgroup 包装，namespace `default` | `blade` 能在 tool 容器里跑；**CPU 注入实测压得动 `cart`**（不只看账本） | `kubectl delete -f reference-install.yaml` + 删 ConfigMap |
| **P4c** | ~~装 Chaos Mesh~~ → **先验现有的**：`NetworkChaos` / `PodChaos` / `StressChaos` 三类能不能用 | 三类 CRD 存在且能创建能删 | 不动；不行再按 P1.5 的答复决定并排装还是换 |
| ~~**P4d**~~ | ~~Coroot 只验~~ | ✅ **已完成**：匿名可访问（`authAnonymousRole=Admin`），项目 id = **`po24tcoz`**，`/prom/api/v1/series` 200，能读到 `/k8s/otel-demo/cart` | — |
| **P5** | 构建平台两个镜像。**范围缩小了**：旧 Harbor 可达，十几个第三方镜像不必搬 | `build-<sha>-image.json` 产出 | 不覆盖同名 tag |
| **P6** | 平台：RBAC → Secret/ConfigMap → PVC → Deployment。**overlay 已就绪**：`python deploy/stage2/env3/render.py` | 三容器 Ready；`scripts/verify_stage2_deployment.py` 全绿（渲染产物已离线自检通过） | `kubectl delete ns resiliencebenchmark-system`（全是我们新建的，干净） |
| **P7** | `/api/v1/stage2/options` 三家可跑 | 三家 `runnable=true` | 看 `provider_circuits` / `serving_stale_result` 分辨是网关还是资格问题 |
| **P8** | 跑通一次 C0 | 完整结束、结构化结果、残留检查 `none` | 注意 O04 改动后恢复未验证时平台会停下而不是重装 |

**P2 之后每一步先说一声再动手。** 这是共享集群，比前两套环境更需要如此。

### 为什么建议把平台放 `otcaix-62`

| | `otcaix-60` | `otcaix-61` | `otcaix-62` |
|---|---|---|---|
| 角色 | control-plane | worker | worker |
| 内存 | 251 GiB | **125 GiB** | 251 GiB |
| Docker | 29.3.1 | 29.0.0 | **28.3.2** |
| 时间同步 | ✅ | **❌ 未同步** | ✅ |

- 不放 60：它是控制面，平台 limits 6 核 / 11 GiB 加上 agent-exec 的 cgroup 操作，不该压控制面
- 不放 61：内存只有一半，而且**时间没同步**（证据窗口对齐依赖它）
- 放 62：资源足、时间同步正常

**已核实 62 的前提全部满足**（见盘点 4.5.6）：AppArmor 模块已加载（170 profiles / 75 enforce）、
cgroup 是 v2、`/sys/fs/cgroup` 权限 555（正是 README 描述的情形）、
控制器有 `cpu` / `memory` / `pids`、Docker cgroup driver 是 systemd。

**唯一的代价**：62 的 Docker 是 28.3.2，三台里最旧。agent-exec 依赖 Docker 的私有
cgroup 命名空间行为，**P2 装完 AppArmor 后要单独验一次**（第二套环境实测的是 29.6.x）。
那要起容器，是写操作，所以放在 P2 之后。

### ⚠️ 可观测栈的资源占用：jaeger 是个隐患

启动语要求方案写清"占多少资源"。这一项补上——**而且查出一个必须先处理的问题**。

`deploy/observability/reference-stack.yaml` 里**只有 `node-exporter` 声明了 resources**
（每节点 req 50m/64Mi、lim 200m/256Mi）；**jaeger、prometheus、loki、promtail、
otel-collector、kube-state-metrics 六个全都没有 requests 也没有 limits。**

第二套环境同一批负载的**实际**占用（`kubectl top`）：

| 组件 | CPU | 内存 |
|---|---:|---:|
| **jaeger** | 10m | **18,842 Mi（≈18.4 GiB）** |
| prometheus | 20m | 1,296 Mi |
| loki | 5m | 157 Mi |
| promtail ×2 | 15m | 93 Mi |
| otel-collector | 1m | 32 Mi |
| kube-state-metrics | 3m | 22 Mi |
| node-exporter ×2 | 13m | 24 Mi |
| **合计** | **≈67m** | **≈20.4 GiB，其中 92% 是 jaeger** |

外加 PVC `prometheus-tsdb` **20Gi**（`nfs-client`，RWO）。

**jaeger 是 all-in-one + 内存存储，没有 limit 就会一直涨。** 在第二套环境那是独占集群，
涨到 18 GiB 也就算了；**第三套环境上还跑着别人的 `aiops`，无上限的 jaeger 迟早挤掉别人。**

**建议（P4a 动手前先定）**：

1. 给 jaeger 加内存 limit，并设 `--memory.max-traces`（或换成带后端存储的部署）；
2. 顺手给 prometheus / loki 也加上 limit——它们现在同样无上限；
3. 三台各 64 核 / 125–251 GiB，**容量本身不是问题**，问题是"无上限"这件事本身
   在共享集群上不可接受。

**这条要你拍板**：加 limit 会改变与第二套环境的一致性（那边是没有 limit 的），
要不要为了共享安全而偏离参照状态。

### 新增的两条禁忌（共享集群特有）

1. **不碰 `aiops`**（`isaiops-be/fe/gateway/problems`，别人的项目）。
2. **不动 `chaos-mesh` 的现有安装**，直到 P1.5 确认归属——它是 chart `0.0.0` 的自定义构建
   （镜像带 `nomongo` / `fix` 补丁），很可能就是 `aiops` 在用。

### P6 的验收已经脚本化

操作手册第 4 节和整改说明第 8.3 节都要求每次换镜像后**手工核对三件事**。
这三件事失败时是**静默的**：Pod 起来了、rollout 成功了，坏的是下一次运行。
现在有脚本了：

```bash
python scripts/verify_stage2_deployment.py \
  --namespace resiliencebenchmark-system \
  --coroot-project <第三套环境自己的 Coroot 项目 id> \
  --require-node-selector \
  --private-listing <在 stage2 容器里抓的 "mode path" 清单>
```

只读，不写集群。检查：三个容器齐、`fsGroup=10001`、
`fsGroupChangePolicy=OnRootMismatch`、两个 Coroot 环境变量在位**且项目 id 是本集群的**、
nodeSelector 在位、私有文件没有组或其他用户可读写。任一不过退出码 1。

**拿它跑仓库里那份清单会直接爆三条**，正好说明了"永远不要 `kubectl apply` 仓库清单"是什么意思：

```
FAIL  fsGroupChangePolicy: is None, expected 'OnRootMismatch'
FAIL  RESBENCH_COROOT_PROJECT_ID: is '9auios5b', expected 'p1nar0hw' for this cluster
FAIL  nodeSelector: absent, but this cluster pins the workload to an AppArmor-enabled node
```

---

## 4. 需要你给的东西（盘点后更新）

| # | 缺什么 | 卡住哪一步 | 盘点后的变化 |
|---|---|---|---|
| 1 | ~~SSH 放行~~ | — | **✅ 解决**：本机 Clash TUN 劫持，绑 en0 源地址即可绕过 |
| 2 | **共享边界确认**：`otel-demo` / `coroot` / `chaos-mesh` 归谁，我们能不能改 | **P1.5，现在最关键** | **新增**。原以为是空环境，实际有别人的 `aiops` 在跑 |
| 3 | ~~k8s v1.29.15 要不要升~~ | — | **✅ 不用升**。门槛是我设错了：平台清单用 AppArmor beta 注解（1.30 前的写法，也是 1.29 唯一接受的形式），无任何 1.30+ 特性，README 本就有「1.28 compatibility」一节。**阻塞级差距归零** |
| 4 | **Chaos Mesh 现有安装怎么处理** | P4c | **新增**。chart `0.0.0` 自定义构建，与仓库官方 values 对不上 |
| 5 | 公开镜像仓库地址 | P5 | **范围缩小**：旧 Harbor 可达，只需构建平台两个镜像 |
| 6 | `BLADEAI_REPO` 与 `BASES` 两份外部材料 | P5 | 不变；或由你那边构建把元数据给我 |
| 7 | 模型网关上游密钥（百炼 / DeepSeek） | P6 | 不变 |
| 8 | ~~OTel Demo 四个运行时占位符~~ | — | **✅ 不需要**：OTel Demo 已经在跑，不重装 |
| 9 | ~~确认是否也要主机 MCP~~ | — | 仍待确认，但优先级降低（Stage-2 不走那条路） |
| 10 | **swap 三台全开、`otcaix-61` 时间未同步**，要不要处理 | P2 前 | **新增** |
| 11 | ~~三台角色分配~~ | — | **✅ 已知**：60 控制面，61/62 worker。平台建议放 62（理由见第 3 节） |

## 5. 已知风险（盘点后更新）

| 原风险 | 实测结果 |
|---|---|
| ~~容器运行时可能不是 Docker~~ | **✅ 是 Docker**。但三台版本不一致（29.3.1 / 29.0.0 / **28.3.2**），平台落在哪台要单独验 |
| ~~可用资源可能不够~~ | **✅ 远超需要**：三台各 64 核，251 / 125 / 251 GiB |
| ~~出不了网~~ | **✅ 全通**：registry.k8s.io / ghcr.io / quay.io 都可达；**旧 Harbor 也可达** |
| ~~内核 < 5.12~~ | **✅ 6.8.0**，满足 `mount_setattr` 要求 |
| ~~四项组件没有可复现资产~~ | **✅ 已解决**：全部入库（见 2.3 节） |

**盘点新暴露的风险：**

1. **共享集群**。`aiops` 是别人的项目（181 天）。`otel-demo` / `coroot` / `chaos-mesh`
   归属未知——**如果 OTel Demo 是别人在用，我们注故障会影响他们**。这是 P1.5 必须先问清的。
2. ~~k8s v1.29.15~~ **已核实不是问题**：清单用 1.30 之前的 AppArmor beta 注解，
   没有任何 1.30+ 特性，`deploy/stage2/README.md` 本就有「Kubernetes 1.28 compatibility」一节。
3. **Chaos Mesh 是自定义构建**（chart `0.0.0`，镜像带 `nomongo` / `fix` 补丁）。
   D8 要的 `NetworkChaos` / `PodChaos` / `StressChaos` 三类能不能用必须实测，不能假定。
4. **swap 三台全开**。集群跑了 183 天说明 kubelet 配了 `failSwapOn: false` 或另有安排，
   动它要重启 kubelet——共享集群上这是有影响的操作。
5. **`otcaix-61` 时间未同步**。证据窗口与指标对齐依赖它；平台不建议放这台。
6. **61 / 62 上 `zhengmingzhuo` 没有 kubeconfig**，只有 60 能 `kubectl`。
   装 AppArmor 和验 Docker 行为要在目标节点上本地做。

## 6. 现状

已完成，见 [stage2-env3-inventory-20260912.md](stage2-env3-inventory-20260912.md)。

一句话：**集群已用 183 天，OTel Demo（chart 0.40.5，23 个 Deployment 全就绪）、
Coroot（operator 0.8.2）、Chaos Mesh 都在跑；真正缺的是可观测栈、ChaosBlade
（含 cgroup 包装）、平台本体、AppArmor profile 四样。**
