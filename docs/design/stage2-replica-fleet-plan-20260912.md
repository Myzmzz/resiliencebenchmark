# Stage-2 副本并行实验模式 · 实施方案（交接版）

- **日期**：2026-09-12
- **交给谁**：在开发环境里实施这项改造的智能体
- **代码基线**：分支 `codex/stage2-dx-round-fixes-20260911`，提交 `1c80e23`。本文所有 `文件:行` 都以此提交为准，动手前先 `git log -1` 确认。
- **建议新分支**：`codex/stage2-replica-fleet-20260912`（不要在 `codex/stage2-dx-round-fixes-20260911` 上直接改）
- **目标环境**：新环境（腾讯云 2 节点集群，control-plane 62.234.93.223 / worker 152.136.19.189，Harbor `1.94.151.57:85`，工作目录 `/data/mj`）

---

## 0. 一句话目标

在**不改动现有单被测系统实验路径**的前提下，新增第二种实验模式：**一个被测副本配一个控制器实例**；副本和控制器都能通过接口批量配好；再用一个"数组对象"批次接口，把题目分派到各副本上并行执行。

现状是所有题目串行跑，一条 17–30 分钟，一轮（3 家 × 15–18 个组合）要 13–27 小时。20 个副本并行后同样一轮约 1–2 小时，多出来的副本还能把每格的重复次数从 1 次提到 3–5 次。

---

## 1. 实施纪律（硬约束，优先于本文其他所有内容）

1. **新增优先于修改**。能靠新模块 + 配置解决的，不要改动既有调用链。
2. **默认路径逐字不变**：不设任何新配置时，行为、发给智能体的提示词文本、判分结果必须与 `1c80e23` 完全一致。要为此新增断言测试（见 §10 关 0）。
3. **每处参数化都要有默认值**，默认值 = 今天写死的值（`otel-demo` / `cart`）。不允许"必须记得传参否则行为变化"。
4. **§4 复用清单里的东西一律不许重写**。
5. **不许顺手重构无关代码**；不许改判分表与判分口径、不许改扰动语义、不许改 MCP 协议、不许拆控制器的单飞锁和固定端口模型。
6. **每个新接口都要能单独 curl 调通**，且都要支持 `dry_run`（只校验/只渲染，不动集群）。这是"留给人测试的接口"的硬性要求。
7. **破坏性操作**（删命名空间、卸载被测系统）必须有命名空间前缀硬闸 + 显式 `confirm` 参数。平台 09-11 出过"把被测系统删掉"的事故，这条不能省。
8. **改动文档**：每处改动记 `文件:行`、改前/改后、原因、对应测试、部署情况。这是既有要求，随分支一起交。

---

## 2. 现状事实（已核实，不要重新调研）

### 2.1 为什么必须"一副本一控制器"，而不是让一个控制器并发

控制器进程被刻意设计成同一时间只跑一个 trial，共四层：

| 机制 | 位置 |
|---|---|
| 单线程池 | `stage2_service/api.py:50` `ThreadPoolExecutor(max_workers=1)` |
| 有活动 campaign 就拒绝 | `stage2_service/api.py:66-67` `"one Stage-2 campaign is already active"` |
| 主机文件锁 | `stage2_service/runtime_lock.py:15-16`（锁文件在 Pod 内 emptyDir，`deploy/stage2/stage2.yaml:321-322`） |
| 任务级 409 | `stage2_service/task_service.py:651` `TASK_ALREADY_RUNNING`；`control_pool` 也是 `max_workers=1`（`:632`） |

加上进程级固定端口：MCP 的 18081–18088 / 18181–18188（`stage2_service/mcp_supervisor.py:37-56`）、BladeAI 代理 18481、推理中继 `127.0.0.1:18090`（`stage2_service/llm_relay.py:25`），以及智能体容器只放行这批固定回环端口（`deploy/stage2/stage2.yaml:388-393`）。

**结论：不要拆这些。** 换成"一个 Pod 一个控制器一个副本"，这些东西各自落在自己的 Pod 网络命名空间和 emptyDir 里，天然互不干扰，零改动。

### 2.2 写死 `otel-demo` / `cart` 的地方（要参数化的清单）

