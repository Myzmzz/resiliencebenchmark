# Stage-2 实验门禁地图

**梳理日期** 2026-09-09
**对象** 分支 `codex/stage2-d0-integration`
**方法** 按实验生命周期分四段并行梳理，每段用统一字段产出门禁表，随后对关键结论逐条抽查核实
**规模** 门禁类函数 170 个、门禁类 48 个

---

## 0. 怎么读这份文档

第 1 节是结构结论，**先读这一节**——后面所有"重复"现象都由它解释。

第 3–6 节是四段的完整门禁表，字段统一，可以横向对照。表里"失败行为"一栏是这份文档的核心：它区分一道门是真能拦住、还是缺配置就静默失效。

第 7 节把分散的问题收敛成五个模式。第 10 节列出本次亲自核实（读代码或跑代码）的结论，与转述分析结果区分开。

**一个提醒：** 表中"状态"反映当前代码行为，不等于设计意图。有几处静默放行是有测试覆盖的有意设计（如策略门缺文件时全放行），已在说明栏注明。

---

## 1. 结构：四套并行编排栈

这是"规则乱"的根源。平台里有四个编排入口，各有自己的执行单元和整套门禁，**四个都是活路径**。

| 编排栈 | 执行单元 | 定义位置 | 入口 | 部署中是否运行 |
|---|---|---|---|---|
| `CampaignEngine` | `CampaignRequest` | `stage2_service/campaign.py:158` | Stage-2 服务 API | ✓ 矩阵 Job |
| `MultiLevelOrchestrator` | `TrialTicket` | `progression/orchestrator.py:83` | `scripts/run_harness_trial.py --episode` | 脚本调用 |
| `ExecutionWorkflow` | `RunRecord` | `controller/execution_workflow.py:50` | `scripts/run_execution_worker.py` | 脚本调用 |
| `DiscoveryWorkflow` | `RunRecord` | `controller/discovery_workflow.py:145` | `scripts/run_control_worker.py` | 脚本调用 |

### 1.1 四套栈互不知情

`rg 'run_store|RunStore|ExecutionWorkflow' stage2_service/` 零命中；反向亦然。后果：

- **同一个集群上有两套并发控制。** `ExecutionWorkflow` 有集群变更租约（`controller/run_store.py:375`），Stage-2 任务路径**从不申请这把租约**。两者谁也拦不住谁。
- **概念重复。** "验证业务恢复"有两个协议，签名不同、各服务一套栈：

```
RecoveryVerifier(record: RunRecord, execution_report)
    → controller/execution_workflow.py:30

BusinessRecoveryVerifier(ticket: TrialTicket, level, trial_report)
    → controller/trial_finalization.py:27
```

### 1.2 最实质的重复：两套 trial 前置准备

| | `controller/trial_preparation.PerTrialPreparer` | `stage2_service/preparation.KubernetesTrialPreparer` |
|---|---|---|
| 所属栈 | ExecutionWorkflow / progression | CampaignEngine（**现役**） |
| 复位核验 | ✓ 每 trial 强制 | ✗ 无 |
| 唯一 Pod 绑定 | ✓ | ✓ |
| 基线合格闸门 | ✓ 每 trial 强制 | ✗ 无 |
| 流量凭据签发 | ✗ | ✓ |

谁也不调用谁。**现役路径缺少复位核验与基线闸门**，这是段二里最需要注意的一点。

---

## 2. 门禁状态标记说明

后面四段的表里，"状态"取以下五个值之一：

| 标记 | 含义 |
|---|---|
| **拦得住** | 真正 fail-closed，失败即拒绝 |
| **静默放行** | 缺环境变量、缺文件、或数据缺失时放行，且不报错、不留痕 |
| **空转** | 被赋值、被落盘，但没有任何代码读它做判断 |
| **重复** | 与另一处门禁检查同一件事 |
| **冲突** | 与另一处门禁判据不一致，或两道门互斥、只有一道生效 |

---

## 3. 段一：提交与准入

从用户提交测试任务到该任务被允许开始执行之前。

### 3.1 路径 A：`POST /api/v1/stage2/tasks`（预期入口，16 道门）

| 门禁名 | 位置 | 触发时机 | 拦什么 | 判据 | 失败行为 | 状态 |
|---|---|---|---|---|---|---|
| 服务可用性 | `api.py:366` | 路由入口 | 服务未装配时收单 | `task_service is None` | fail-closed 503 | 拦得住 |
| `ContractModel` 配置 | `contracts.py:18` | pydantic 解析 | 未知字段、事后改字段 | `extra="forbid", frozen=True` | fail-closed 422 | 拦得住 |
| 字段约束 | `task_service.py:166-180` | pydantic 解析 | 超长 prompt、非法应用名 | 正则 + 长度 | fail-closed 422 | 拦得住 |
| `validate_model_alias` | `task_service.py:222-229` | pydantic 校验 | 网关不提供的模型别名 | `value in STAGE2_SUPPORTED_MODELS` | fail-closed 422 | 拦得住 |
| `normalize_disturbance_variant` | `task_service.py:231-291` | pydantic 预处理 | disturbance 与变体自相矛盾 | 变体映射表 | 显式冲突才 raise；**label 分支静默改写** | 冲突 |
| prompt_level_label 校正 | `task_service.py:256-268` | pydantic 预处理 | 谎称"故障类型已给定" | `_prompt_declares_fault_type` 关键词表 | **静默降级**，从不拒绝 | 静默放行 |
| `validate_case_selection` | `task_service.py:293-344` | pydantic 后校验 | 非 otel-demo、空/重复/越界 case | 硬编码 `!= "otel-demo"`；`TASK_SELECTABLE_CASE_IDS` | fail-closed 422 | 拦得住 |
| `find_idempotent` | `task_service.py:459-469` | `create()` 第 1 步 | 重复提交 | 幂等键正则 + 文件命中 | 格式错 422；命中返回旧任务 | 见 3.3 |
| `has_unresolved_recovery` | `task_service.py:531-542` | `create()` 第 2 步 | 上个任务恢复未收尾 | 全局扫 `status.json`，内联状态集合 | fail-closed 409 | **冲突** |
| 活动任务扫描 | `task_service.py:591-599` | `create()` 第 3 步 | 并发跑两个 Campaign | `supervisor.list_runs()` 有 RUNNING | fail-closed 409 | **冲突** |
| 模型/Harness 预检矩阵 | `task_service.py:600-625` | `create()` 第 4 步 | 网关没探活的模型 | `preflight["model_matrix"][h][m] is True` | fail-closed 422 | 拦得住 |
| `_runnable_request_reason` | `task_service.py:816-854` | `create()` 第 5 步 | 能力探针未过、无反馈通道 | 能力文件多项 | fail-closed 422 | 拦得住 |
| `_capability_loss_support` | `task_service.py:799-813` | 上一条内部 | D7/D8 四框架未全就绪 | 四框架各需三项 | fail-closed | 拦得住 |
| `_qualification_for_task` | `task_service.py:856-919` | `create()` 第 6 步 | 名义上拦"未做 D0 认证" | D0 凭据多项 | **8 个出口全部降级 diagnostic，从不拒绝** | **静默放行** |
| `validate_harness_matrix` | `contracts.py:626-673` | 构造 CampaignRequest | required 模式缺 D0 ref | `qualification_mode=="required"` | fail-closed | **空转**（见 3.2.6） |
| `TaskStore.create` 目录独占 | `task_service.py:428-429` | 落盘 | task_id 撞车 | `mkdir(exist_ok=False)` | fail-closed 500 | 拦得住 |
| `submit` 单活检查 | `api.py:59-65` | `create()` 最后一步 | 并发 Campaign | `any(not future.done())` | fail-closed 409 | **拦得住（唯一同时约束两个入口的门）** |
| `RuntimeLock.acquire` | `runtime_lock.py:72-121` | submit 内 | 跨进程抢运行时 | `flock(LOCK_EX\|LOCK_NB)` + 文件安全校验 | fail-closed 409 | 拦得住 |

