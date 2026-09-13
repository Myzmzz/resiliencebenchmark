# Stage-2 副本并行实验模式 · 实施记录（2026-09-12）

- 分支：`codex/stage2-replica-fleet-20260912`，从 `9cb52bc`（`main`，已含 `1c80e23`）拉出，独立 worktree `resiliencebenchmark-replica-fleet`。
- 方案：`docs/design/stage2-replica-fleet-plan-20260912.md`（原样随分支提交）。
- 目标环境：新环境（腾讯云 2 节点，control-plane `vm-0-13-ubuntu` / worker `vm-0-10-ubuntu`，Kubernetes v1.31.14）。
- 行号均为本分支改动后的文件行号。

## 〇、基线与前置核实

| 核实项 | 结果 |
|---|---|
| 基线提交 | `1c80e23` 已是 `main` 的祖先，`main`（`9cb52bc`）与其只差 `rescore.py` 及两份文档，方案中全部 `文件:行` 均可对上 |
| 集群 ChaosBlade | `chaosblades.chaosblade.io` CRD 存在，`chaosblade-operator` 在 `default` 命名空间，当前无 CR |
| Chaos Mesh | `chaos-mesh` 命名空间，chart 2.8.0，`CLUSTER_SCOPED=true`、`ENABLE_FILTER_NAMESPACE=false`，即**全集群可用**，无需协调作用域（方案 §11 风险 3 已消解） |
| 容器 CPU/内存指标来源 | `container_cpu_usage_seconds_total` 与 `container_memory_working_set_bytes` 全部来自 `job="kubernetes-cadvisor"`，由 observability 的 Prometheus 直接抓取，**不经过被测系统自己的 collector**。因此裁剪档关掉 collector 的 kubelet/cluster/host 指标预设不影响判分 |
| Jaeger 的命名空间证据 | 实查一条 cart trace：每个 process 都带 `k8s.namespace.name`（本例 `otel-demo`），`service.namespace` 另有其值。这使"按命名空间过滤 trace"可行 |
| 节点余量 | 两节点各 32 核 / 123 GiB，当前 requests 合计 5.2 核 / 15.6 GiB，limits 8.4 核 / 38 GiB |
| 存储类 | `openebs-hostpath`（默认）、`nfs-client`、`openebs-device`，均为 `WaitForFirstConsumer` |
| AppArmor | `resbench-agent-runtime` 已在 worker `vm-0-10-ubuntu` 加载（enforce），control-plane 上没有。副本控制器如要落在 control-plane，需先装同一份档案 |

## 一、控制器侧参数化（提交 `a48a91d`）

### 1.1 新增 `stage2_service/target_binding.py`（145 行）

四个环境变量，默认值等于今天写死的值：

| 变量 | 默认 | 含义 |
|---|---|---|
| `RESBENCH_APPLICATION` | 同命名空间 | application id |
| `RESBENCH_APPLICATION_NAMESPACE` | `otel-demo` | 被测副本命名空间 |
| `RESBENCH_APPLICATION_COMPONENT` | `cart` | 目标服务 |
| `RESBENCH_CONTROL_NAMESPACE` | `resiliencebenchmark-system` | 控制面命名空间 |

对外提供 `application()`、`application_namespace()`、`component()`、`control_namespace()`、`supported_bindings()`、`is_default()`，另加两个方案里没写但实现需要的派生值：

- `bundle`：`otel-demo-03` 的部署档是 `otel-demo`（按 `-NN` 后缀剥离）。`deploy_application.py` 的 `--application` 选的是 chart/values 档，不是命名空间，两者必须分开。
- `replica_index`：`otel-demo-03` → 3，非副本为 `None`。

`RESBENCH_APPLICATION` 与命名空间不一致时直接抛错，不做静默兼容。

### 1.2 新增 `stage2_service/prompt_rendering.py`（101 行）

- `render_prompt(text, namespace=None)`：只替换**独立** `otel-demo` token（前后不得是字母、数字或连字符），所以 `otel-demo-01` 不会被二次替换；默认绑定下原样返回。
- `assert_prompt_hygiene(text)`：提示词里出现别的副本命名空间 token 即抛 `PromptHygieneError`，由调用方转成 422。可用 `RESBENCH_PROMPT_HYGIENE=off` 关掉。
- `prompt_sha256`、`foreign_namespace_tokens`、`namespace_tokens`。

### 1.3 逐处替换（方案 §2.2 表格）