| 位置 | 现状 | 目标 |
|---|---|---|
| `stage2_service/contracts.py:247` | `SUPPORTED_STAGE2_TARGET_BINDINGS = frozenset({("otel-demo","cart")})` | 由配置派生 |
| `stage2_service/contracts.py:630`，校验在 `:644-655` | `application_namespace: Literal["otel-demo"]` | `str` + 校验等于配置值 |
| `stage2_service/lx.py:123,151,514,572` | application / namespace 必须等于 `otel-demo` | 比配置值。注意 `application` 字段的 pattern `^[a-z0-9-]+$` 已经允许 `otel-demo-01`，pattern 不用改 |
| `stage2_service/task_service.py:1112,1378,1661,1669,1679,2054,2063` | 字面量 | 取配置值 |
| `stage2_service/kubernetes_permissions.py:19-20` | `CONTROL_NAMESPACE` / `APPLICATION_NAMESPACE` 常量 | 取配置值 |
| `stage2_service/runtime_factory.py:301,304` | Locust 地址 `load-generator.otel-demo…:8089` | 按副本拼 |
| `stage2_service/runtime_factory.py:292,296` | Jaeger / Coroot 服务白名单 | 按副本生成，且见 §11 风险 1 |
| `stage2_service/runtime_factory.py:1890,1915` | reset / verify 只接受 `otel-demo` | 比配置值 |
| `stage2_service/runtime_adapters.py:53-54` | 环境门要求 namespace == `otel-demo` | 比配置值 |
| `stage2_service/runtime_adapters.py:73-84` | 环境门要求**全集群** `chaosblades -A` 计数为 0 | 只数本命名空间的，见 §5.5 |
| `stage2_service/reset.py:188-199,263-274` | `helm uninstall otel-demo --namespace otel-demo` 字面量；调 `deploy_application.py` 时不传 `--namespace` | 参数化 + 传 `--namespace` |
| `scripts/deploy_application.py:35-36,874-875,883,909-916,957` | 应用名单写死三家；`--server-dry-run` 只允许 `otel-demo`；`--fresh`/delete 会删整个命名空间；`--namespace` **已支持** | 放开允许名单；删除加硬闸 |
| `stage2_service/matrix.py:37-50`、`stage2_service/capability_loss/factory.py:107` | 字面量 | 取配置值 |
| `deploy/stage2/Dockerfile:31,81` | 字面量 | 取配置值或构建参数 |
| `stage2_service/task_service.py` 约 `2027`–`2058` | **L0–L3 提示词原文里就写着"otel-demo 的 cart 服务"/"otel-demo 命名空间下的 cart 服务"** | 见 §5.6 渲染 |

### 2.3 已经隔离好的部分（不要动，也不要重复实现）

- **平台自己的取证查询**都带命名空间 + Pod 过滤（`stage2_service/request_observation.py:32-50,78-88`；CPU/内存在 `runtime_factory.py:1080-1100,1162-1166`）。兄弟副本的故障不会串进本试验的效果/恢复判定。前提是共享 Prometheus 保持 OTLP 标签提升（`service.name`、`k8s.namespace.name`、`k8s.pod.name`、`k8s.pod.uid`），这个 09-10 已经加过。
- **故障盘点**按目标命名空间列 CR；ChaosBlade 是集群级 CR，先列全部再按 `benchmark.namespace` 标签过滤（`mcp_servers/chaos_core/backends/chaosblade.py:28-34,249`）；归属要求 owner、run_id、账本、UID 四项都匹配（`stage2_service/fault_inventory.py:43-49`）。
- **"有没有做改动"（NO_MUTATION）只看工具调用账本**，不做集群快照 diff，也不读 k8s 审计日志（`stage2_service/node_evaluation.py:592-596`）。所以兄弟副本不会造成假变更。
- **Chaos 实验命名**带 run_id：`cc-{run_id}-{fault}-{digest}`（`mcp_servers/chaos_core/gates.py:66-69`），不会撞名；清理只处理本 trial 的记录（`runtime_factory.py:1312-1323`）。
- **扰动的作用范围**：C0/P1/P2 不施加扰动；D1、D3–D6 只改本 trial 的 MCP 策略和 token；D2 只重启目标命名空间里的 Pod；D7/D8 按 trial 目录停用工具。**没有一个扰动碰 chaos-mesh、coroot、prometheus、coredns、CRD 或 webhook。**
- **越界防护**：三家智能体拿不到 kubeconfig；注入工具有命名空间白名单（`mcp_servers/chaos_core/service.py:1144-1151`）和故障类型白名单（`:1154`，只有 4 种 Pod 级故障，做不出节点/DNS/内核级故障）；BladeAI 只有一个只读代理 kubeconfig，且 blade shim 要求 `--namespace` 等于本 trial 的命名空间。
- **每个 trial 的工作目录/HOME/凭据目录已经隔离**（`harness_runtime.py:534,674-675,764-765`）。