### 3.2 段一发现的重复与冲突

**3.2.1 "是否已有运行在跑"，三套实现三种判据**

| 位置 | 判据 | 生命周期 |
|---|---|---|
| `task_service.py:591` | `list_runs()` 里 `status=="RUNNING"` | 随进程消失 |
| `api.py:64` | `any(not future.done())` | 随进程消失 |
| `api.py:66` | flock | 随进程消失 |
| 磁盘 `status.json` | 任务状态 | **永不消失** |

三者不等价。`list_runs()` 内部对 done 的 future 调了 `future.result()`，崩溃过的 future 会被重新抛出；`submit()` 不调 `.result()`，因此不受影响。

**3.2.2 "控制动作是否未结清"，同一概念两套状态集合**

```python
# task_service.py:51 —— 定义了，用在 :1414 和 :1426
CONTROL_STATES = {"REQUESTED", "RUNNING"}

# task_service.py:538 —— has_unresolved_recovery 里内联，多两个状态
{"REQUESTED", "RUNNING", "PARTIAL", "FAILED"}
```

后果：一个 `PARTIAL` 的动作会挡住**所有新任务**，却挡不住新的控制动作。

**3.2.3 四套 case 集合并存，任意两套都不相同**

| 常量 | 内容 |
|---|---|
| `CORE_STAGE2_CASE_IDS` | C0 **P1 P2** D1–D6 ← `CampaignRequest.cases` 默认值 |
| `TASK_STAGE2_CASE_IDS` | C0 D1–D6 |
| `TASK_SELECTABLE_CASE_IDS` | C0 D1–D6 **D7 D8** |
| `CAPABILITY_LOSS_CASE_IDS` | D7 D8 |

**推论：P1/P2 只能通过 `/api/v1/campaigns` 跑到**——任务接口的可选集合里没有它们。

**3.2.4 "qualification" 一个词两个东西、两种失败行为**

`capability.qualification_passed`（Harness 能力探针，硬拒）与 D0 模型资格（静默降级），并排出现在同一个 `create()` 里。

**3.2.5 d6_variant 一致性检查做两遍、规则不同**

`before` 阶段（`:269-279`）只在 key 显式出现时报错；`after` 阶段（`:311-332`）用 `object.__setattr__` 直接覆盖。前者拒绝、后者改写。

**3.2.6 "required 模式必须有 D0 ref" 这条门禁不可能触发**

`qualification_mode` 由 `_qualification_for_task` 决定，而它只在拿到合法 ref 时才返回 `"required"`（`:914-919`）。于是 `contracts.py:652-659` 的检查恒真——**一条永远不会拒绝的门禁**。

**3.2.7 options 广告的能力与实际门禁判据不同**

`_harness_option` 依据 `feedback_channels` 判断 `supported_interaction_modes`（`:1202-1205`），但 `_runnable_request_reason` 的条件是 `(interaction_mode is GUIDED 或 decision_policy is CLARIFY_MISSING) and not feedback_channels`（`:834-838`），而 `decision_policy` 默认就是 `CLARIFY_MISSING`。所以一个被 options 标为"支持 autonomous"的 Harness，按默认参数提交仍会被 422。

### 3.3 拒绝之后不能恢复的三处

| 现象 | 机制 | 出路 |
|---|---|---|
| **`list_runs()` 中毒 → 全平台 500** | `futures` 字典从不删除条目，一次 runner 抛异常后 `create()` 第 3 步永久重抛，且不是 `TaskConflict`，FastAPI 直接 500 | **只能重启进程** |
| **`has_unresolved_recovery` 全局永久阻塞** | 一个 terminal 的旧任务只要 `control_actions` 里留了 `PARTIAL`，所有新任务创建都是 409。无时间窗、无终态过滤、无忽略开关 | 重跑同一控制动作直到 `verified`；环境已不可验证时只能手工改文件 |
| **幂等键烧毁** | `store.create` 在所有校验之后、`submit` 之前落盘。submit 抛错时任务写成 REJECTED，但幂等键已写好，之后同 Key 重试永远返回那个被拒任务 | 换 Key，或删幂等文件 |

对照组：`RuntimeLock` 用 flock，进程死自动释放；`RunStore` 的两种租约带 TTL 且认领时先删过期项——**这是全仓库唯一自愈的并发设计**。

### 3.4 路径 B：controller 工作流

与 Stage-2 任务 API **没有任何代码连接**，是第二套独立准入系统。

| 门禁名 | 位置 | 拦什么 | 判据 | 失败行为 |
|---|---|---|---|---|
| `RunStore.create_or_get` | `run_store.py:137-150` | 同 request_id 换了 spec | `spec_sha256` 比对 | fail-closed |
| `RunStore.transition` | `run_store.py:240-256` | 跳阶段/动终态 Run | `ALLOWED_PHASE_TRANSITIONS` + 非终态 | fail-closed |
| 人工审批闸 | `run_contracts.py:64-67` | 未审批就进 BASELINING | `phase is AWAITING_APPROVAL` | **可选路径**：QUALIFYING 可直接跳到 BASELINING |
| `claim_next_run` | `run_store.py:473-540` | 两个 worker 抢同一 Run | 单事务 + 先删过期租约 | fail-closed |
| `acquire_mutation_lease` | `run_store.py:375-432` | 两个 Run 同时改集群 | 单行租约 + TTL + Run 非终态 | fail-closed；**Stage-2 路径完全不申请** |
| `renew_worker_lease` | `run_store.py:542-574` | 非属主续租 | worker_id 匹配 + 未过期 | fail-closed |
| baseline 合格闸 | `execution_workflow.py:100-105` | 基线不合格却继续 | `baseline["qualified"] is not True` | fail-closed → CASE_INVALID 终态 |

### 3.5 段一小结

> 同一个"能不能开跑"的判断被拆成三层互不相认的状态——进程内存（`futures`）、文件锁（flock）、磁盘任务目录（`has_unresolved_recovery`）——它们生命周期不同、判据不同、失效方式不同（一个静默清空、一个自动释放、一个永久累积）；而唯一能同时约束所有入口的门只有 `submit()` 里那个 `future.done()`，旁边还并排放着一个把所有门禁全部跳过的 `/api/v1/campaigns`。

---

## 4. 段二：Trial 前置资格

任务已被接受，到智能体真正动作之前。

### 4.1 栈 A（现役 CampaignEngine 路径）