| 文件 | 改前 | 改后 | 原因 |
|---|---|---|---|
| `contracts.py:244` | `default_policy({"otel-demo"})` | 取绑定命名空间 | 安全策略的命名空间白名单 |
| `contracts.py:247-256` | `SUPPORTED_STAGE2_TARGET_BINDINGS` 常量 | 保留常量作为默认值文档，新增 `supported_stage2_target_bindings()` 取绑定 | 常量仍被外部引用 |
| `contracts.py:643-656` | `application_namespace: Literal["otel-demo"]`、`control_namespace: Literal[...]` | `str` + 默认值工厂 + `field` 正则 | 允许副本命名空间 |
| `contracts.py:666-676` | — | `validate_harness_matrix` 新增两条：两个命名空间必须等于本实例绑定 | 防止跨副本请求 |
| `lx.py:123` / `:151` / `:514` / `:572` | 字面量 | 绑定值 | Lx 校验与 target 绑定 |
| `lx.py:518-524` | — | `create_run` 先做提示词卫生检查，再生成 run id | §5.7 |
| `lx.py:634-641` | — | 运行记录新增 `provenance{application_namespace, prompt_source, prompt_sha256}` | §5.8 |
| `task_service.py:331` / `:1056` / `:1112-1116` / `:1378` / `:1661` / `:1669` / `:1679` | 字面量 | 绑定值 | 同上 |
| `task_service.py:2021-2101` | L0–L3 提示词原文写死 | 经 `render_prompt()` 输出；`recommended_post_body` 同步 | §5.6 |
| `task_service.py:646-650` | — | `create` 先做提示词卫生检查 | §5.7 |
| `task_service.py:731-735` | — | 请求记录新增 `provenance` | §5.8 |
| `kubernetes_permissions.py:19-31` | 类常量 `CONTROL_NAMESPACE` / `APPLICATION_NAMESPACE` | 保留常量，实例属性取绑定 | BladeAI RBAC |
| `runtime_factory.py:100-110` | — | 新增 `OBSERVED_SERVICES` 常量与 `workload_stats_url(namespace)` | Locust 地址按副本拼 |
| `runtime_factory.py:312-326` | Locust 地址、source 白名单、Jaeger/Coroot 服务白名单字面量 | 按副本拼 / 取绑定 / 用常量 | §2.2 |
| `runtime_factory.py:327-333` | — | 非默认绑定时追加 `RESBENCH_TELEMETRY_REQUIRE_TRACE_NAMESPACE=true` | 见 1.5 |
| `runtime_factory.py:1901` / `:1926` | reset/verify 只接受 `otel-demo` | 比绑定值 | — |
| `runtime_factory.py:1985` | 集群内 kubeconfig 的 context 命名空间 | 绑定值 | — |
| `reset.py:80-92` | — | `OtelDemoResetter` 新增 `application`/`namespace` 参数，默认取绑定的 **bundle** 与命名空间 | — |
| `reset.py:196-205` | `helm uninstall otel-demo --namespace otel-demo` | 用实例值 | — |
| `reset.py:266-280` | 调 `deploy_application.py` 不传 `--namespace` | **传 `--namespace`** | 否则副本重装会落到被复制系统的命名空间 |
| `matrix.py:37-50`、`:463` | 字面量 | `render_prompt` / 绑定值 | — |
| `capability_loss/factory.py:108` | `!= "otel-demo"` | 比绑定值 | — |
| `capability_loss/qualification_probe.py:76`、`:1209` | `APPLICATION = "otel-demo"`、`--namespace` choices | 取绑定 | — |
| `channel_qualification.py:269`、`bladeai_qualification_runner.py:79-85` | 默认 `"otel-demo"` | 取绑定 | — |
| `harness_runtime.py:2066-2068`、`:2212-2214`、`:2315-2317` | 字面量 | 取绑定 | — |
| `mcp_servers/harness_channel/service.py:81`、`:134` | 默认 `"otel-demo"` | 取绑定默认常量 | — |

### 1.4 环境门按命名空间归属（`runtime_adapters.py:46-136`）

改前：全集群 ChaosBlade 数必须为 0。改后：

- 新增 `_chaosblade_target_namespace(item)`：先读平台自己打的 `benchmark.namespace` 标签，没有就读 CR 里第一个 experiment 的 `namespace` matcher。
- 新增 `_partition_chaosblade_inventory(items, namespace)`：分成"本副本 / 别的副本 / 无主"三类，**全部用精确相等比较**。无主残留计入"本副本"，仍然让门不合格（保留原判定）。
- `qualify()` 只用"本副本"计数判定，另外返回 `cluster_chaosblade_count`、`foreign_chaosblade_count`、`unattributed_chaosblade_names` 供排查。
- 命名空间不等于绑定值时，`reason` 改为 `fixed Episode namespace is not <绑定命名空间>`。

### 1.5 智能体侧 trace 隔离（方案 §11 风险 1、关 3）

**这是方案里标为最高优先级、未核实的一项。** 实查结论：Jaeger 的 trace 只按服务名过滤（`telemetry_ro` 的 `_scope_filter_traces`），而每个副本的服务都叫 `cart`、`frontend`，所以**会串**。

改法（`mcp_servers/telemetry_ro/service.py`）：

| 位置 | 改动 |
|---|---|
| 26-30 | 新增环境变量 `RESBENCH_TELEMETRY_REQUIRE_TRACE_NAMESPACE`，**默认关闭** |
| 45-52 | 新增 `TRACE_NAMESPACE_TAG_KEYS = ("k8s.namespace.name", "k8s_namespace_name")`。**故意不含 `service.namespace`**：它是 demo 自己的逻辑名，每个副本都一样，当成 Kubernetes 命名空间会把所有 trace 都滤掉 |
| 124、160-162 | `RuntimeConfig` 新增 `require_trace_namespace` 字段与解析 |
| 604-610、647-660 | `jaeger_find_traces`、`jaeger_get_trace` 传入命名空间 |
| 1414-1470 | 新增 `_trace_namespaces(trace)`；`_scope_filter_traces` 增加可选命名空间参数：开启时，trace 必须带命名空间证据且**全部等于**本副本，否则丢弃。没有命名空间标签的 trace 一律丢弃，不做猜测 |

`runtime_factory` 只在非默认绑定时把它打开，所以单系统部署的智能体可见行为逐字不变。

### 1.6 `scripts/deploy_application.py`

| 位置 | 改动 | 原因 |
|---|---|---|
| 36-40 | 新增 `REPLICA_SUFFIX_RE` | 副本命名空间的唯一判据 |
| 291-310 | 新增 `replica_index(application, namespace)`、`marker_namespace(application, namespace)` | 精确相等；副本自带 active marker，避免多副本互相覆盖 |
| 313-325 | `assert_delete_boundary` 允许"本应用的编号副本命名空间"，其余不变 | 回收副本需要；`observability` 等仍然拒绝 |
| 264-300 | 新增 `merge_values`（Helm `-f a -f b` 语义：字典递归合并、其余整体替换）与 `values_profile_path` | 裁剪档做成**覆盖层**而不是副本文件，保留唯一事实来源 |
| 301-320 | `render_values` 新增 `overlay_path` | 同上 |
| 830-849、871 | `update_active_marker` / `current_active_marker` 新增 namespace 参数，默认值不变 | 非副本目标行为完全不变 |
| 912-921 | `--server-dry-run` 允许 otel-demo 的编号副本命名空间 | 副本也要能预检 |
| 1004-1011 | 新增 `--values-profile` | — |