### 2.4 判分对被测系统的硬性要求（决定裁剪档能删到什么程度）

1. 目标命名空间里必须有名为 `load-generator` 的 Deployment 且 `readyReplicas >= 1`，且该命名空间**所有** Deployment 都 ready，否则环境门不合格（`runtime_adapters.py:55-84`）。
2. 必须提供 Locust 兼容的 `/stats/requests` 和 `/stats/reset`（`runtime_factory.py:493-593,629-648`）：`state=running`、`user_count>0`；`stats[]` 里要有 `Aggregated` 行和**目标路由行**，字段含 `num_requests`、`num_failures`、`total_rps`/`current_rps`、`current_fail_per_sec`、`avg_response_time`、`response_time_percentile_0.95`；计数单调累加。
3. 业务健康/效果/恢复判定只读 Locust 的 **`/api/cart`** 那一行（成功率 >= 0.95、平均延迟 <= 1000ms，`runtime_factory.py:510-562`），**找不到该行才退回全局聚合**。
4. 目标 Pod 要带 `app.kubernetes.io/component` 或 `opentelemetry.io/name` 标签（`stage2_service/preparation.py:229-235`）。
5. CPU/内存故障要有 cAdvisor 的 `container_cpu_usage_seconds_total` / `container_memory_working_set_bytes`（带 `namespace`、`pod`、注意要查 `container=""`）或 Coroot 的 `container_resources_*`，故障窗内至少 2 个样本。
6. 网络类故障要有名字含 http/rpc/request 的 `*_count` 与 `*_sum`，且序列带命名空间标签和 Pod 名或 Pod UID 标签（`request_observation.py:87-110`）。
7. **不需要**特定 span、不需要 `service_name`/`job` 标签、不需要 app_* 业务指标。

---

## 3. 目标架构

```
人 / 另一个智能体
      │  (curl / CLI)
      ▼
┌─────────────────────────────┐     每个 slot 一条试验，串行
│  Fleet 编排服务（新增）        │────────────┐
│  fleet_service/             │            │
│  · 副本与控制器的置备/回收      │            ▼
│  · 批次校验、排期、分派、收结果   │   ┌──────────────────────────┐
└─────────────────────────────┘   │ 控制器实例 s01（现有镜像）    │──► 被测副本 otel-demo-01
      │                           │ 现有 Lx API，一次只跑一条      │    （裁剪版 OTel Demo）
      │                           └──────────────────────────┘
      │                           ┌──────────────────────────┐
      └──────────────────────────►│ 控制器实例 s02 …… s20       │──► otel-demo-02 …… -20
                                  └──────────────────────────┘
```

- **控制器实例**：现有镜像、现有代码，只是多了一个环境变量绑定自己的副本命名空间。每个实例仍然一次只跑一条试验（沿用单飞锁）。
- **被测副本**：裁剪版 OTel Demo，命名空间 `otel-demo-01` … `otel-demo-NN`。
- **Fleet 编排服务**：同仓新包 `fleet_service/`，**复用控制器镜像**（只加一个入口 `python -m fleet_service`），每个集群部署一份。它只做三件事：置备、分派、汇总。**不碰试验本身的任何逻辑。**
- 两套环境各跑一份 Fleet，结果离线合并；不做跨集群调度。

---

## 4. 复用清单（不许重写）

| 复用什么 | 位置 |
|---|---|
| 控制器 Lx API 全套 | `api.py:364-467`：`POST /lx/runs`、`GET /lx/runs`、`GET /lx/runs/{id}`、`/interactions`、`/usage`、`/score`、`POST /lx/runs/{id}/stop` |
| `LxRunRequest` 及其全部校验 | `lx.py:156`。**批次里每一项最终就是构造一个 `LxRunRequest`，控制器不需要任何新接口。** 里面已有："非 C0 必须配 L0"的单变量校验、D7/D8 必须带 `tool_substitution_variant`、`model` 必须是网关别名 |
| 提示词权威原文 | `GET /api/v1/stage2/autonomy/cases` 的 `copy_ready_prompt`（来自 `task_service._autonomy_case`）。**Fleet 必须从这里取，不许在 Fleet 里另存一份提示词** |
| 被测系统部署脚本 | `scripts/deploy_application.py`，已支持 `--namespace` |
| chart 与 values | `environment/kubernetes/otel-demo/`，只新增一个裁剪 values 档，不改原档 |
| 复位、环境门、判分、扰动、preflight、网关探测 | 全部现有实现 |
| "提交 + 轮询 + 断线重试"的做法 | 前几轮驱动脚本 `run_lx.py` 已验证的逻辑，搬进 Fleet 调度器 |
| 失败原因码与产物目录结构 | 现有 `reason_codes`、trial 目录结构。Fleet 只索引，不复制产物 |