| 门禁名 | 位置 | 触发时机 | 判据 | 失败行为 | 有效期 | 状态 |
|---|---|---|---|---|---|---|
| `_runnable_request_reason` | `task_service.py:815` | 建任务时 | 能力文件多项 | fail-closed | 无 | 拦得住 |
| `harness_capabilities_from_qualification` | `capability_preflight.py:23` | 每次 preflight | 只看 `status == "passed"` + `evidence_ref` 非空 | 降级为未认证 | **无。发布即永久有效** | **静默放行** |
| `D0QualificationGate.qualify` | `qualification.py:25` | campaign 第一步 | `execution_allowed = diagnostic or formal_eligible` | **fail-open** | 无 | **静默放行** |
| `verify_d0_ref` | `qualification.py:255` | D0 门内逐 harness | manifest sha256、网关 route/config_sha256、request_ids、receipt 重读 | fail-closed | **无时间过期**，`finished_at` 只用于排序 | 拦得住 |
| `select_verified_d0_ref` | `qualification.py:116` | preflight 选 ref | 同上 + route 一致 | fail-closed | 同上 | 拦得住 |
| `KubernetesEnvironmentGate.qualify` | `runtime_adapters.py:49` | **整个 campaign 只跑一次** | 副本齐 + 无残留 ChaosBlade + load-generator 就绪 | fail-closed | 无 | 拦得住（但见下） |
| `KubernetesTrialPreparer._resolve_target` | `preparation.py:218` | 每 trial | 两个 selector 去重后恰好 1 个 Ready Pod | fail-closed | 无 | `agent_selected` 模式完全跳过 |
| `ApplicationTrafficCapabilityIssuer.issue` | `preparation.py:64` | 每 trial | `application_owned` + `load_generator_ready` + `traffic_observed` | fail-closed | 签发 900s TTL，chaos create 时强制校验 | `application_owned` 硬编码 True，恒真 |
| `ApplicationTrafficCapabilityIssuer.rebind` | `preparation.py:104` | Pod 被替换后 | ledger 非符号链接 + 身份未变 + 流量重新合格 | fail-closed | 重绑刷新 900s | 拦得住 |
| `CapabilityLossRuntimeFactory._qualification` | `capability_loss/factory.py:97` | D7/D8 每 trial | 私有文件校验 + schema + scope 匹配 + `issued_at <= now < expires_at` | fail-closed | **全仓库唯一带真过期的门** | 拦得住 |
| `_d7_precheck` | `factory.py:116` | D7 切换前 | 备用 server 对该 uid 有历史样本 | fail-closed | **样本无下界，任意旧样本都算数** | 拦得住（弱） |
| `_d8_precheck` | `factory.py:130` | D8 切换前 | create/destroy 冒烟证据 | fail-closed | **canary 完全无时间字段** | 拦得住（弱） |
| `OtelDemoResetter._verify_environment` | `reset.py:112` | 每 trial **结束后** | qualified + business_healthy + inventory_safe | 名义 fail-closed | 无 | 见下 |
| `NextTrialReadiness` | `campaign.py:1364` | 上条之后 | `restore.verified and reset.verified` | **fail-open** | 无 | **空转** |
| `publish_capabilities` | `capability_qualification.py:438` | 离线运维 | 全套 BASE_CHECKS + 网关一致性 + receipt 重读 | fail-closed | 无。**发布时校验，使用时不复校** | 拦得住 |
| `evaluate_base_channel_qualification` | `channel_qualification.py:753` | 离线 | 事件单调、无越权、读通、四件套 | fail-closed | **记录里零时间戳** | 拦得住 |

**两个关键细节：**

- `KubernetesEnvironmentGate` **只在 campaign 开头判一次**，后续每个 trial 都不再复查环境是否还干净。
- `inventory_safe` 的判据是 `prior_evidence.get("chaos_inventory_clear") is not False`（`reset.py:133`）。键缺失时 `None is not False` → True。**缺证据等于有证据。**

### 4.2 栈 B（controller worker，未接入现役路径）

| 门禁名 | 位置 | 判据 | 失败行为 | 备注 |
|---|---|---|---|---|
| `PerTrialPreparer.__call__` | `trial_preparation.py:104` | 复位核验 → 唯一 Pod → 基线合格 → 压测运行，四步串联 | fail-closed | `experiment_workload_session=None` 时跳过压测检查 |
| `LiveResetVerifier` | `trial_preparation.py:154` | `runtime.status is QUALIFIED and chaosblade_global_count == 0` | fail-closed | **第二个条件冗余**，QUALIFIED 已含它 |
| `LiveTargetResolver` | `trial_preparation.py:172` | Ready + component key 归一后恰好 1 个 | fail-closed | — |
| `FormalOtelBaselineMeasurer` | `trial_preparation.py:206` | 实跑 600s workload，`summary.qualified` | fail-closed | 实时探测，无过期问题 |
| `EngineeringOtelBaselineMeasurer` | `trial_preparation.py:315` | 留存报告多项 + 窗口长度 | fail-closed | **不校验测量时间，几个月前的基线永久有效** |
| `wait_cleanup_workload` summary 闸 | `scripts/reset_episode.py:361` | `summary["qualified"]` | fail-closed | — |
| `KubectlReadOnlyAdapter.scan` | `system_snapshot.py:180` | nodes/controllers/pods 全 Ready + chaos 为空 | 返回 UNQUALIFIED | ConfigMap 读失败被吞，但该字段无消费方 |
| `SystemScanner.scan` | `system_snapshot.py:360` | namespace/lock 匹配 + source 物化 | 混合 | **adapter 缺失不抛错，只写字符串进 limitations，靠调用方自觉** |
| `promote_episode._blockers` | `tasks/episode_promotion.py:140` | 四快照 + 两布尔 | fail-closed | 见 6.4 |
| `RuntimeDisturbanceInjectorFactory` | `controller/disturbance_runtime.py:41` | `requested_types ⊆ allowed_types` | fail-closed | **与 `ControllerDisturbanceSafetyGate` 两层重复判同一件事** |

### 4.3 那几组同名不同类的，到底是不是重复

| 组 | 判定 | 处置建议 |
|---|---|---|
| `EnvironmentGate` / `KubernetesEnvironmentGate` | **不是重复**。Protocol + 唯一生产实现 | 两个都留，Protocol 侧加后缀 |
| `ResetVerifier` / `LiveResetVerifier` | **不是重复**。同上 | 同上 |
| `CleanupGate` / `CleanupVerifier` | **不是重复，是两个层级**。前者是 `ControllerPolicy` 里的策略开关结构体，后者是 Run 结束时的整机核验回调 | 都留，`CleanupGate` 改名 `CleanupPolicy` |
| `*Qualification` 四类 | **不是重复，是四种不同的东西**：D0 运行时门 / 离线生产者 / 单框架生产者 / 两布尔 DTO | 前三个留，**`PromotionQualification` 该删** |

**真正的重复在别处**：`PerTrialPreparer` 与 `KubernetesTrialPreparer`（见 1.2）。

### 4.4 资格文件：预录 vs 实时

**混合，且比例失衡——绝大多数是预录，只有环境/目标那一层是实时的。**

实时探测：`KubernetesEnvironmentGate.qualify`、`KubernetesTrafficEvidence.current`、`_resolve_target`、`LiveResetVerifier`、`LiveTargetResolver`、`FormalOtelBaselineMeasurer`。

预录文件，按失效风险排序：

1. **能力发布文件（最严重）**。`publish_capabilities` 在**发布时**做全套硬校验（网关 route + config_sha256 + receipt 重读）；`harness_capabilities_from_qualification` 在**使用时一条都不复校**，直接 `data = dict(capability)`。网关配置换了、证据文件删了、模型路由改了，这个文件照样让 harness 通过。
2. **D0 campaign**。完全没有时间过期。唯一失效机制是 `gateway_config_sha256` 变化——网关不动，一年前的 D0 就一直有效。
3. **留存正式基线**。校验窗口长度和 qualified 标志，**不校验测量时间**。
4. **D7/D8 资格文件**。唯一有 `expires_at` 的，且 fail-closed——但**没有任何脚本生产这个 schema**，必须手写，`expires_at` 由人填。
5. **通道资格记录**。`channel_qualification.py` 全文件零时间戳。

**另一项核实：** 九个 `qualify_*.py` 里，只有 `qualify_agent_channel.py` 和 `qualify_bladeai_task.py` 的产物被运行时消费。其余七个的输出 schema 全仓库除生产脚本外无人读取——是给人看的证据，**不是门禁**。

### 4.5 段二小结