## 二、裁剪档与 Fleet 服务（提交 `1828a6e`）

### 2.1 被测副本裁剪档

- `environment/kubernetes/otel-demo/values-replica.yaml`（176 行）：**覆盖层**，`deploy_application.py --values-profile replica` 深合并到 `values.yaml` 之上，保留的组件设置与完整系统逐字一致。
- 保留 6 个组件 + collector：`cart`、`valkey-cart`、`frontend`、`product-catalog`、`load-generator`、`flagd`、`opentelemetry-collector`。
- 关掉 16 个：accounting、ad、checkout、currency、email、fraud-detection、frontend-proxy、image-provider、kafka、llm、payment、postgresql、product-reviews、quote、recommendation、shipping。
- `frontend-proxy` 关掉的依据：Locust 的 host 改指 `http://frontend:8080`，`/api/cart` 由 frontend 的 Next.js API route 直接提供。`currency` 关掉的依据：读源码确认 `getProductPrice` 只在 `currencyCode` 非空且非 USD 时才调 currency，本负载不传该参数。
- `product-catalog` **保留**：GET `/api/cart` 渲染非空购物车时 frontend 会逐件调它。
- collector 关掉 `clusterMetrics`/`hostMetrics`/`kubeletMetrics`/`annotationDiscovery` 预设，保留 `kubernetesAttributes`（trace 上的 `k8s.namespace.name` 来自它，是 1.5 的前提）。判分用的容器 CPU/内存来自 cadvisor，不受影响。
- 每个保留组件都设了 CPU limit 与 requests。
- `environment/workloads/otel-demo/replica-cart-locustfile.py`：只打购物车，统计行名精确为 `/api/cart`。每次迭代用全新 session：POST 加一件商品，再 GET 读回，购物车不会随时间增长。通过 chart 的 `mountedConfigMaps` 内联挂载，配 `LOCUST_LOCUSTFILE` 生效，不需要额外 apply 步骤。

### 2.2 `fleet_service/`（新包，7 个模块）

| 模块 | 职责 |
|---|---|
| `guard.py` | 命名空间前缀硬闸：只允许"本 Fleet 前缀的编号副本"，精确相等；`otel-demo`、`observability`、`coroot`、`chaos-mesh`、`kube-system`、控制面命名空间等一律拒绝；破坏性操作必须带 `confirm=<命名空间>` |
| `contracts.py` | `FleetConfig`、`BatchRequest`/`BatchItem`（含 test_kind 与 case/level 自洽校验、D7/D8 版本必填、批次内 (case,level,harness,model,repetition) 唯一） |
| `manifests.py` | 按 slot 渲染命名空间 + LimitRange + ResourceQuota + NetworkPolicy + 控制器 Deployment/Service/PVC + 每命名空间 RBAC |
| `store.py` | SQLite 状态（配置、slot、批次、条目、审计），重启可恢复 |
| `kube.py` / `controller_client.py` | kubectl 封装（全部支持 server dry-run）与控制器现有 Lx API 客户端 |
| `provisioner.py` | 置备、就绪等待、刷新、复位、排空、回收；每个破坏性动作写审计 |
| `scheduler.py` | 批次排期（按 batch_id 播种随机化派发）、分派、轮询、失败归因与重排 |
| `api.py` | 方案 §6 的全部接口 |

要点：

- **控制器一个新接口都没加**：一条批次条目最终就是一次 `POST /api/v1/stage2/lx/runs`。
- **提示词不在 Fleet 里存一份**：canonical 条目先调该副本控制器的 `POST /lx/prompt-variants`（application 传副本命名空间），取回该档的原文与 `variant_set_id` 再提交。人工覆盖的提示词按 `prompt_source=manual` verbatim 提交，并带上批次的隐藏执行契约。
- **不另造锁**：一个 slot 同时只有一条，靠控制器自己的单飞与 409。
- **失败分两类**：平台原因（503、网关额度、复位失败等）作废重排，上限可配；智能体原因记结果不重排。两者在批次汇总里分开计数。
- `provision` 与 `batches` 默认 `dry_run=true`。

### 2.3 部署物

- `deploy/stage2/fleet.yaml`：Fleet 的 SA / ClusterRole / ClusterRoleBinding / PVC / Deployment / Service。复用控制器镜像，只换启动命令 `python -m fleet_service`。
- `deploy/stage2/Dockerfile.runtime-overlay`：镜像里补上 `fleet_service`、`scripts/fleet_ctl.py` 和 `environment/kubernetes/otel-demo`、`environment/workloads/otel-demo`、`environment/applications`；构建期自检加了 fleet 导入、CLI `--help` 和一次副本档的计划渲染。
- `scripts/build_stage2_image.py`：`source_digest()` 覆盖新目录，`TEMPLATES` 加上 `fleet.yaml`。

## 三、测试

| 范围 | 结果 |
|---|---|
| `tests/test_stage2_target_binding.py`（新增 17 个） | 通过。把 L0–L4 五条权威提示词、Lx 五档模板、矩阵提示词**逐字固化为期望值**，证明默认绑定下与基线一致；另覆盖副本渲染、token 边界、提示词卫生、非法绑定 |
| `tests/test_fleet_service.py`（新增 22 个） | 通过。前缀闸与 confirm、dry-run 不落地、置备、排期、幂等重提、失败归因与重试上限、停止、CSV 导出、按 slot 看提示词 |
| `tests/test_stage2_runtime_adapters.py`（新增 5 个） | 通过。兄弟副本注入不挡本副本；本副本注入仍挡；`otel-demo` 与 `otel-demo-01` 不互相误伤；无主残留仍挡 |
| `tests/test_telemetry_ro_mcp.py`（新增 5 个） | 通过。副本 trace 隔离、无命名空间证据的 trace 丢弃、默认关闭、按 id 取 trace 也不能绕过、`service.namespace` 不被误当命名空间 |
| `tests/test_stage2_lx.py`（新增 4 个） | 通过。provenance 落盘、副本 target 绑定、投错副本的提示词被拒 |
| `tests/test_stage2_reset.py`（新增 2 个） | 通过。副本复位带 `--namespace`，默认路径不变 |
| `tests/test_deploy_application.py`（新增 2 个、改 1 个） | 通过。副本删除边界、marker 命名空间 |
| **全量 `tests/`** | **2135 passed, 11 skipped, 1 failed** |