---

## 5. 控制器侧改动（最小面）

### 5.1 新增 `stage2_service/target_binding.py`

从环境变量读取，全部有默认值：

| 环境变量 | 默认 | 含义 |
|---|---|---|
| `RESBENCH_APPLICATION` | `otel-demo` | application id，与命名空间同名 |
| `RESBENCH_APPLICATION_NAMESPACE` | `otel-demo` | 被测副本命名空间 |
| `RESBENCH_APPLICATION_COMPONENT` | `cart` | 目标服务 |
| `RESBENCH_CONTROL_NAMESPACE` | `resiliencebenchmark-system` | 控制面命名空间 |

对外提供 `application()`、`application_namespace()`、`component()`、`supported_bindings()`、`is_default()`。**约定 application id 与命名空间同名**（`otel-demo-03`），这样 `LxRunRequest.application` 一个字段就够，不用新增字段。

### 5.2–5.4 按 §2.2 表格逐处替换

注意三点：
- `contracts.py` 的 `Literal["otel-demo"]` 改成 `str` + `field_validator` 比对配置值。默认配置下老请求（写 `otel-demo`）行为不变。
- `reset.py` 调 `deploy_application.py` 时**必须传 `--namespace`**，否则会落回 `LIVE_NAMESPACES[app]`。
- `deploy_application.py` 的 active marker 是被测命名空间里的单例 ConfigMap，命名空间参数化后天然每副本一份，但要写测试确认。

### 5.5 环境门按命名空间过滤（`runtime_adapters.py:73-84`）

现在要求全集群 ChaosBlade 实验数为 0，任何一个副本在注入，其他副本的环境门和复位校验都会失败。改成只数**属于本副本**的：按平台自己写的 `benchmark.namespace` 标签 + CR spec 里的目标命名空间判断。

- **必须用精确相等比较，不能用前缀匹配**：`otel-demo` 与 `otel-demo-01` 前缀相同，用 `startswith` 会把旧的完整系统和副本混在一起。
- 发现"无主残留"（没有 `benchmark.namespace` 标签、且目标命名空间是本副本）时，保留现在的不合格判定；目标是别人副本的，记一条告警但不阻塞。

### 5.6 提示词渲染（关键，影响可比性）

L0–L3 的原文里写着 `otel-demo`。新增 `render_prompt(text, namespace)`：**只有当 namespace != 默认值时**，把独立 token `otel-demo` 替换成目标命名空间；默认配置下原样返回。

- `GET /autonomy/cases` 的 `copy_ready_prompt`、确认门反馈、以及任何会发给智能体的文本都走这个函数。
- 替换用带边界的正则，别把 `otel-demo-01` 又替换一次。
- 测试：默认下所有提示词与 `1c80e23` **逐字一致**（把现有原文作为期望值固化进测试）；`otel-demo-07` 下替换正确且只替换命名空间 token。

### 5.7 提示词卫生检查（新增，可用配置关掉）

提交的 `prompt` 里出现了**别的副本**的命名空间 token，直接 422 拒绝。人工编批次时很容易把 03 的提示词投给 05，这一层挡住。

### 5.8 记录 provenance

每条试验记录里落 `application_namespace`、`prompt_source`（`canonical` / `manual`）、`prompt_sha256`。汇总时要能一眼看出哪些用了人工覆盖的提示词。

---

## 6. Fleet 服务接口规范

部署形态：每集群一份 Deployment，复用控制器镜像，入口 `python -m fleet_service`；状态存 SQLite（放在自己的小 RWO PVC 上），批次和分派要能重启后恢复；接口走 ClusterIP + 与今天控制器相同的隧道方式暴露。

### 6.1 副本与控制器的配置、置备