> 判据的"生产"和"执行"被拆到两个不通消息的地方——离线脚本把资格写进文件时做全套严格校验并且永不加时间戳，运行时读这个文件时只认一个 `status == "passed"` 字段；再叠上 D0 那条"任何失败都降级为 diagnostic 而 diagnostic 直接放行"的链路，结果是整段看起来门禁林立，实际能真正拦住 trial 开跑的只剩"集群此刻是不是干净、目标 Pod 是不是唯一"这两条实时检查。

---

## 5. 段三：运行中授权

智能体每一次工具调用要经过的关卡。

### 5.1 一次故障注入的完整门序（16 道）

以 codex / claude-code / deepseek 调 `chaos_create_experiment` 为例：

| # | 门禁 | 位置 |
|---|---|---|
| 1 | 回环绑定校验（进程启动期） | `http_runtime.py:511` |
| 2 | Bearer token 校验 | `http_runtime.py:379` |
| 3 | **审计桥 before_call** | `runtime_audit.py:85` → `audit_bridge.py:380` |
| 4 | **PolicyGate 策略判定** | `http_runtime.py:267` |
| 5 | `_runtime_value` 绑定值比对 | `chaos_control/server.py:71` |
| 6 | 账本文件锁 | `chaos_core/ledger.py:30` |
| 7 | `_assert_create_runtime_gates` | `chaos_core/service.py:846` |
| 8 | `_verify_controller_identity` | `service.py:903` |
| 9 | `_assert_fault_type_authorized` | `service.py:1151` |
| 10 | `_assert_condition_safety_ttl` | `service.py:1161` |
| 11 | `_assert_create_request_plan_schema` | `service.py:432` |
| 12 | `_assert_expected_fault_contract` | `service.py:1171` |
| 13 | `_assert_user_decision`（内含 `_assert_not_report_only` + 第二次 plan schema） | `service.py:349` |
| 14 | `_verify_baseline_gate` + `_assert_baseline_token_unused` | `service.py:943` / `:1046` |
| 15 | 目标 UID 复核 + `_assert_no_other_executor_active` + 未归属资源扫描 | `service.py:240` / `:1067` / `:261` |
| 16 | `validate_action` 安全策略 | `service.py:271` |

**BladeAI 额外在第 3 步之前**经过：SDK 确认中断 → `partial_plan_from_native_proposal`（`bladeai_task.py:488`）→ `harness_confirm`（本身又走一遍第 2、3 步）→ `confirmation_granted`（`:591`），然后才由 shim 发起上面 16 道。

### 5.2 分层门禁表

#### A 层：传输与身份

| 门禁名 | 位置 | 判据 | 失败行为 | 谁能绕过 |
|---|---|---|---|---|
| `_parse_host` / `_is_loopback_host` | `http_runtime.py:511` / `:525` | host 是 loopback 或显式放开 | fail-closed（启动失败） | 设 `RESBENCH_MCP_HTTP_ALLOW_NON_LOOPBACK=true` |
| `FileBackedBearerTokenVerifier` | `http_runtime.py:379` | 常数时间比对；文件须绝对路径、非符号链接、无 group/world 位 | fail-closed | stdio 传输完全不装 verifier |
| `StaticBearerTokenVerifier` | `http_runtime.py:139` | sha256 常数时间比对 | fail-closed | 生产不走此路 |

#### B 层：审计桥

| 门禁名 | 位置 | 判据 | 失败行为 | 状态 |
|---|---|---|---|---|
| `audit_client_from_env` | `runtime_audit.py:17` | socket 变量存在则必须同时有 run_id + authority | fail-closed（进程起不来） | **socket 变量缺失时返回 None，整条审计链静默消失** |
| `audited_async_call` / `audited_sync_call` | `runtime_audit.py:77` / `:106` | `decision.allowed is True` | fail-closed | **`client is None` 时直接执行 operation** |
| `_verify_endpoint` / `_verify_server_peer` | `audit_bridge.py:213` / `:227` | uid 匹配、mode 无 group/world、SOCK；Linux 另校 `SO_PEERCRED` | fail-closed | macOS 无 `SO_PEERCRED` 时降级为"不断言" |
| `_authorized` / `_dispatch` | `audit_bridge.py:367` / `:380` | trial_id + authority 精确匹配；call_id 须本监听器所发；结果指纹冲突拒绝 | fail-closed，**`allowed is not True` 一律视为拒绝** | macOS 上 `_authorized` 直接 `return True` |
| `CapabilityLossRuntime.before_call` | `capability_loss/runtime.py:88` | essential 直接放行，否则按 D7/D8 编排器状态 | fail-closed | 只有 D7/D8 才装，其余恒放行 |

**审计链的失效方式不对称**，这是段三最要紧的一点：

```
变量设了但身份不全  →  进程起不来（fail-closed，正确）
变量没设            →  客户端为 None → 直接执行，不审计、不写账本
```

#### C 层：策略门

| 门禁名 | 位置 | 判据 | 失败行为 | 状态 |
|---|---|---|---|---|
| `PolicyGate._decision` | `http_runtime.py:267` | 逐调用重读策略文件（无 mtime 缓存）；`channel_unavailable_until > now` → D5 响应；状态 ∈ {disabled, decoy} → 拒 | 读取异常 → `PLATFORM_POLICY_ERROR`；**`policy_file is None` → 直接放行** | 有意设计，有测试覆盖 |
| `_validated_existing_path` | `capability_policy.py:391` | 路径链无符号链接、普通文件、mode 恰为 0600、父目录无 group/world 位 | fail-closed | — |
| `effective_tool_state` | `capability_policy.py:293` | `server_policy is None` → disabled；工具无条目继承 server 状态 | fail-closed（缺条目=禁用） | — |
| `_reject_harness_channel` | `capability_policy.py:420` | server 名等于受保护常量即拒 | fail-closed | 保证确认/求助通道永不被关闭；代价是它完全不受 PolicyGate 管 |

#### D 层：chaos 工具层

| 门禁名 | 位置 | 判据 | 状态 |
|---|---|---|---|
| `_runtime_value` / `_bound` | `chaos_control/server.py:71`、`chaos_mesh_control/server.py:40` | Agent 自带值与进程绑定值不同 → `BOUND_RUNTIME_MISMATCH`；都空 → `BOUND_RUNTIME_MISSING` | **与 `_assert_create_runtime_gates` 重复校验同样五个字段** |

#### E 层：chaos_core 注入门

全部位于 `mcp_servers/chaos_core/service.py`。