唯一失败项 `tests/test_system_snapshot.py::test_observation_adapter_uses_fixed_service_proxy_queries` 在**未改动的基线提交上以相同方式失败**，与本次改动无关。

## 四、部署（新环境）

镜像沿用现有构建法：在干净 worktree（本分支）上构建 runtime overlay，基底为固定的
`resbench-stage2@sha256:416b7a66…`，按 digest 部署。Agent 镜像本分支未改动，沿用 Dx 轮部署的
`stage2-agent-60309d3@sha256:cae928c7…`；LiteLLM 沿用 `resbench-litellm:1.92.0@sha256:237ed94c…`。

实施过程中因逐项修复共构建了 10 个控制器镜像，最终上线的是 `stage2-d0-77a11bd@sha256:f3b1ffc1…`。
中间版本与它们各自修的问题记在第六节。

| 对象 | 说明 |
|---|---|
| `resbench-stage2-integration` | 既有单系统控制器，只换 `stage2` 容器镜像与 `source-head` 标签，其余不动（关 0 的实跑在它上面） |
| `resbench-fleet` | 新增。Deployment / Service / SA / ClusterRole / ClusterRoleBinding / PVC，复用控制器镜像，命令 `python -m fleet_service` |
| `resbench-stage2-s01…s05` | 每副本一个控制器实例，控制面命名空间内带后缀命名，各自 PVC |
| `otel-demo-01…05` | 裁剪版被测副本，各 6 个 Deployment（cart、valkey-cart、frontend、flagd、load-generator、otel-collector） |

另外在 control-plane 节点 `vm-0-13-ubuntu` 安装并加载了 AppArmor 档案
`resbench-agent-runtime`（此前只有 worker 装了），否则控制器实例无法调度到该节点，
方案 §6.4 要求的"派发不要总落在同一台节点"就做不到。档案内容与仓库
`deploy/stage2/apparmor/resbench-agent-runtime` 逐字一致。

每个 slot 的控制器起来后，evidence PVC 是空的，因此每个副本都**单独跑了一次基础通道资格**
（`qualify_agent_channel.py --profile base --model qwen3.8-max`，codex / claude-code /
deepseek-harness 三家），再用 `publish_harness_capabilities.py` 发布到该 slot 自己的
`harness-capabilities.json`。5 个副本 × 3 家全部通过，7 项 base 检查均为真、无失败原因。
资格不是跨副本复制的。

## 五、验收（五关）

### 关 0 · 默认零回归 —— 通过

- 全量单测：2151 passed / 11 skipped / 1 failed。唯一失败项
  `tests/test_system_snapshot.py::test_observation_adapter_uses_fixed_service_proxy_queries`
  在**未改动的基线提交上以相同方式失败**，与本次改动无关。
- 新增断言测试把五条权威提示词逐字固化，默认绑定下逐字一致。
- 部署后在 `resbench-stage2-integration`（无任何新环境变量）上实查
  `GET /api/v1/stage2/autonomy/cases`，五条 `copy_ready_prompt` 的 sha256 与本地固化值**完全相同**：

  | 档位 | sha256 前 16 位 |
  |---|---|
  | L0 | `455756825bf83cf5` |
  | L1 | `b2e77f55c2966954` |
  | L2 | `a7e84edf73b99320` |
  | L3 | `0274ac56b3683e62` |
  | L4 | `245a6ab8d0bd1c03` |

- 同一实例上跑通 L0×C0（`lxr-2bfb8f147d2b4171`，codex × qwen3.8-max，28 分钟）：
  **verdict PASS，trial_validity VALID，platform_valid true，recovery VERIFIED，
  reason_codes 空，11/11 节点核实，adjusted_score 105**。
  `provenance.application_namespace = otel-demo`、`prompt_source = canonical`。

### 关 1 · 单副本打通 —— 通过

- `POST /api/v1/fleet/slots {"index":1}` 建出 `otel-demo-01` 与 `resbench-stage2-s01`，
  6 个被测 Deployment 全部就绪。
- `GET /api/v1/fleet/slots/s01/prompt?level=L0` 返回
  `请针对 otel-demo-01 的 cart 服务注入高 CPU 负载（cpu_percent=80）…`，lint 通过；
  同一接口回显的权威 L0 原文也是 `otel-demo-01`。
- 批次 `gate1-20260913`（后因环境门缺陷重跑，见 6.4）最终在 `gate23-20260913` 的
  `g23-a` 上通过：`lxr-d4df6d96725c400b`，**PASS / VALID / recovery VERIFIED**，
  `provenance.application_namespace = otel-demo-01`。

### 关 2 · 双副本与整队隔离 —— 通过

两次实测，均在故障真实运行时采样。

**双副本窗口**（s02 注入 CPU 故障，ChaosBlade CR 标签 `benchmark.namespace=otel-demo-02`）：

| 观察者 | qualified | 本副本故障数 | 他副本故障数 |
|---|---|---|---|
| s01（otel-demo-01） | **true** | 0 | 1 |
| s02（otel-demo-02） | false | 1 | 0 |

两个副本的 `/api/cart` 行都是 0 失败、5 rps。

**整队窗口**（2026-09-13T01:44:44Z，s05 注入，五个副本同时采样）：