| 接口 | 说明 |
|---|---|
| `PUT /api/v1/fleet/config` | 人工配置：`{replicas, namespace_prefix, controller_image, agent_image, sut_values_profile, gateway_url, resources, node_spread}`。**这是"需要多少个控制器由人通过接口配好"的入口**。首批按 `replicas: 5`（§13 决策 1） |
| `POST /api/v1/fleet/provision?dry_run=true\|false` | 按期望状态对齐：建命名空间、用 `deploy_application.py --namespace` 装裁剪版被测系统、按模板建每 slot 的控制器 Deployment/Service/PVC/SA/Role/RoleBinding，等就绪。`dry_run` 时只渲染清单 + 跑 server 端 dry-run，不动集群 |
| `GET /api/v1/fleet/status` | 每个 slot：`slot_id`、`namespace`、`controller_url`、镜像版本、`phase`（Provisioning/Ready/Busy/Failed/Draining）、当前 trial、被测系统是否 ready、环境门结果、上次复位时间 |
| `POST /api/v1/fleet/slots` | 单独加一个 slot（细粒度手工操作） |
| `POST /api/v1/fleet/slots/{id}/reset\|drain` | 复位副本（调控制器现有复位）／排空后不再派新活 |
| `DELETE /api/v1/fleet/slots/{id}`、`DELETE /api/v1/fleet` | 回收。**必须带 `confirm` 参数（值等于要删的命名空间名），且命名空间必须匹配配置的前缀，否则直接拒绝** |

### 6.2 批次（用户要的"数组对象"接口）

`POST /api/v1/fleet/batches?dry_run=true|false`

```json
{
  "batch_id": "dx-parallel-20260912-01",
  "cluster": "tencent-2node",
  "max_concurrency": 5,
  "defaults": {
    "model": "qwen3.8-max",
    "llm_tag": "qwen3.8-max@dashscope",
    "duration_seconds": 300,
    "prompt_source": "canonical",
    "repetitions": 1
  },
  "items": [
    {
      "item_id": "i-001",
      "namespace": "otel-demo-03",
      "test_kind": "Dx",
      "autonomy_level": "L0",
      "case": "D7",
      "tool_substitution_variant": "A",
      "harness": "codex",
      "model": "qwen3.8-max",
      "llm_tag": "qwen3.8-max@dashscope",
      "duration_seconds": 300,
      "prompt": null,
      "prompt_source": "canonical",
      "repetition": 1,
      "note": "Dx 轮第 1 次重复"
    }
  ]
}
```

字段说明与校验：

| 字段 | 必填 | 说明与校验 |
|---|---|---|
| `namespace` | 否 | 指定则钉在该副本（必须存在且 Ready）；留空则进队列，由空闲副本领取 |
| `test_kind` | 是 | `Lx` / `Dx` / `Px`，人看的标签。必须与 (`autonomy_level`,`case`) 自洽：`Lx` ⇒ case=C0；`Dx` ⇒ case ∈ D1–D8 且 level=L0；`Px` ⇒ case ∈ {P1,P2} 且 level=L0 |
| `autonomy_level` | 是 | `L0`–`L4` |
| `case` | 是 | `C0`/`D1`…`D8`/`P1`/`P2`，取值以控制器的 `LX_SELECTABLE_CASE_IDS` 为准 |
| `tool_substitution_variant` | D7/D8 必填 | `A`/`B`；其他用例必须留空 |
| `harness` | 是 | `codex` / `claude-code` / `deepseek-harness` / `bladeai` |
| `model`、`llm_tag` | 是（可用 defaults） | `model` 必须是网关别名 |
| `duration_seconds` | 是（可用 defaults） | **默认 300**（§13 决策 2）。这一个值同时决定三件事：披露时长的级别（L0/L1）提示词里"最长持续 N 秒"就是它（`lx.py:329`）；确认门批准注入时长的上限，也是平台自动中止的基准（`lx.py:626`，超出后再 2 分钟未恢复即中止）；本身受控制器全局 20 分钟上限约束（`controller/safety.py:18`，校验在 `lx.py:127-128`）。取 300 正好等于权威 L0 提示词里的秒数，提示词逐字不变 |
| `prompt` | 否 | **允许人工覆盖（§13 决策 3 已批准）**。留空（推荐）时 Fleet 从该副本控制器的 `/autonomy/cases` 取已按副本渲染的原文；填了则 verbatim 使用，`prompt_source` 记 `manual`、落 `prompt_sha256`、并走 §5.7 卫生检查。汇总里必须把 `manual` 的行标出来，不与原文轮混算 |
| `repetition` | 否 | 默认 1；(case, level, harness, model, repetition) 在批次内唯一 |

其他语义：
- **同一个 `batch_id` 重复提交幂等**，返回已有排期，不重复跑。
- `dry_run=true` 只做校验 + 排期，返回"哪一项在哪个副本第几轮跑"，人先看一眼再真跑。
- 每项映射到一个 `LxRunRequest`：`namespace → application`，其余字段同名直传。校验错误要把控制器返回的 422 原文带出来。