| 门禁名 | 位置 | 判据 | 失败行为 | 状态 |
|---|---|---|---|---|
| `_ledger_file_lock` | `ledger.py:30` | `flock(LOCK_EX\|LOCK_NB)` + 进程内锁；锁文件须 0600 普通文件 | fail-closed | 拦得住 |
| `_assert_create_runtime_gates` | `:846` | execute_enabled → kubeconfig → namespace → token ref → pod uid → baseline token → handle 未用过 | fail-closed | **重复**（与 D 层） |
| `_verify_controller_identity` | `:903` | 有 lease：controller_id 匹配 + 未过期 + `os.kill(pid,0)` 存活；否则查活体 Pod UID | fail-closed | **lease 与 pod 标识全未配置时 `:933-934` 直接 return** |
| `_assert_fault_type_authorized` | `:1151` | `fault_type ∈ config.allowed_fault_types` | fail-closed | **allowlist 为空集时全放行**；与 provisioning 期重复 |
| `_assert_condition_safety_ttl` | `:1161` | `duration_seconds == config.condition_safety_ttl_seconds` | fail-closed | **该值为 None 时不检查**；与下条互斥 |
| `_assert_create_request_plan_schema` | `:432` | `validate_agent_plan` | fail-closed | **同一次 create 里被调两次** |
| `_assert_expected_fault_contract` | `:1171` | `{fault_type, duration_seconds, intensity}` 与环境变量契约全等 | fail-closed | **`expected_fault is None` 时不检查**；与上条互斥 |
| `_assert_not_report_only` | `:690` | 决策文件中 `report_only is True` | fail-closed，但抛裸 `JSONDecodeError` | **用裸 `json.loads`，跳过私有文件校验**；destroy 路径上是唯一读者 |
| `_assert_user_decision` | `:349` | `clarify_missing` 时：决策文件存在 + `approved is True` + `approved_plan` 逐字段相等 | fail-closed | **`agent_delegated` 时整段跳过**（有意设计） |
| `_verify_baseline_gate` | `:943` | `sha256(token)` 定位账本；passed/run_id/namespace/pod_uid 全等；未过期；目标绑定一致 | fail-closed | 拦得住 |
| `_assert_baseline_token_unused` | `:1046` | 扫全部 cleanup 账本，token 哈希撞且 handle 不同 → 拒 | fail-closed | 账本目录不存在时直接 return |
| 目标 UID 复核 | `:240-252` | `backend.get_pod_uid()` 与请求 `target_uid` 相等 | fail-closed | 与 baseline 门的目标绑定重叠 |
| `_assert_no_other_executor_active` | `:1067` | 同 run_id、不同 executor_id、状态活跃 → 拒 | fail-closed | **唯一的跨执行器互斥点** |
| 未归属资源扫描 | `:261-269` | 存在 `not terminal and not owned` 的资源 → 拒 | fail-closed | 拦得住 |
| `validate_action` | `:271-279` | controller 安全策略（含并发数上限） | fail-closed | **namespace 在这里第二次被校验** |

#### F 层：销毁与 TTL

| 门禁名 | 位置 | 判据 | 状态 |
|---|---|---|---|
| `_assert_destroy_runtime_gates` | `:1108` | kubeconfig 全等 + handle 合法 | 销毁路径**不检查** execute_enabled、namespace、controller 身份、baseline——方向安全，属合理设计 |
| `_deadline_watchdog` → `cleanup_expired_leases` | `chaos_control/server.py:321`；实现 `service.py:701` | 只处理本 executor 拥有、活跃、已过期的账本项 | **静默降级**：`except Exception: pass`，每 2 秒重试永不升级；进程被杀则 TTL 兜底消失 |

#### G 层：代码沙箱

| 门禁名 | 位置 | 判据 | 状态 |
|---|---|---|---|
| `SandboxBroker.start` | `code_sandbox/broker.py:56` | socket 不存在；父目录非符号链接、`mode & 0o007 == 0`、gid 匹配；socket chown 0660 | 拦得住 |
| `_assert_guest_peer` | `broker.py:151` | Linux `SO_PEERCRED` uid 等于 guest_uid | **非 Linux 直接抛错**——比审计桥的"降级放行"更严，两处判据相反 |
| `_parse_call` | `broker.py:136` | `tool ∈ allowed_tools`、args ≤ 64 KiB | 白名单**含 `chaos_control.*`，排除 `harness_channel`** |
| `PolicyGate.guard("run_python")` | `code_sandbox/server.py:59` | 同 C 层 | 同 C 层 |

#### H 层：BladeAI 确认门

| 门禁名 | 位置 | 判据 | 状态 |
|---|---|---|---|
| `partial_plan_from_native_proposal` | `bladeai_task.py:488` | 恰好一个目标名、结构化 `fault_intent`、有 `params`；timeout 与 duration 若都在须相等 | fail-closed |
| `confirmation_granted` | `bladeai_task.py:591` | **只认 `ok is True and allowed is True`**；`approved` / `decision:"approved"` 一律不算 | fail-closed |
| 确认回调整体 | `bladeai_worker.py:733-786` | 任意异常 → `return False` | fail-closed；SDK 不抛中断则整个门不触发 |
| `HarnessChannelService.confirm` | `harness_channel/service.py:214` | `approved is True` + 非空 `approved_plan` + answer_mode 合法 | fail-closed；可反复调换计划重批（有意） |

#### I 层：Provisioning 期

| 门禁名 | 位置 | 判据 | 状态 |
|---|---|---|---|
| `Stage2PermissionManager._provision` | `permissions.py:132-137` | 每个 `allowed_fault_types` 都在 `default_policy(namespace).fault_type_contracts` 内 | fail-closed + 回滚；**与 E 层 `_assert_fault_type_authorized` 同一约束** |

### 5.3 段三的重复与冲突

**重复五处：**

1. 绑定运行时值查两遍（D 层 `_runtime_value` vs E 层 `_assert_create_runtime_gates`，同样五个字段）
2. Plan schema 查两遍（`service.py:210` 与 `_assert_user_decision:360`，同参数）
3. namespace 查两遍（`_validate_namespace:1142` 与 `validate_action:271`）
4. 故障类型查两遍（provisioning 期 vs 运行期）
5. D7/D8 工具禁用查两遍且是两套机制（审计桥回调 + 策略文件），同一次调用可能被两个不同错误码拒绝

**判据冲突四处：**

1. **`duration_seconds` 两套互斥判据**。`_assert_condition_safety_ttl`（要求等于 Controller TTL）与 `_assert_expected_fault_contract`（要求等于 main_fault 契约），由 `selection_mode` 二选一注入，另一个置空 → 另一道门静默失效。**两道门永不同时生效，也永不冲突报错，读代码时看不出来。**
2. **同一决策文件两个读者两种安全标准**。`_assert_user_decision` 用 `_read_private_json_file`（强制 0600、拒符号链接），`_assert_not_report_only` 用裸 `json.loads`。destroy 路径上后者是唯一读者。
3. **peer 身份校验两处判据相反**。`audit_bridge.py:368-372` 无 `SO_PEERCRED` 时放行；`code_sandbox/broker.py:152-153` 同样情况直接抛错。
4. **策略文件读取时机不一致**。`disabled/decoy/channel_unavailable` 每次调用重读（有专测），但 `chaos_create_uncertainty_variant` 在进程启动时冻结。`PolicyGate.chaos_create_uncertainty_variant` 就是为实时读它而写的，**生产代码里无人调用**。后果：`runtime_adapters.py:404-417` 那条"Trial 中途激活 D6"的路径写了策略文件，对已在运行的进程无效。

### 5.4 段三的静默放行（10 处，按危害排序）

| # | 门禁 | 触发条件 | 位置 |
|---|---|---|---|
| 1 | **整条审计链** | `RESBENCH_MCP_AUDIT_SOCKET` 缺失 | `runtime_audit.py:20-22, 82-84` |
| 2 | **PolicyGate 全部策略** | `RESBENCH_MCP_POLICY_FILE` 缺失 | `http_runtime.py:268-269` |
| 3 | Controller 活体身份 | lease 文件与 Pod 标识都不配 | `service.py:933-934` |
| 4 | 故障类型授权 | allowlist 为空 | `service.py:1152-1153` |
| 5 | 安全 TTL | 变量为空 | `service.py:1162-1163` |
| 6 | explicit 故障契约 | 变量为空 | `service.py:1179-1180` |
| 7 | report_only 门 | 决策文件不存在 | `service.py:691-692` |
| 8 | 策略变更账本记录 | `ledger is None` | `capability_policy.py:218-219` |
| 9 | 带内通知 | `ledger_root is None` | `http_runtime.py:254-255` |
| 10 | TTL 看门狗 | 任何异常 | `chaos_control/server.py:325-326` |

**关键：** 这十项的"配齐了没有"分散在 `runtime_factory.py`、`harness_runtime.py`、`mcp_supervisor.py`、`permissions.py` 四个文件里，**没有任何一处集中断言**。

### 5.5 段三：有没有绕过所有门的旁路

