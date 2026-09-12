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

## 四、部署与验收

见第五节（执行中，结果随后补入）。
