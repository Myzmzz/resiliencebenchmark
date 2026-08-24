# 创建和运行多关卡 Episode

## 1. 先准备一个已合格的主线任务

输入 plan 必须只描述一道主线缺陷任务，并至少提供：`episode_id`、不可变运行时目标（namespace、Pod name、UID）、主故障类型/参数和 `budget.max_experiments`。三关 Episode 的总预算至少为 3；如果仍是内部 Episode 设计，不要直接交给被测 Agent，因为其中可能含隐藏假设。

```python
from progression.builder import build_multi_level_episode

episode = build_multi_level_episode(
    internal_plan,
    level_count=3,
    total_retry_budget=6,
    agent_visible_task=validated_public_episode,
)
```

`agent_visible_task` 必须是单独物化并通过 `episode-public.schema.json` 的公开合同。构造器不会自动把内部 plan 复制进 Agent prompt。

生成后同时做 JSON Schema 和语义校验：

```python
import json
import jsonschema
from progression.builder import validate_multi_level_episode

schema = json.load(open("tasks/schemas/multi-level-episode.schema.json"))
jsonschema.validate(episode, schema)
validate_multi_level_episode(episode)
```

语义校验额外保证 L1 无扰动、level id 连续、复杂度不下降、扰动 id 唯一以及关卡预算之和等于总预算。

## 2. 检查关卡选择

构造器按主故障类型推荐相关扰动。例如网络延迟优先选择效果偏离和遥测异常，CPU/内存类优先选择配额突变和安全逼近，Pod 类优先选择目标漂移。自动推荐是起点，不替代实验设计审核。

审核时确认三点：扰动不会泄漏缺陷答案；不同阶段的复合扰动不会制造无法归因的并发故障；每个 expected behavior 都有独立可采集证据。需要自定义时应改为注册的 `DisturbanceSpec`，不要嵌入 Shell 命令。

仓库样例为 `tasks/examples/multi-level/episode.3-levels.yaml`。

## 3. 注入 Controller 后端和安全策略

```python
from controller.disturbance import ControllerDisturbanceSafetyGate
from controller.safety import default_policy
from disturbances.injector import KubernetesDisturbanceAdapter, TelemetryInterceptorAdapter
from disturbances.types import DisturbanceType

gate = ControllerDisturbanceSafetyGate(
    default_policy({"otel-demo"}),
    allowed_types={item.value for item in DisturbanceType},
)
adapters = {
    "kubernetes": KubernetesDisturbanceAdapter(controller_k8s_client),
    "telemetry_interceptor": TelemetryInterceptorAdapter(mcp_proxy_client),
}
```

`controller_k8s_client` 使用 Controller 身份并实现精确 Pod 重启、ResourceQuota 读/改/恢复。`mcp_proxy_client` 实现规则注册和删除。任何后端在正式运行前都应通过权限、范围、幂等和清理资格测试。

每次重试和进入下一关前都必须从固定逻辑目标重新准备环境，并由 Controller 重新解析当前 Pod name/UID。Episode 中的 UID 是初始资格检查绑定，不是跨关永不变化的标识；`injector_factory` 应把本次 trial 的精确绑定传给 Injector，同时把绑定证据写入 Controller record。无法重新绑定或基线未恢复时，本次前置检查应为 `SKIP/CASE_INVALID`。

## 4. 使用实时事件驱动 Harness

`MultiLevelOrchestrator` 需要一个 streaming runner。runner 在 Agent 仍运行时发出事件：

```python
def runner(ticket, level, emit_event):
    # Agent 调用主故障工具并且 Controller 已独立确认效果后：
    emit_event(LifecycleEvent(
        run_id=ticket.run_id,
        level_id=ticket.level_id,
        phase=DisturbancePhase.EXECUTION,
        kind="main_fault_applied",
    ))
    # 每次实时 MCP 调用也应立即发 tool_call 事件。
    return live_harness_result
```

不能用“进程结束后解析 stdout”替代这一步。后者可以生成 trace 证据，但无法在第 N 次工具调用发生时注入环境变化。

## 5. 独立评测每次尝试

评测输入来自公开 contract、私有 Oracle observation、Controller record 和 harness trace：

```python
from evaluator.evaluator import evaluate_level, simplified_level_contract

level_result = evaluate_level(
    simplified_level_contract(episode["episode_id"]),
    oracle_observation,
    run_id=ticket.run_id,
    level=level,
    attempt=ticket.attempt,
    metrics={
        "duration_seconds": duration,
        "tool_calls": tool_calls,
        "tokens_used": tokens,
    },
)
```

前置条件失效返回 `SKIP/CASE_INVALID`；核心或扰动应对失败返回 `FAIL` 并保留 `failure_status`；只有所有必需项通过才返回 `PASS`。Agent 自报不能让 gate 或 expected behavior 通过。

## 6. 运行递进控制器

```python
from scripts.run_harness_trial import run_multi_level_episode

report = run_multi_level_episode(
    repo_root,
    episode_file=episode_path,
    run_id="run-20260823-001",
    agent_id="codex-gpt56-config-a",
    trial_runner=runner,
    level_evaluator=level_evaluator,
    injector_factory=injector_factory,
    progression_store=checkpoint_store,
    resume=False,
)
```

Controller 只消费 evaluator 的 `PASS/FAIL/SKIP`，不会从 Agent 退出码自行推断通关。失败自动停留本关；达到关卡或 Episode 总预算后停止后续关卡。

## 7. 评分和跨 Episode 聚合

先把同一 Episode、同一难度和同一环境配置的 Agent 放入同一 `comparison_group`，调用 `normalize_and_score_cohort()` 计算效率与 Episode 分。再调用 `aggregate_agent_scores()`，默认用总关卡数作为难度权重，也可传入冻结的 difficulty weights。

连续分只用于排序和分析。报告必须同时保留 Episode 离散状态、失败 gate、重试轨迹和 safety violations。

## 完成标准

一个多关卡 Episode 只有在以下条件都满足时才算可用于正式比较：Schema/语义校验通过；L1 控制组稳定；每种扰动在 Controller record 中可复现；实时事件链已验证；安全拒绝与 Agent 失联清理测试通过；Oracle 与 Agent 证据隔离；三关集成跑通；旧单关回归通过。

如果事件只能事后读取、目标 UID 未固定、清理只看 API ACK、效率比较组不同或扰动后端没有独立权限，当前方案不成立，应停在资格检查而不是开始正式测评。