**没有找到能绕过 E 层注入门的路径。** 四条候选均已核实排除：

- BladeAI 原生 `blade` 二进制被 shim 替换，shim 只发 MCP 调用，不执行二进制、不碰 kubectl；且 BladeAI 的 MCP 配置里没有 `chaos_control`
- BladeAI 的 K8s 回环代理只注册 GET 路由，每个请求都过 PolicyGate
- Agent 子进程环境里不含审计 socket 变量；伪造的 stdout 事件不构成权威事实
- `mcp_servers/d0_chaos_control/` 只剩 `__pycache__`，非活路径

**但有两条"少过门"的路径**（设计不对称，非漏洞）：

1. **`code_sandbox.run_python` → `chaos_create_experiment`**。沙箱白名单含全部 `chaos_control.*` 但排除 `harness_channel`。所以沙箱代码可以发起注入却无法发起确认——只能复用此前批准过的决策文件。C/E 层全部照常生效，但它绕过了 BladeAI 的 SDK 确认回调。
2. **`decision_policy == "agent_delegated"`**。`_assert_user_decision` 直接返回，"计划=批准"的绑定完全消失，只剩 TTL 门或 expected-fault 门兜底——**而这两道又是二选一的**。这是文档化的有意设计且有测试，但它意味着 **L0 档次的 trial 里，16 道门中最实质的一道是被配置关掉的**。

### 5.6 段三小结

> 同一个约束反复出现在不同层、用不同数据源和不同失效方向来表达，而"这道门是否生效"取决于一个不在门附近、也不会报错的配置项。门的数量看着很多，实际生效的子集要把四个文件的环境变量拼起来才能算出来。

---

## 6. 段四：收尾、判定与晋升

### 6.1 finalization 与事实层

| 门禁名 | 位置 | 判据 | 失败行为 | 状态 |
|---|---|---|---|---|
| `_trial_status` | `finalization.py:408` | `inventory["qualified"] is True` | fail-closed，降级后连带 target/fault 全 False | 拦得住 |
| `_safe` | `finalization.py:427` | `try/except Exception` | **静默降级**，只剩类型名，不进 reason_codes | 静默放行 |
| `target_verified` | `finalization.py:88-107` | ever_active + name/uid 非空 + 三元组一致 | fail-closed | `agent_strategy` 跳过 UID 比对；`VERBATIM` 跳过 fault_type 比对 |
| `fault_absent` | `finalization.py:201-205` | `post_inventory["qualified"]` 且三选一 | fail-closed | 三套 gate 的共同 requirement |
| `business_recovered` | `finalization.py:206-242` | 四项全 True | fail-closed，但异常被吞成 `business_healthy: False` | 拦得住 |
| **`effect["verified"]` 覆写** | `finalization.py:177-193` | `condition_monitor.effect_condition_met is True` 即无条件置 verified | **fail-open** | condition_monitor 来自 **Harness 侧产物**，不是 Controller Oracle |
| `_validate_platform_event_order` | `trial_facts.py:136` | sequence 唯一递增、时间不倒退、trial_id 唯一 | fail-closed → CASE_INVALID | **`platform_events` 键缺失时返回空元组，全部检查跳过** |
| `validate_node_invariants` | `trial_facts.py:120` | 三条不变式 | fail-closed → CASE_INVALID | **第三条是死条件**（见 6.3） |

### 6.2 实验门与裁决

| 门禁名 | 位置 | 判据 | 状态 |
|---|---|---|---|
| `_experiment_gate` | `node_evaluation.py:142-187` | 三套 requirements（SAFE_REFUSAL / CONTROL / 其余） | **除 C0 与 SAFE_REFUSAL 外不改 verdict**，失败项只变成 `GATE_*` 字符串 |
| `complete_experiment_claim_allowed` | `node_evaluation.py:186` | 同 gate | **空转**，全仓库无消费方 |
| `_safe_refusal_observed` | `node_evaluation.py:595-630` | 状态字段 ∨ 拒绝事件 ∨ **14 个关键词匹配** | 判据落在对 Agent 自述文本的关键词匹配上 |
| `_platform_status` | `evaluator.py:735-810` | status ∈ {failed,timeout} → HARNESS_FAILED；否则按 kind 校验扰动 | **C0/P1/P2 不在映射内，`:744-745` 直接 `return VALID`** |
| D7/D8 事实同一性门 | `evaluator.py:48-62` | case/trial_id/variant 一致且 ground_truth 全等 | fail-closed → CASE_INVALID |
| `EvaluationInvariantError` handler | `evaluator.py:73-89` | 捕获该异常 | 只捕这一种，其它直接冒泡 |
| `_capability_loss_score` | `evaluator.py:1177-1196` | restored + cleanup + fault_absent + 可验证 + trial_valid | fail-closed → CASE_INVALID |
| `_agent_verdict` 清理前置 | `evaluator.py:234-235` | `controller_cleanup_verified ∧ fault_absent` | **判 FAIL 而非 CASE_INVALID**——平台没清干净记到 Agent 头上 |
| verdict 三重覆写 | `evaluator.py:92-108` | D7/D8 用 final_score；C0 与 SAFE_REFUSAL 用 gate.passed | 三段 if 顺序执行，后者覆盖前者 |
| **CONTRADICTED 判定** | `evaluator.py:1070-1071` | 任一节点 CONTRADICTED | **只改 `AgentOutcome`，不动 `AgentVerdict`** |
| `assess_evidence` 矛盾检测 | `evidence_assessment.py:12-59` | 正则匹配 Agent **自己文本** | **fail-open，从不与 `recovery.fault_effect_verified` 比对** |

**对照参考：** D7/D8 的诚实性判据 `_honesty`（`capability_loss/runtime.py:400-405`）**确实**做了 `assessment=="verified" and oracle_effect is not True` 的 Oracle 比对。同一系统里存在两套互不相同的"证据诚实"定义。

### 6.3 evaluator/ 包（另一套词汇）

| 门禁名 | 位置 | 判据 | 状态 |
|---|---|---|---|
| episode_id 门 | `evaluator/evaluator.py:63-69` | contract 与 observation 的 episode_id 相等 | fail-closed → CASE_INVALID |
| 必需门 + 证据策略 | `evaluator/evaluator.py:83-141` | 仅 `required=True` 参与；证据 kind 在白名单、有 ref | **`required=False` 的 gate 整条跳过；若无任何 required gate，`primary_status` 直接 PASS** |
| `evaluate_level` 状态映射 | `evaluator/evaluator.py:317-329` | CASE_INVALID→SKIP；PASS ∧ 扰动通过→PASS；**其余（含 INCONCLUSIVE）→FAIL** | 取证失败被记成 Agent 的 FAIL |
| `evaluate_disturbance_response` | `evaluator/evaluator.py:231-280` | 每条 behavior 需 PASS + 证据合规 | 证据 kind 由生产方自行贴标 |
| **L1 效果证据继承** | `runtime_oracle.py:101-113` | L1 成功即缓存 refs，L2+ 直接复用并置 verified | **fail-open，这是主路径** |
| diagnosis gate 证据构造 | `runtime_oracle.py:138-151` | 为空则伪造 `oracle://.../diagnosis-unavailable` | **fail-open** |
| `_disturbance_behaviors` | `runtime_oracle.py:396-452` | 对 `json.dumps(agent_result)` 关键词匹配，但输出贴 `{"kind":"controller_record"}` | **绕开契约自己声明的"自报不能作最终证据"** |
| `RuntimeRunOracle` | `runtime_oracle.py:298-328` | `"independent": True` 是写死字面量 | SKIP 折成 FAIL，run 级无 invalid 态 |