### 6.3 运行与观察

| 接口 | 说明 |
|---|---|
| `GET /api/v1/fleet/batches/{id}` | 每项状态：Queued/Assigned/Running/Done/Failed/Invalid、trial id、所在副本、耗时、分数摘要、失败原因码 |
| `GET /api/v1/fleet/batches/{id}/results?format=json\|csv` | 汇总矩阵，列名沿用现有汇总口径 |
| `POST /api/v1/fleet/batches/{id}/stop` | 调各控制器现有 `POST /lx/runs/{id}/stop` |
| `GET /api/v1/fleet/batches/{id}/items/{item_id}/artifacts` | 指向控制器的现有产物接口，不复制存储 |

### 6.4 调度语义

- 每个副本同一时刻只跑一条，直接沿用控制器的单飞与 409，不在 Fleet 里再造一套锁。
- `max_concurrency` 可配且 <= slot 数，用于并发爬坡（5 → 10 → 20）。
- **失败分类**（沿用现有 reason_codes）：平台原因（隧道断、503、上游限流/额度）自动作废并重排，次数上限可配；智能体原因（HARNESS_TIMEOUT、OUTPUT_UNSTRUCTURED 等）记结果不重排。两类必须分开统计，否则并行跑出来的失败率没法解释。
- 试验超时沿用控制器的 30 分钟上限，Fleet 不另设更短的超时。
- **派发要随机化**：同一家智能体不要总落在同一个副本/同一台节点，避免节点差异变成混杂因素。

### 6.5 留给人测试的接口

| 接口 | 用途 |
|---|---|
| `GET /api/v1/fleet/preflight` | 逐副本跑现有 preflight + 环境门 + 网关探测，一次看全 |
| `POST /api/v1/fleet/slots/{id}/trial` | 直投单条到指定副本，等价于今天手工 curl 控制器，用于调试 |
| `GET /api/v1/fleet/slots/{id}/prompt?case=&level=` | 看该副本渲染后的提示词原文（确认命名空间替换对不对） |
| `POST /api/v1/fleet/provision?dry_run=true` | 输出将要 apply 的清单 |
| `scripts/fleet_ctl.py` | 上述接口的 CLI 包装，给不想拼 JSON 的人用 |

---

## 7. 被测副本：裁剪档

**用现有 chart 关组件，不要自己写新系统。** 服务名、标签、指标、源码快照都不变，平台不用写新适配层，将来和旧数据也好对照。

- **必须保留**：`cart`（唯一目标）、`valkey-cart`（cart 的 init 阻塞依赖）、`frontend`（`/api/cart` 路由所在）、`load-generator`（Locust 是业务判定唯一数据源）。
- **大概率要**：`frontend-proxy`（内置 Locust 默认打它，未核实；把 Locust host 指向 frontend 就能省掉，省掉前要实测）。
- **建议保留**：`flagd`（cart/frontend/Locust 都引用）、`opentelemetry-collector`（智能体侧的 Jaeger/Prometheus/Coroot 数据来自它，缺了 C0/D3–D5/D7 的观测会空）。
- **可选**：`product-catalog`（购物车非空时 frontend 会调它，实测后决定）。
- **可关掉**：accounting、ad、checkout、currency、email、fraud-detection、image-provider、kafka、llm、payment、postgresql、product-reviews、quote、recommendation、shipping。没有任何判定读它们。

另外三件事必须做：

1. **Locust 只保留购物车相关任务，统计行名必须精确是 `/api/cart`**。仓库里 `environment/workloads/otel-demo/locustfile.py:65-87` 用的是 `/api/cart [add]` / `[view]`，精确匹配不上，会退回全局聚合——别直接拿它用。关掉其他服务后，打向它们的请求会变成失败行，污染全局聚合，所以任务必须裁到只打购物车。
2. **每个 Pod 设 CPU limit**，并给每个副本命名空间加 `LimitRange` + `ResourceQuota`。现在 `environment/kubernetes/otel-demo/values.yaml` 里 30 处设了内存上限、CPU 基本没设，而实测一次 CPU 故障吃掉过 19–25 个核；20 个副本挤在 2 台节点上，一个副本注入会拖垮同节点其他副本的健康门。
3. **命名与标签规范**：命名空间 `otel-demo-01`…；每个对象带 `benchmark.slot` 和 `benchmark.namespace` 标签，供盘点和清理精确匹配。