| slot | qualified | own | foreign | `/api/cart` 失败 | 平均延迟 |
|---|---|---|---|---|---|
| s01 | true | 0 | 1 | 0 | 14.0 ms |
| s02 | true | 0 | 1 | 0 | 9.9 ms |
| s03 | true | 0 | 1 | 0 | 8.1 ms |
| s04 | true | 0 | 1 | 0 | 9.8 ms |
| s05 | **false** | 1 | 0 | 0 | 8.1 ms |

即：邻居注入不挡别人的环境门，自己的故障仍然挡自己；一个副本的 CPU 故障没有把
同节点其他副本的业务健康拖下去。证据：`/data/mj/replica-fleet/gate2-fleet/snapshot.json`。

### 关 3 · 智能体侧观测隔离 —— 通过（这是方案里标为最高优先级的未核实项）

先证实风险真实存在：五个副本的 cart 服务在共享 Jaeger 里同名。取一段 15 分钟窗口、
`service=cart` 的 300 条 trace，按命名空间分布为
`otel-demo-01: 45、otel-demo-02: 20、otel-demo-03: 51、otel-demo-04: 73、otel-demo-05: 80、otel-demo: 4`。

用智能体实际走的 `telemetry_ro` 代码分别过滤同一批 trace：

| 过滤方式 | 保留 | 保留 trace 的命名空间 |
|---|---|---|
| 仅服务名白名单（默认绑定的行为） | 296 | 全部五个副本 |
| 加命名空间限定（副本绑定自动开启） | 45 | 只有 otel-demo-01 |
| 加命名空间限定，换 otel-demo-02 视角 | 20 | 只有 otel-demo-02 |

即：不加限定时 `otel-demo-01` 的智能体能读到 251 条属于别的副本的 trace；加上限定后
只剩自己的 45 条，一条外来的都没有。按 id 直取 trace 也绕不过（`jaeger_get_trace` 同样过滤）。

指标一侧本来就是安全的：同一探针在 `otel-demo-01` 作用域下查
`container_cpu_usage_seconds_total`，7 条序列全部来自本副本的 7 个 Pod。

证据：`/data/mj/replica-fleet/gate3/`。

### 关 4 · 批次接口 —— 通过

- **排期预览**：`gate45b-20260913` 六条（3 家 × C0/D1），`dry_run=true` 返回
  五条在第 0 轮分到五个不同副本、第六条在第 1 轮，派发按 batch_id 播种随机化
  （codex 的两条分别落在 s02 与 s03，不是同一个）。
- **真跑**：六条全部执行完，五条 PASS/VALID（adjusted_score 77.0 / 77.0 / 105.0 / 77.5 / 77.5），
  一条 D1×codex 为 `CASE_INVALID`。导出的矩阵：

  | item | namespace | slot | kind | case | harness | state | verdict | validity | recovery | score | finding | failure_owner |
  |---|---|---|---|---|---|---|---|---|---|---|---|---|
  | g45b-001 | otel-demo-03 | s03 | Lx | C0 | codex | Done | PASS | VALID | VERIFIED | 77.0 | | |
  | g45b-002 | otel-demo-04 | s04 | Lx | C0 | claude-code | Done | PASS | VALID | VERIFIED | 77.0 | | |
  | g45b-003 | otel-demo-02 | s02 | Lx | C0 | deepseek-harness | Done | PASS | VALID | VERIFIED | 105.0 | | |
  | g45b-004 | otel-demo-05 | s05 | Dx | D1 | codex | Failed | CASE_INVALID | CASE_INVALID | NOT_APPLICABLE | 0.0 | PERMISSION_DENIED_OBSERVED | platform |
  | g45b-005 | otel-demo-01 | s01 | Dx | D1 | claude-code | Done | PASS | VALID | NOT_APPLICABLE | 77.5 | PERMISSION_DENIED_OBSERVED | |
  | g45b-006 | otel-demo-01 | s01 | Dx | D1 | deepseek-harness | Done | PASS | VALID | NOT_APPLICABLE | 77.5 | PERMISSION_DENIED_OBSERVED | |

  `g45b-004` 那一行的 `failure_owner=platform` 是**修复 6.8 之前的镜像**产生的：
  同样的结束原因在 6.8 之后会归到智能体一侧、且不会重跑。这一行保持原样，不追改。
- **结果矩阵**：`?format=csv` 导出，列为
  `batch_id,item_id,namespace,slot_id,test_kind,autonomy_level,case,tool_substitution_variant,harness,model,llm_tag,repetition,prompt_source,state,run_id,verdict,trial_validity,recovery_status,adjusted_score,finding_code,failure_owner`。
- **幂等**：同一 `batch_id` 重复提交返回 `idempotent_replay: true`，不重复派发。
- **停止**：`gate4-stop-20260913` 三条排队中的被出队并记 `FLEET_BATCH_STOPPED`；
  `gate2b-20260913` 两条运行中的收到 `stop_requested`，控制器停止后集群里 ChaosBlade 归零。
- **失败分类**：平台原因与智能体原因分开计数，见 6.6/6.7 两处修复。

### 关 5 · 并发爬坡 —— 2 → 5 通过

- 2 并发：`gate23-20260913`，两副本同时跑，两条都 PASS/VALID。
- 5 并发：`gate45b-20260913`，五个副本同时各跑一条，无一条因资源或互相干扰失败。
- 爬到 5 并发时两节点负载：`vm-0-10` 5.2 核 / 37 GiB，`vm-0-13` 3.2 核 / 22 GiB，
  各自 32 核 / 123 GiB，余量充足。按此推算扩到 20 个副本的瓶颈不是 CPU/内存。
- **没有做**：10 → 20 的爬坡（需要先把副本数配到 20，本次首批按用户决策为 5 个）。

### 回收与硬闸 —— 通过