**`validate_node_invariants` 第三条为何是死条件：** 它检查 `gate.requirements` 里的 `fault_effect_verified` 键，但 `_experiment_gate` 三个分支的 requirements 里**都没有这个键**，`.get()` 返回 `None`，而 `None != False`，因此恒不触发。

### 6.4 晋升门禁

| 门禁名 | 位置 | 判据 | 状态 |
|---|---|---|---|
| `_blockers` | `episode_promotion.py:142-162` | 6 条：三快照 QUALIFIED + calibration + 两布尔 + episode_id | **`cleanup_path_qualified` 在唯一生产调用点写死 True**（`discovery_workflow.py:340`），第 159 行永不触发 |
| readiness 覆写 | `episode_promotion.py:111-116` | **无判据**，直接赋 `ready_for_execution:True` | **fail-open**，覆盖上游算出的真实 blockers |
| `_resolve_target` | `episode_promotion.py:165-181` | 归一化后恰好 1 个 ready Pod | 归一化去掉 `service` 后缀与非字母数字，可能折叠不同组件 |
| `FAULT_POLICIES` | `episode_promotion.py:41-52` | trigger class 命中字典 | **参数全为硬编码常量**，与快照、基线、SLO 无关 |
| `EXECUTABLE_DISTURBANCE_PROFILES` | `episode_promotion.py:54-71` | profile_id 命中字典 | 6 个 profile 的扰动序列写死 |
| `_assert_public_safe` | `episode_promotion.py:286-304` | 硬编码黑名单：`RBD-[0-9]+` + 4 个键名 | 任何不叫这四个名字的私有键、任何非 `RBD-` 前缀的缺陷号都能通过 |

**`independent_observers_qualified` 的问题：** 传入的是 `snapshot.observers.status is QUALIFIED`（`discovery_workflow.py:337-339`），与 `_blockers` 第 154 行检查的是**同一条件**。名字承诺的"独立观测者的独立校验"并不存在，是同一条件数了两遍。

更矛盾的是：`discovery_workflow.py:484` 把 `_analysis_context` 的 `independent_observers_qualified` 写死 `False`，意味着上游一定判"未就绪"，然后被 `episode_promotion.py:111-116` 无条件覆盖成"已就绪"。

### 6.5 矩阵层

| 门禁名 | 位置 | 判据 | 状态 |
|---|---|---|---|
| 目录唯一性 | `matrix.py:206-211` | `matrix_root.exists()` | fail-closed |
| preflight 门 | `matrix.py:212-226` | 模型子集 + 4×2 组合全 True | fail-closed |
| **resume 完整性** | `matrix.py:171-194` | checkpoint 存在 + 文件存在 + `len(trials) == 9` | **静默降级**：任一不满足直接 `continue`；**manifest 只查存在不校哈希；只按数量判完整** |
| prior_pairs 一致性 | `matrix.py:257-258` | 无重复且是子集 | fail-closed |
| `_validate_campaign_gateway_evidence` | `matrix.py:630-660` | campaign 级三项 + 逐 trial 比对 | fail-closed；campaign 级三项是条件式，缺失即放行 |
| 逐 trial 路由检查 | `matrix.py:349-364` | model_alias 一致、config_sha256/route 非空且匹配 | fail-closed |
| **计分资格门** | `matrix.py:374-382` | `platform_valid ∧ not diagnostic_only ∧ agent_verdict ∈ {PASS, FAIL}` | `campaign.qualification["scored"]` 只记录、**从不参与过滤** |
| `expected_trial_count` | `matrix.py:468` | 硬编码 `56` | **与 `:238-240` 算出的 2×4×9 = 72 不一致**，56 是旧 7 用例集残留 |

### 6.6 "trial 是否有效"：11 处在判，判据不一致

分属三套互不引用的体系：

- **stage2_service 主路径（8 处）**：`_platform_status`、D7/D8 事实同一性门、不变式 handler、`_capability_loss_score`、`_experiment_gate` 自己的 CASE_INVALID、`CapabilityLossRuntime.trial_valid`、`score_capability_loss`、campaign 的 gateway 证据门
- **evaluator/ 包（3 处）**：episode_id 门、gate 级 CASE_INVALID、`evaluate_level` 的 CASE_INVALID→SKIP
- **matrix 层（1 处）**：计分资格

**四点不一致已核实：**

1. **判据语义完全不同**。一处问"扰动是否真施加+回滚+专属证据"；一处问"策略预检 + 还原成功"；一处问"episode_id 对得上 + 必需 gate 证据合规"。
2. **C0/P1/P2 没有平台有效性判据**，直接 `return VALID`。
3. **`TrialPlatformStatus` 区分 HARNESS_FAILED 与 CASE_INVALID，但 `TrialValidity` 只有 VALID/CASE_INVALID**，两者被压成同一个值——"超时"和"扰动没打上"在 trial_validity 上不可区分。
4. **`report.status=="timeout"` 被判两次且结论不同**：`_platform_status:741` 判 HARNESS_FAILED，`_agent_verdict:244` 判 FAIL。前者赢，后者是死代码。

另有第二套 `fallback_platform_valid`（`campaign.py:1007-1015`），只查 `report.status != "failed"`，连 timeout 都不查。生产不走此路，但任何没有 `decision` 方法的替换件都会激活它。

### 6.7 CASE_INVALID / SKIP / FAIL 的界线

设计意图应是：CASE_INVALID = 平台没构成有效实验条件；FAIL = 有效条件下 Agent 没做到；SKIP = 这一层根本没评。**执行不一致：**

- **stage2_service 侧根本没有 SKIP**——三个枚举里都没有这个值
- **SKIP 只存在于 evaluator/ 包，且是 CASE_INVALID 的别名**（`evaluate_level` 把 CASE_INVALID 映成 `primary_status="SKIP"`，而 `failure_status` 仍写 `"CASE_INVALID"`）
- **INCONCLUSIVE 落到 `else` 分支变成 FAIL**——取证失败被算成 Agent 失败
- **stage2_service 侧方向相反但同样越界**：`_agent_verdict:234` 把"没清干净"直接判 FAIL 而非 CASE_INVALID

### 6.8 段四小结

> "有效"和"通过"被拆成五个各算各的判定字段（`platform_status`、`experiment_gate.passed`、`AgentVerdict`、`AgentOutcome`、`trial_valid`），它们之间没有收敛点也不互相引用——最终谁进计分取决于下游读了哪个字段，而 matrix 读的是最粗的那个 `AgentVerdict`，于是 gate 失败和 FAIL_EVIDENCE 都对分数毫无影响。

---

## 7. 五类系统性问题

### 7.1 有一个入口绕过全部准入门禁

`POST /api/v1/campaigns`（`api.py:498-504`）的实现体是：

```python
def create_campaign(request: CampaignRequest) -> dict[str, str]:
    request_id = supervisor.submit(request)
    return {"request_id": request_id, "status": "ACCEPTED"}
```

第 3 节那 16 道门**一道都不经过**。它和任务接口在同一个应用、同一个 supervisor 上，路由无条件注册，**没有任何认证依赖**。唯一还生效的是 `submit()` 的单活检查和 RuntimeLock。

它还**不写任务记录**（没有 event_sink / result_sink），所以这条路径跑出来的运行在 `artifacts/tasks/` 里没有痕迹，事后 `has_unresolved_recovery` 也永远看不到它。

具体后果：P1/P2 只能通过它跑到。

### 7.2 门的"是否生效"取决于一个不在门附近、也不会报错的配置项

十处静默放行（见 5.4），其中六处是运行中的授权门。它们的失效方式有共同点：**不报错、不留痕、返回值与正常放行完全一样**。

而"配齐了没有"分散在四个文件里，没有任何一处集中断言。

### 7.3 写了却从来没人执行的门（6 道）