附带好处：完整 OTel Demo 里 accounting 大约每 17 分钟 OOM 一次的噪声没了；cart 现在每秒只有 0.1–0.4 个请求，流量集中后按请求数的判定更稳。

---

## 8. 部署（新环境）

- **分支**：从 `1c80e23` 拉 `codex/stage2-replica-fleet-20260912`。
- **镜像**：控制器与 Agent 镜像沿用现有构建法（干净 worktree 构建、`dirty=0`、按 digest 部署）。Fleet 复用控制器镜像，只换启动命令。
- **控制面**：沿用一个控制命名空间 `resiliencebenchmark-system`，每 slot 一套带后缀的 Deployment/Service/PVC/SA（`resbench-stage2-s01` …）。**不要**为每个 slot 开一个控制命名空间。
- **RBAC**：共享一份 ClusterRole，每 slot 一个 ClusterRoleBinding（不要让多个 slot 去改同一个 Binding 的 subjects，会互相覆盖）；每 slot 一个 Role + RoleBinding，建在**自己的**被测命名空间里（模板来自 `deploy/stage2/stage2.yaml:86-142` 和 `deploy/stage2/execution-identities.yaml:63-110`）。
- **PVC**：每 slot 一个 RWO PVC。新环境是 `openebs-hostpath` 本地卷，注意 `WaitForFirstConsumer` 与节点绑定。
- **Chaos Mesh**：D8 要用它作替代执行器。旧集群的配置是 `clusterScoped: false` + `targetNamespace: otel-demo`（`deploy/chaos-mesh/values-old-cluster.yaml:2,17`），这种配置只管一个命名空间。**新环境那套 Chaos Mesh 是别人装的，现状未核实：动手前先查 `clusterScoped` 和 `enableFilterNamespace`，要改作用范围必须先和占用方协调**（集群是共享的，还有 `sregym` 等命名空间在用）。
- **Coroot**：D7 依赖，新环境是定制版（ISobserver），匿名只读由用户开通。
- **观测栈**：都钉在 `vm-0-10-ubuntu`；副本变多后关注 Prometheus 的序列增长。
- **NetworkPolicy 兜底**：仓库里现在没有任何 NetworkPolicy。给每个副本命名空间加一条，只放行本副本内部 + 观测栈 + 自己控制器的流量。智能体容器本身已有出站限制（`deploy/stage2/stage2.yaml:388-393`），这层是双保险。
- **旧的完整 `otel-demo` 保留不缩容**（§13 决策 4），与副本长期共存。因此：① 所有命名空间匹配**必须精确相等**，`otel-demo` 与 `otel-demo-01` 前缀相同，任何 `startswith` 都是 bug；② Fleet 的前缀硬闸要把 `otel-demo` 本身排除在可操作范围外，绝不允许对它执行置备/复位/删除；③ 5 个副本 + 完整 otel-demo + 观测栈的资源要先算一遍再置备（完整 otel-demo 约 20 多个组件，副本每份 6–8 个）。

---

## 9. 安全护栏

1. Fleet 的所有破坏性操作（删命名空间、`--fresh`、卸载）都要：命名空间匹配配置的前缀 + 请求带 `confirm=<命名空间名>`，两者缺一即拒绝。
2. 绝不允许操作不匹配前缀的命名空间，尤其是 `otel-demo`、`observability`、`coroot`、`chaos-mesh`、`kube-system`。
3. `provision` 与 `batches` 默认 `dry_run=true`，真跑必须显式关掉。
4. Fleet 需要建/删命名空间的权限，k8s RBAC 没法按名字前缀限制，所以前缀闸只能在代码里做——必须有单测覆盖"传了别的命名空间会被拒绝"。
5. 每个破坏性动作写审计日志（谁、什么时候、动了哪个命名空间、dry_run 与否）。

---

## 10. 验收标准（五关，每关都要留证据）

**关 0 · 默认零回归**
不设任何新环境变量：现有单测全绿；L0×C0 在 `otel-demo` 上跑通；新增断言测试证明所有提示词文本与 `1c80e23` 逐字一致。

**关 1 · 单副本打通**
建 `otel-demo-01` + 一个控制器实例，跑通 L0×C0（PASS/VALID），提示词里出现 `otel-demo-01`，复位在本命名空间内完成。

**关 2 · 双副本隔离**（最关键）
- A 注入期间，B 的环境门仍然 qualified；
- B 的 `recovery.json` / `fault_effect_evidence` 里不含 A 的数据；
- A 触发整套重装（T3）只影响 A，B 的试验不受影响；
- A、B 的 ChaosBlade CR 互不干扰，清理各自完成。