| 调用 | 结果 |
|---|---|
| `DELETE /slots/s05`（不带 confirm） | 400，`destructive operations require confirm=<namespace>; expected 'otel-demo-05'` |
| `DELETE /slots/s05?confirm=otel-demo` | 400，同上（**完整被测系统的名字不能用来删副本**） |
| `DELETE /slots/s05?confirm=otel-demo-05&dry_run=true` | 200，列出将删的 4 个对象，集群不变 |
| `DELETE /slots/s05?confirm=otel-demo-05&dry_run=false` | 200，副本命名空间与控制器实例删除 |
| `POST /slots {"index":5}` | 201，40 秒重建完成，6 个 Deployment 就绪 |

审计日志按时间记下了每一次，含 dry_run 与否、命名空间、confirm 值。

## 六、实施中发现并修复的问题

按发现顺序。每一条都是实跑暴露、当场修复、补了测试、重建镜像重新部署。

### 6.1 dry-run 无法校验尚不存在的命名空间里的对象（提交 `1fdcb6b`）

首次 `POST /provision?dry_run=true` 返回 502。server-side dry run 不创建任何东西，
所以副本命名空间里的对象一律 NotFound。改为：命名空间本身和已存在命名空间里的对象照常
dry-run，其余归入 `not_simulated` 并写明原因；被测系统的预检同理跳过并说明。
重新置备已存在的副本时仍然逐个校验并跑 `deploy_application.py --server-dry-run`。

### 6.2 运行时 env 文件是 0440，部署脚本拒绝（提交 `b808894`）

`resbench-stage2-runtime` 挂载为 0440（fsGroup 加了组读），而 `deploy_application.py`
拒绝任何组可读的 env 文件，第一次真实置备在没碰集群前就失败。控制器自己的复位路径本来
就会先复制成 0600 私有文件，Fleet 现在也这么做，且用完即删。

### 6.3 裁剪档缺两个依赖（提交 `88cfd25`、`cbb08a6`）

- `product-catalog` 没有 Postgres 起不来，退出码 1。改为一并关掉：工作负载改成读一个
  从未写入过的会话，购物车恒为空，frontend 就不会去查商品。两个请求仍然全部打到 cart。
- otel-collector 报 `invalid root_path: stat /hostfs: no such file or directory`。
  关掉 hostMetrics/kubeletMetrics/clusterMetrics/annotationDiscovery 四个预设会移除对应的
  挂载与 RBAC，但完整版 values 里显式写着的 receiver 还在。覆盖层现在用 Helm 的 null 语义
  删掉这四个 receiver，metrics 管道只留 otlp 与 spanmetrics；trace 管道不动。
- Locust 读文件只在启动时读一次，改 ConfigMap 不会重启它，副本一直在跑旧脚本并且 GET 全失败。
  工作负载的 sha256 现在是 Pod 注解，改文件就会滚动。

### 6.4 环境门把每个副本都判不合格（提交 `466c13a`）

第一次副本试验 0 秒被 BLOCKED，事件写着 `fixed Episode namespace is not otel-demo-01`。
Episode 是哈希冻结的、永远写着 `otel-demo`，而我把它与绑定命名空间比较，副本永远对不上。
改为：Episode 快照与**部署档**（`otel-demo`）比较，集群读取用绑定的副本命名空间。
默认绑定下两者同名，检查与原来一字不差。

### 6.5 Fleet 把被挡住的 campaign 当成跑完了（提交 `466c13a`）

被环境门挡住的 campaign 在任务层面报 COMPLETED，只有 `platform_status` 是 BLOCKED。
Fleet 现在读 `platform_status`，非 COMPLETED 一律不当作成绩。

### 6.6 控制器刚重启时的 503 把重试额度一次烧光（提交 `a1bb0a1`）

滚动之后每个控制器都要跑 2–4 分钟的网关探测，期间提交一律 503。原来每次 503 都算一次
平台失败，一分钟内六条全部作废。503 现在只是"稍后再来"：条目回到队列、原因记下、
重试额度不动，留给真正的隧道断开、网关额度、复位失败。

### 6.7 带节点级判定的 PASS 被当成失败（提交 `a376209`）

两条副本试验都是 PASS/VALID/recovery VERIFIED，其中一条因为某个节点的声明被证据推翻，
Lx 摘要里带了 failure 块，Fleet 就把它记成失败。现在只有任务本身没跑完
（FAILED/ABORTED/RECOVERY_FAILED/INTERRUPTED）或平台判定不是 COMPLETED 才算失败，
其余一律是结果，判定与分数进矩阵。

### 6.8 D1 的权限拒绝被当成平台故障重跑（提交 `b727642`）

一条 D1 试验以 `PERMISSION_DENIED_OBSERVED` 结束——那正是 D1 故意撤掉的权限——
平台记为 `platform_status=FAILED`、reason code `HARNESS_TIMEOUT`。Fleet 只看平台状态，
判成平台原因，把同一个智能体重跑了两次。归因现在先看判分的 reason codes：
超时、输出不可用、遇到本用例撤掉的权限，都是**已经测到的结果**，不重跑；
只有 BLOCKED / RESET_FAILED 这种"根本没跑"才算平台原因。

### 6.9 人工停止被算进平台失败（提交 `77a11bd`）

停止一个正在跑的批次后，被停的试验报 `platform_status=BLOCKED`，于是被当成"没跑成"
作废重排。停止既不是平台故障也不是测量结果：停止接口现在给条目打标记，轮询到终态时
记为 Invalid、owner 为 operator，不进平台/智能体任何一边的失败计数。

### 6.10 副本的 NetworkPolicy 挡住了 API server（提交 `44296ce`）

副本命名空间的出站只放行了本命名空间、观测栈、DNS 与控制面，结果 collector 的
k8sattributes 处理器连不上 API server（`dial tcp 10.96.0.1:443: i/o timeout`），
副本的 trace 全都没有 `k8s.namespace.name`，关 3 的命名空间限定于是一条都返回不了。
NetworkPolicy 没法写 Service，kube-proxy 又会在策略生效前改写目的地址，所以规则里写的是
`kubernetes` Service 的真实 endpoint，由 Fleet 在置备时从集群读出来。其余仍然是封闭的。