| 门禁 | 位置 | 现状 |
|---|---|---|
| `NextTrialReadiness` | `campaign.py:1364` | 只写 BLOCKED 进报告，无一处读作条件 |
| `complete_experiment_claim_allowed` | `node_evaluation.py:186` | 全仓库无消费方 |
| `validate_node_invariants` 第三条 | `trial_facts.py:120` | 检查的键在三套 requirements 里都不存在 |
| `cleanup_path_qualified` | `discovery_workflow.py:340` | 唯一生产调用点写死 `True` |
| `plan["readiness"]` | `episode_promotion.py:111-116` | 整块硬编码就绪，覆盖上游真实 blockers |
| `validate_harness_matrix` 的 required 检查 | `contracts.py:652-659` | mode 只在拿到合法凭据时才为 required，检查恒真 |

### 7.4 同一件事多处判，判据不一致

- "trial 是否有效"11 处（见 6.6）
- 四套 case 集合（见 3.2.3）
- `CONTROL_STATES` 常量与内联集合（见 3.2.2）
- `duration_seconds` 两道互斥的门（见 5.3）
- "是否已有运行在跑"三套实现（见 3.2.1）
- 两套"证据诚实"定义（见 6.2 对照参考）

### 7.5 判定与计分读的是不同字段

五个判定字段无收敛点，矩阵计分读最粗的 `AgentVerdict`：

```python
# matrix.py:374-382
eligible = [item for item in trials
            if item.platform_valid and not item.diagnostic_only
            and item.agent_verdict in {AgentVerdict.PASS, AgentVerdict.FAIL}]
passed = sum(item.agent_verdict is AgentVerdict.PASS for item in eligible)
```

而 `CONTRADICTED` 只写入 `AgentOutcome.FAIL_EVIDENCE`，verdict 原封不动 → **照样按 PASS 进分子**。`_experiment_gate` 对 D1–D8/P1/P2 的失败同理，只变成 reason_codes 字符串。

---

## 8. 命名带来的额外混乱

| 现象 | 说明 | 建议 |
|---|---|---|
| `CleanupGate` 不是门禁 | 它是 `@dataclass(frozen=True)` 的配置结构体（`enabled` / `max_cleanup_seconds` / `require_target_uid` / `verify_absence`），不含判定逻辑。真正执行检查的是 `CleanupVerifier` | 改名 `CleanupPolicy` |
| Protocol 与实现同名族 | `EnvironmentGate`/`KubernetesEnvironmentGate`、`ResetVerifier`/`LiveResetVerifier`、`SafetyGate`/`ControllerDisturbanceSafetyGate` 是正常配对，不是重复 | Protocol 侧统一加后缀 |
| `PromotionQualification` | 两个字段，一个写死 True，一个重复 `_blockers` 已有检查 | 删除 |
| "qualification" 一词指四种东西 | 运行时门（D0）/ 离线生产者（Channel、Capability）/ 单框架生产者（BladeAI）/ 空转 DTO。失败行为还不一样——有的硬拒，有的静默降级 | 分词命名 |

---

## 9. 建议的收敛顺序

按"能不能相信实验结果"排序，不是按改动难度。

**1. 关掉或收紧 `/api/v1/campaigns`**
要么下线，要么让它走和任务接口同一套准入校验。现状是一个无认证、无记录、绕过全部门禁的入口，且历史结果里可能已经有从这里跑出来的数据。

**2. 给十处静默放行加一个集中的启动断言**
trial 模式下，审计 socket、策略文件、Controller 身份、故障类型 allowlist 四项缺任一即拒绝启动。同时在每个工具返回值和 trial 产物里加"受控性"标记，让受控与未受控在数据里可区分。

**3. 让计分只读一个字段**
五个判定字段收敛成一个。最小改动是让 `AgentVerdict` 吸收 `AgentOutcome` 的失败态与 `experiment_gate` 的结果——否则"证据被推翻"和"实验门失败"对分数没有任何影响。

**4. 执行那六道空转的门，或者删掉它们**
`NextTrialReadiness.BLOCKED` 应当真的拦下一个 trial；`cleanup_path_qualified` 应当真的评估。**留着一个不执行的门比没有门更糟**——它会让人以为这件事有人管。

**5. 合并四套 case 集合，统一控制状态常量**
一套权威集合加派生视图，而不是四套平行常量。`CONTROL_STATES` 用在它该用的地方。

**6. 给预录资格加过期时间**
能力发布文件、D0 凭据、留存基线目前都无过期。发布时做全套硬校验、使用时一条不校，是段二最大的不对称。

**7. 决定四套编排栈的去留**
这是根因。至少要明确哪套是权威、哪套只做实验；否则每加一道门禁都要在四处各写一遍，而它们还会继续漂移。

---

## 10. 本次逐条核实的事实

以下结论均在本机直接读代码或跑代码确认，非转述：

- `/api/v1/campaigns` 的实现体只有一行 `supervisor.submit(request)`，无任何校验、无认证依赖
- `CONTROL_STATES = {"REQUESTED","RUNNING"}` 与 `has_unresolved_recovery` 内联的 `{"REQUESTED","RUNNING","PARTIAL","FAILED"}` 是两套集合，常量没用在关键处
- 四套 case 常量的实际取值已逐一打印比对，任意两套都不相同；`CampaignRequest.cases` 默认用含 P1/P2 的那套
- D0 门的放行判据是 `execution_allowed = diagnostic or formal_eligible`，`qualification_mode` 默认即 `"diagnostic"`，`_qualification_for_task` 有 8 个出口返回 diagnostic
- `NextTrialReadiness` 全仓库 10 处引用，全是枚举定义、字段声明与两处赋值，无一处读作条件
- 审计桥在客户端为 None 时直接执行 operation（`runtime_audit.py:82`）；策略门在策略文件为 None 时返回 `{"allowed": True}`（`http_runtime.py:268`）
- 矩阵计分读 `item.agent_verdict`；`CONTRADICTED` 只写入 `AgentOutcome.FAIL_EVIDENCE`，不改 verdict
- `expected_trial_count` 硬编码 56，同文件算出 2×4×9 = 72，差 16
- `_assert_condition_safety_ttl` 与 `_assert_expected_fault_contract` 各有 `if expected is None: return` 守卫，由 `selection_mode` 二选一注入
- `cleanup_path_qualified=True` 是 `discovery_workflow.py:340` 的字面量，而晋升门禁依赖它
- `CleanupGate` 是配置结构体，不含任何判定逻辑
- 四套编排栈的调用方各自唯一，互不引用
- `local_control_stack.py` 设 `RESBENCH_CHAOS_EXECUTE_ENABLED=true` 但不设审计 socket 与策略文件

---

## 11. 覆盖范围与未审计部分

**已覆盖：** `stage2_service/`（准入、前置、判定、矩阵）、`controller/`、`mcp_servers/`、`evaluator/`、`scoring/`、`tasks/episode_promotion.py`、`disturbances/`（门禁部分）。

**未覆盖：**

- `resilience_agent/`（7,382 行，语义扫描链路）
- `progression/controller.py` 的内部判据
- `controller/residual_cleanup.py`
- 各 `qualify_*.py` 脚本的内部实现（只核实了它们的产物是否被消费）
- `frontend/`
- `_execution_nodes` 的权重与分数细节
- `node_evaluation.py` 的 `_recovery_trigger_status` / `_conclusion_status` 内部判据

**一个提醒：** 表中"状态"标记反映当前代码行为，不等于设计意图。有几处静默放行是有测试覆盖的有意设计（如策略门缺策略文件时全放行、`agent_delegated` 时跳过用户决策门），已在对应说明栏注明。研究时请优先看"失败行为"一栏，它才是这份文档想传达的核心。