**关 3 · 智能体侧观测隔离**
A 注入期间，B 的智能体通过 `telemetry_ro` / `coroot_ro` 查 cart **看不到** A 的故障。这一项现在**未核实**，必须实测：如果串，就要给智能体侧的 trace/指标查询加命名空间过滤。若不修，智能体可能把别人的故障当成自己注入成功——正好是这个 benchmark 要测的"虚假成功声明"，会直接毁掉结果。

**关 4 · 批次接口**
一次提交 6 条（3 家 × 2 题），自动分派到 3 个副本跑完；`dry_run` 能看到排期；结果矩阵能导出 CSV；停止接口有效；幂等重复提交不重跑。

**关 5 · 并发爬坡**
首批 5 个副本：2 → 5 逐级加，每级记录失败原因分布；网关 429/额度类失败单独归类；CPU 故障期间同节点其他副本的健康门不失败。扩到 20 个副本时再爬 10 → 20。

---

## 11. 风险与未核实项（实施时逐条确认并记录）

1. **智能体侧观测可能串数据**（关 3）：Jaeger/Coroot 白名单按服务名放行（`runtime_factory.py:292,296`），20 个副本的 `service.name` 全叫 cart。**最高优先级，先测再往下做。**
2. **模型网关是并行的真瓶颈**：`deploy/stage2/litellm/config.yaml` 没配任何 rpm/tpm/并发上限，且 `:114` `num_retries: 0`，`qwen3.8-max` 直连 DashScope。上游一旦 429，平台不重试，直接变成智能体失败被算到智能体头上。建议：查清账号 RPM/TPM，给网关配排队而不是报错，并把限流/额度类失败单列为平台原因。09-10 已经出过额度耗尽中断一轮的事。
3. **Chaos Mesh 作用范围**（§8），共享集群需协调。
4. **裁剪档的三个待实测项**：`frontend-proxy` 能不能省、`flagd` 缺失是降级还是报错、`product-catalog` 是否必需。
5. **裁剪 = 换了环境**：按"每次只动一个变量"的口径，要在裁剪后的系统上重跑 C0 基线，**不要和旧的完整 otel-demo 分数放进同一张表**。
6. **副本数量与资源**：控制器每实例 requests 1.25 核 / 2.5 GiB（`deploy/stage2/stage2.yaml` 两个容器之和），20 个约 25 核 / 50 GiB；新环境两节点共 64 核 / 246 GiB。副本自身开销要实测后再定最终副本数。

---

## 12. 交付物

1. 分支 `codex/stage2-replica-fleet-20260912`，提交按主题拆分（参数化 / 环境门 / 提示词渲染 / 清单模板 / 裁剪档 / Fleet 服务 / 测试）。
2. 改动文档：每处改动记 `文件:行`、改前/改后、原因、对应测试、部署情况。
3. 测试：默认路径逐字一致的断言测试、前缀闸拒绝测试、批次校验测试、提示词渲染测试、双副本 E2E 的执行记录。
4. 部署记录：镜像 digest、每 slot 的对象清单、关 0–关 5 的证据位置。
5. 把本方案复制到分支的 `docs/design/` 下，随代码一起走。

---

## 13. 决策记录（用户 2026-09-12 已定）

| # | 事项 | 决定 | 对实施的影响 |
|---|---|---|---|
| 1 | 首批副本数 | **5 个**（`otel-demo-01` … `otel-demo-05`） | `fleet/config` 的 `replicas` 首批填 5；关 5 的爬坡改成 2 → 5；模板必须能无改动扩到 20，不要把 5 写死在任何地方 |
| 2 | `duration_seconds` 默认值 | **300 秒** | 作为批次 `defaults.duration_seconds`。与权威 L0 提示词里的"最长持续 300 秒"一致，提示词逐字不变；同时是确认门批准上限与自动中止基准；全局上限 20 分钟不变 |
| 3 | 允许批次里人工覆盖 `prompt` | **允许** | 保留 `prompt` 字段与 `prompt_source=manual` 标记、`prompt_sha256`、§5.7 卫生检查；汇总与导出必须能按 `prompt_source` 过滤，人工覆盖的不与原文轮混算 |
| 4 | 旧的完整 `otel-demo` | **先保留，不缩容** | 见 §8 对应条目：精确相等匹配、前缀硬闸排除 `otel-demo`、置备前先算资源 |

实施过程中如果发现这四条中的任何一条与代码现状冲突，先停下问用户，不要自行改口径。