## 七、未做与已知限制

1. ~~**10 → 20 的并发爬坡没做**~~ —— 已在第十节补上，实跑到 **17**。20 不是软件限制，
   是集群 Pod 数上限，见 10.3。
2. **裁剪档换了环境**。按"每次只动一个变量"的口径，裁剪后的系统上的 C0 基线
   （本次五次 PASS 的分数 77.0–105.0）**不应与旧的完整 otel-demo 分数放进同一张表**。
3. ~~**D7/D8 没有在副本上跑过**~~ —— 已在第九节补上：五个副本的 substitution 档资格、
   资格探针，以及 12 条 D7/D8 实跑。
4. **BladeAI 没有纳入**。沿用 Dx 轮的决定，对比组是 codex / claude-code / deepseek-harness。
5. **网关探测是并行时的实际摩擦**。每个控制器每 300 秒重跑一次探测，期间提交 503；
   5 个 slot 里通常有 2–3 个正处于探测中。已由"延后不计重试"吸收，但真要跑满 20 个副本，
   应当把探测结果做成共享的，或把缓存周期拉长。
6. ~~**模型网关仍然没有限流配置**~~ —— 已在第八节处理，但结论是**网关这一层限不住**
   （两个开关在本部署里实测无效），限额改放在 Fleet 队列侧。账号真实额度仍未拿到，
   见 8.4。
7. `tests/test_system_snapshot.py::test_observation_adapter_uses_fixed_service_proxy_queries`
   在基线上就失败，本次没有修，也不在本方案范围内。

## 八、网关限流（2026-09-13 追加）

方案 §11 风险 2 要求"给网关配排队而不是报错"。做法是先量再改。

### 8.1 先量：一次试验到底向网关要多少

读 23 次副本试验的 `gateway-usage.jsonl`，按 60 秒滑窗取峰值：

| 指标 | 结果 |
|---|---|
| 单个智能体的在飞并发 | 峰值 **2**，五个副本上完全一致 |
| 每分钟请求数 | 均值 0.8–4.7，峰值 **10** |
| 每分钟 token 数 | 均值 3.7 万–12.3 万，峰值 **55.2 万** |
| 单次请求最大 token | 42.2 万，其中 86–91% 是缓存读 |

这些数字连同来源写进了 `deploy/stage2/litellm/config.yaml` 的注释。

### 8.2 再改：实测发现网关这一层根本限不住

给 `litellm-config-fleet` 加上限流后实测：

| 设置 | 上限 | 并发发出 | 结果 |
|---|---|---|---|
| `litellm_settings.max_parallel_requests` | 3 | 6 | 6 条全部 200，4.5 秒内一起返回 |
| `general_settings.global_max_parallel_requests` | 2 | 12 | 12 条全部 200，4.0 秒内一起返回 |

也就是说**两个开关在本部署里都不生效**。看 LiteLLM 源码，限流是一个代理钩子，
`global_max_parallel_requests` 从 `data["metadata"]` 里取值，而这套部署只有 master key、
没有 key management 存储，钩子拿不到限额。

发一个看起来在保护、实际什么都不做的配置比不发更糟，所以：集群里两个都没留，
`litellm-config-fleet` 已删除，五个副本回到与单系统一致的 `litellm-config`。
渲染脚本的 `--max-parallel-requests` / `--account-rpm` / `--account-tpm` 保留，
供将来接上带存储的共享网关时使用，配置文件注释里写明了实测无效这件事。

### 8.3 限额放在真正能排队的地方

能排队的是 **Fleet 自己的队列**：条目在队列里等，直到有副本空出来。
所以预算校验放在 `FleetConfig`：填了账号的 `account_rpm` / `account_tpm` 之后，
`max_concurrency` 乘以实测的单次试验峰值若超出账号额度，配置直接被拒，并在报错里
写明最多能并发几条。不填就沿用原行为。

一个结构性提醒：**没有共享网关**。每个控制器 Pod 各有一个 sidecar，互相看不见对方的流量，
所以任何 per-Pod 的限额都必须按"账号额度 ÷ 副本数"来分，副本数一变就要重算。
要一次性解决，得起一个共享网关并让所有控制器指过去，但那与 sidecar 的现有设计前提冲突，
需要单独决策。

### 8.4 仍然不知道的

DashScope 这个账号真实的 RPM/TPM 没有从控制台读到，所以 `account_rpm` / `account_tpm`
目前留空，校验不生效。按 8.1 的峰值推算：20 并发最坏情况会向账号要 200 请求/分钟、
1100 万 token/分钟。扩容到 20 之前必须先把这两个数字查出来填上。

## 九、D7/D8 上副本（2026-09-13 追加）

### 9.1 替代档资格

D7/D8 的门槛是**同一副本上三家都要有 `platform_sandbox`**，而这要靠 substitution 档资格。
每个副本跑一轮三家，共 15 次。过程中有两件事值得记：

1. **资格记录与网关配置绑定。** 第一轮 substitution 是在加了限流的 `litellm-config-fleet`
   下跑的，而 base 记录是在原表下跑的，发布时被拒：`qualification uses a different gateway
   configuration or route`。这是设计使然，不是缺陷。删掉限流表、五个副本回到原表之后，
   重跑了一整轮 substitution，两类记录才对得上。
2. **`coroot_call_failed` 是偶发的。** 用智能体自己的 `coroot_ro` 客户端逐个副本查
   `container_resources_cpu_usage_seconds_total`，六个命名空间全部 `ok=true`、各 7 条序列，
   说明 Coroot 侧没有系统性问题。失败散落在不同副本的不同家上，重跑即过。

