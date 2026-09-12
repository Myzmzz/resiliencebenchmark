# 第三套环境准备方案（2026-09-12）

目标机器：`124.16.138.60` / `.61` / `.62`
分支：`codex/stage2-env3-bladeai-20260912`
状态：**盘点未执行（机器连不上），本方案是"目标状态 + 装法 + 回滚"，现状一栏待盘点后填**

---

## 0. 先说卡点

盘点做不了，不是账号问题——**TCP 到 22 端口连得上，但对端在 SSH banner 交换之前就断开**：

```
kex_exchange_identification: Connection closed by remote host
```

三台一样。对照组：同一台机器连旧集群 `1.94.151.57:22`、新集群 `62.234.93.223:22` 都能走到
`Permission denied (publickey,password)`，说明本机 SSH 出网正常。再从旧集群主机上探这三台，
22 端口同样不通。应用层（80/443/6443）也无任何响应。

结论是**入站被网络边界挡住**，大概率源 IP 白名单或内网限制。

**需要你做一件事，二选一：**

1. 把出口 IP **`12.104.14.23`** 加进这三台的 SSH 放行名单；
2. 或给一台能连到它们的跳板机（地址 + 账号）。

另外**镜像仓库地址还空着**（启动语里是 `<仓库地址待填>`），构建推镜像那步要用。

盘点脚本已备好：[tools/env3/inventory.sh](../../tools/env3/inventory.sh)，12 个章节，
纯只读、不装不改不启服务，没 root 也能跑完（拿不到的标 N/A）。一开通就能出结果。

---

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
第三套环境需要一份**属于它自己的** overlay，我建议新建 `deploy/stage2/env3/`
存这几个差异值，而不是改共用清单——后者会污染另外两套环境。

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

| 阶段 | 做什么 | 验收 | 失败回滚 |
|---|---|---|---|
| **P0** | 跑 `tools/env3/inventory.sh`，三台各一份 | 拿到 12 章完整输出 | 无副作用 |
| **P1** | 填本文档"现状"列，列出真实差距，**发你确认** | 你点头 | — |
| **P2** | 底座：k8s（若无）、存储类、cgroup v2 确认、AppArmor profile 装载 | `kubectl get nodes` 全 Ready；`aa-status` 里 profile 是 `(enforce)`；`stat -fc %T /sys/fs/cgroup` = `cgroup2fs` | 卸载 profile；`kubeadm reset`（仅限我们新装的） |
| **P3** | OTel Demo：`deploy_application.py --server-dry-run` → `--execute` | 23 个 Deployment 全就绪；`load-generator` 有流量 | `helm uninstall otel-demo` |
| **P4** | 可观测栈 + Coroot + ChaosBlade + Chaos Mesh | Prometheus/Loki/Jaeger 能查；Coroot 匿名只读通；ChaosBlade CPU 注入能起能清；Chaos Mesh Controller 1/1、daemon 全就绪 | 逐个 `helm uninstall`，互不影响 |
| **P5** | 构建并推镜像到公开仓库 | `build-<sha>-image.json` 产出；两个 tag 在仓库里 | 不覆盖同名 tag，重建换新 sha |
| **P6** | 平台：RBAC → Secret/ConfigMap → PVC → Deployment（**用 env3 专属 overlay**） | 三容器 Ready；**跑 `scripts/verify_stage2_deployment.py` 全绿**（见下） | `kubectl delete deploy`；PVC 保留 |
| **P7** | 验收：`/api/v1/stage2/options` 三家可跑 | 三家 `runnable=true`，模型探测 complete | 看熔断与探测状态（O03/O10 新增的 `provider_circuits` / `serving_stale_result` 字段能直接看出是网关问题还是资格问题） |
| **P8** | 跑通一次 C0 | 完整结束、有结构化结果、残留检查 `none` | 按平台复位流程；**注意 O04 改动后，恢复未验证时平台会停下而不是重装** |

P2–P6 每一步**先说一声再动手**（整改说明第 2 节的协调规则）。

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

## 4. 需要你给的东西（汇总）

| # | 缺什么 | 卡住哪一步 | 备注 |
|---|---|---|---|
| 1 | **SSH 放行 `12.104.14.23` 或跳板机** | P0，**现在就卡** | 全部工作的前提 |
| 2 | **公开镜像仓库地址** | P5 | 启动语里还是 `<仓库地址待填>` |
| 3 | 可观测栈 / Coroot / ChaosBlade 的 chart 版本与 values | P4 | 或授权我从新环境导出 |
| 4 | `chaosblade-cgroupns-wrapper` ConfigMap 内容 | P4 | 没有它 CPU/内存注入不可用 |
| 5 | `BLADEAI_REPO` 与 `BASES` 两份外部材料（各约 200 MB） | P5 | 或者由你那边构建，把元数据给我 |
| 6 | 模型网关上游密钥（百炼 / DeepSeek） | P6 | 不进仓库 |
| 7 | OTel Demo 四个运行时占位符的值 | P3 | 不进仓库 |
| 8 | **确认第三套环境是否也要主机 MCP**（见 1.2） | P4/P7 | 影响验收口径和工作量 |
| 9 | 三台的角色分配（哪台控制面、哪台跑负载） | P2 | 也可按盘点结果由我提议 |

---

## 5. 已知风险

1. **容器运行时如果不是 Docker**，agent-exec 的 cgroup 方案和 Chaos Mesh 的 socket 配置都要重做，
   这不是改配置的量级。盘点第 4 章会看到。
2. **可用资源可能不够**。新环境每台 32 核 / 123 GiB；光被测系统 + 平台 limits 就约 6 核 / 18 GiB。
   盘点第 3 章会看到。
3. **出网**。盘点第 8 章会探 registry-1.docker.io、ghcr.io、registry.k8s.io 等。
   出不去就必须全部走公开镜像仓库镜像化，工作量明显变大。
4. **内核版本 < 5.12** 会让 AppArmor 那套 `mount_setattr` 方案不成立。盘点第 1 章会看到。
5. **四项组件没有可复现的安装资产**（见 2.3）。这是本方案里最不确定的部分。

---

## 6. 现状（待填）

盘点跑完后填这一节，然后整份发你确认再动手。

| 检查项 | .60 | .61 | .62 |
|---|---|---|---|
| OS / 内核 | | | |
| 时间同步 | | | |
| CPU / 内存 / 磁盘 | | | |
| 容器运行时 | | | |
| Kubernetes 有无 / 版本 / 角色 | | | |
| 存储类 | | | |
| CNI | | | |
| DNS / 出网 | | | |
| cgroup 版本 / AppArmor | | | |
| 端口占用 | | | |
| 已有相关制品 | | | |
| 节点互通 | | | |
