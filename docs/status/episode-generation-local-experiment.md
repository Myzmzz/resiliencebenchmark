# TemplateMatch 到 Episode 一对一生成本地实验

> 状态日期：2026-08-24 CST
>
> 范围：本地合同编译和泄漏验证；未运行ChaosBlade，未部署到node3

## 核心结论

Episode生成阶段已完整跑通。生成器不使用LLM自由拼接命令，而是从已经Verifier确认且`question_eligible=true`的`TemplateMatch`确定性编译内部Episode和Agent可见题目。

生成关系严格一对一：每个模板ID在一次扫描中最多有一个匹配；同一ID重复匹配时编译失败，不会默认选第一个。

## 实验结果

### 阳性对照

- 源扫描：`semantic-rd14-positive-v4`。
- 源匹配：1。
- 生成Episode：1。
- Episode ID：`EPI-F5184CBF4E79A02A`。
- 一对一校验：通过。

内部Episode包含：

- 私有`defect_basis`与0.98置信度证据链；
- fixture Pod name/UID和三类重绑条件；
- `pod-delete`一次性主故障；
- `duration_seconds=600`，语义为一次性故障后的观测窗口；
- 可执行`blade create k8s pod-pod delete ... --timeout 600`命令；
- `blade destroy {experiment_uid}`清理命令；
- 故障生效判据：旧UID消失、Ready Endpoint/业务容量变化；
- `target_drift`、`metric_data_gap`、`cleanup_confirmation_delay`三种动态扰动；
- `max_experiments=5`、清理句柄和中止条件；
- MCP/CodeGraph资源及6道Oracle门。

### 阴性对照

- 源扫描：`semantic-rd14-negative-v2`。
- 源匹配：0。
- 生成Episode：0。
- 一对一校验：通过。

相同CodeGraph业务链下，2副本+PDB+拓扑分散被正确判为不匹配，因此没有产生空题或误报题。

## 内外边界

内部`episode-internal.json`保留缺陷名、证据、机制链、Pod UID、精确命令、扰动和Oracle。

Agent可见`episode-public.json`只包含：

- 环境快照和固定SLO；
- 允许故障类型与逻辑目标范围；
- 实验预算、MCP和CodeGraph入口；
- 安全要求、期望证据和不确定性输出。

自动泄漏检查结果：缺陷名、Pod UID、主故障命令、清理命令、Evidence ID和Oracle字段的泄漏数均为0。

## 证据位置

- 结构化总结：`artifacts/episode-generation/local-episode-experiment-summary-v2.json`，`status=passed`。
- 内部Episode：`artifacts/episode-generation/semantic-rd14-positive-v4/EPI-F5184CBF4E79A02A/episode-internal.json`。
- 公开题目：`artifacts/episode-generation/semantic-rd14-positive-v4/EPI-F5184CBF4E79A02A/episode-public.json`。
- 输出Schema与上述工件同目录保存。

## 边界

本次`runtime_binding.status=fixture`，Pod name/UID是本地合同实验数据，不是真实OTel Demo Pod。部署前必须由node3 Controller从当前集群重新绑定真实Pod UID，不得将fixture Episode直接执行。