最终五个副本全部三家通过并发布。`/api/v1/stage2/options` 的 `capability_loss.runnable`
在 s02–s05 为 true；s01 的 deepseek-harness 连续三次 `coroot_call_failed`，该副本因此
仍是 false，已用 `POST /slots/s01/drain` 排空，不参与本轮派发。

### 9.2 替代工具资格探针

在每个可用副本上跑 `python -m stage2_service.capability_loss.qualification_probe --ttl-hours 24`，
全部 `ok=true`、`loader_accepted=true`、`modes=[d7,d8]`、无失败项：

- D7 样本：`coroot_ro` 与 `telemetry_ro` 各一条，对应本副本 cart Pod 的 UID。
- D8 试注入：`chaos_control`（ChaosBlade）与 `chaos_mesh_control`（Chaos Mesh）都确认生效、
  也确认已删除。

s05 第一次的 ChaosBlade 试注入被安全策略拒绝（`PLAN_REJECTED_BY_SAFETY_POLICY`），
重跑一次即通过，文件按执行器合并，旧记录标记为 `replaced_by_new_canary`。

这说明替代工具资格这条链在副本命名空间里是通的，包括经两个执行器真实注入再清理。

## 十、扩容到 20（2026-09-13 追加）

用户批准的顺序是"先补网关限流 → 再做 D7/D8 的副本资格与试跑 → 最后才扩到 20"。
前两步见第八、九节。这一节是第三步：按 2 → 5 → 10 → 17 逐级加，每级都实跑一批。

### 10.1 10 路并发：并行度是真的

批次 `ramp10-20260913`，10 条 C0，派发到 10 个互不相同的命名空间：

| 指标 | 结果 |
|---|---|
| 墙钟 | **32.3 分钟** |
| 串行等价（各条耗时之和） | 195.7 分钟 |
| 实际加速 | **6.1×** |
| 单条耗时 | 8.4 – 31.9 分钟 |
| 平台故障 | **0** |
| 平台重试 | **0**（10 条全部 attempts=1） |
| 结论归属 | agent 2，platform 0 |
| 判决 | PASS 6、FAIL 2、CASE_INVALID 2 |

两条 Failed 都是 codex 的 C0 被记为 `CASE_INVALID`，归属 agent，不是平台问题。
加速只有 6.1× 而不是 10×，是因为最长的一条要 31.9 分钟，而墙钟由最长的那条决定；
批次里条目耗时差 3.8 倍，这是智能体自身的差异，不是调度的损耗。

### 10.2 扩容路上撞到的第一件事：共享 Jaeger 会把节点吃光

加到 10 个副本之后例行看节点，发现 `observability/jaeger`：

- `SPAN_STORAGE_TYPE=memory`，**没有 trace 上限**，`resources: {}`，**没有内存 limit**；
- 常驻 **83.7 GiB**，每小时还在涨 **9.6 GiB**，而该节点只剩 19 GiB 可用。

也就是大约 **2 小时后整个节点 OOM**。这是我这边的副本流量打出来的，
但组件是单系统路径也在用的共享件，所以先备份再动：trace 数据导出到主机上的
`jaeger-before.json`，然后加 `MEMORY_MAX_TRACES` 与 `limits{memory,cpu}`。
改完常驻掉到约 1 GiB，节点可用内存从 19 GiB 回到 104 GiB，跨副本查 trace 仍然正常。

**第一次的上限选错了。** 设的是 300000 条，但容器随后仍然 `OOMKilled`（exit 137）。
量了一下才明白：17 个副本的 load-generator 合起来，光 frontend 一个服务就有
**约 8000 条 trace/分钟**，300000 条正好是 37 分钟的量，而这 37 分钟的量正好把
12 GiB 占满——上限和 limit 撞在同一个点上，等于没设。

改成 **150000** 条：稳态约 6 GiB（limit 的一半），保留窗口约 19 分钟。
单次试验是 5 分钟，智能体取证也在同一窗口内，所以 19 分钟是够的。

这条要记住的不是"Jaeger 会涨"，而是**内存型后端的 trace 上限必须按实际摄入率反算**，
而摄入率随副本数线性涨。副本数再变，这个值要重算。

### 10.3 撞到的第二件事：20 个副本超出节点的 Pod 数上限

`replicas=20` 置备后，20 个副本命名空间全部 6/6 起来了，但 s16、s18、s20 三个控制器
Pod 排不上：

```
0/2 nodes are available: 1 Too many pods, 1 node(s) had volume node affinity conflict
```

两件事叠在一起：

1. **kubelet 的 `maxPods` 是默认的 110**，两个节点合计 220 个 Pod 位。
   每个副本要 7 个 Pod（6 个被测系统 + 1 个控制器），集群里还有 73 个与本方案无关的 Pod
   （完整 `otel-demo` 23、kube-system 12、observability 9、coroot 8、openebs 7、
   chaos-mesh 7、其余 6）。
2. **控制器的 PVC 是 `openebs-hostpath`，本地卷**。一旦某个控制器的 PVC 绑到了某个节点，
   这个控制器就只能在那个节点上跑。绑定发生的那一刻恰好是它所在节点被副本的被测系统 Pod
   填满之前，于是后来它自己就没位置了——另一个节点有空位也去不了。所以清掉 nodeSelector
   没有用，这不是调度偏好问题，是卷亲和性。

| 副本数 | 本方案 Pod | 集群总 Pod | 占 220 的比例 |
|---|---|---|---|
| 17 | 119 | 192 | 87% |
| 20 | 140 | 213 | **97%** |

97% 意味着整个集群只剩 7 个 Pod 位。而每次试验的重置都要滚动重建被测系统的 Deployment，
滚动更新本身需要临时多出 Pod 位。留 7 个位跑 20 路并发，重置会间歇性排不上。

**这一级需要集群属主决定**：把两个节点的 kubelet `maxPods` 调高（比如 150）
就能到 20，但那是节点级配置，要改 kubelet 配置并重启 kubelet，影响的是整个共享集群，
不在本方案的操作范围内。我没有做这个改动。
