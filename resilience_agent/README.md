# Resilience Analysis Agent

## V2：CodeGraph + Kubernetes + LangChain语义扫描

当前生产候选实现已收敛为12类可用ChaosBlade验证的韧性缺陷。Controller通过显式配置的代码库路径执行CodeGraph `init/index/sync`；LangChain子Agent只能使用受限的`query/callers/callees/context`和结构化Kubernetes配置查询。

编排使用LangGraph：

```text
CodeGraph/K8s资格化
  -> 扫描计划
  -> 12个模板专用无状态子Agent（隔离上下文）
  -> 独立反证Verifier
  -> 确定性证据/D类/故障/目标/置信度门
  -> semantic-scan-report.json
```

固定输出包含：`defect_name`、`evidence_explanation`、`mechanism_chain`、`available_fault_types`和`fault_injection_target`。Prompt全部位于`prompts/semantic_scan/`，每个模板有独立Prompt，且每次Run保留Prompt Hash、CodeGraph Index Hash、Kubernetes Manifest Hash、Evidence Ledger、子Agent Draft、Verifier结论和断点检查点。

本地OTel Demo完整扫描：

```bash
uv run python scripts/run_semantic_scan.py \
  --config resilience_agent/config/semantic-scan.otel-demo.yaml
```

RD-14正负对照：

```bash
uv run python scripts/run_semantic_scan.py \
  --config resilience_agent/config/semantic-scan.rd14-positive.yaml
uv run python scripts/run_semantic_scan.py \
  --config resilience_agent/config/semantic-scan.rd14-negative.yaml
```

一次网关/模型故障不会丢失进度：指定原Run ID加`--resume`即可重试未完成模板，前提是CodeGraph和Kubernetes输入Hash与检查点一致。

## Episode一对一编译

`scripts/generate_episodes.py`只消费Verifier已确认的`TemplateMatch`。它为每个匹配生成一份内部Episode和一份Agent可见题目；重复模板ID、缺少Pod绑定、没有ChaosBlade FaultProfile、小于600秒窗口或无清理/效果判据都会拒绝编译。

```bash
uv run python scripts/generate_episodes.py \
  --config resilience_agent/config/episode-generation.rd14-positive.yaml \
  --scan-report artifacts/semantic-scan/semantic-rd14-positive-v4/semantic-scan-report.json
```

内部Episode保留缺陷证据、机制链、精确Pod UID、命令、扰动和Oracle；公开题目由`prompts/episode/agent_task.md`渲染，并在落盘前执行确定性泄漏检查。

语义扫描使用独立的`config/model-semantic-scan.yaml`，当前固定`gpt-5.5 + medium + store=false`，并由LangChain Tool/Model Call Limit、单Agent硬超时和上下文字符预算共同限制执行。

## V1兼容实现（仅用于回归）

以下30类DefectSpec和正则预筛流程是旧实现，为了现有测试和工件兼容而保留，不再作为生产缺陷匹配策略。

这是当前工程自行实现的韧性分析 Agent。BladeAI、Claude Code、Codex 等外部智能体系统不读取这里的模板，也不参与“缺陷识别 → Episode 设计”过程；它们至多在后续 Benchmark 阶段成为被测对象。

```text
授权的配置 / 源码 / 系统上下文
  -> 确定性证据采集和 matcher 预筛
  -> 自有模型通过受限工具进行语义分析
  -> candidate-defects.v0.1（Schema 校验）
  -> 确定性 Episode 骨架 + 自有模型设计评审
  -> episode-designs.v0.1（内部草案，未执行、未锁题）
  -> agent-run.json（模型、工具、token 和阶段追踪）
```

## 职责边界

| 对象 | 负责 | 不负责 |
| --- | --- | --- |
| Agent 编排器 | 确定输入范围、调用两个内部能力、校验接口、落盘结果 | 执行故障、替代 Controller/Evaluator |
| 缺陷识别能力 | 采集证据，由模型结合 30 类 DefectSpec、源码、配置和系统上下文形成或驳回候选 | 接受不存在的文件/行号或将候选宣布为已验证缺陷 |
| Episode 设计能力 | 由确定性骨架和模型共同设计快照、工作负载、证据、动作空间、预算、实验序列、Oracle 和恢复 | 自动确认隐藏因果真值、虚构运行时参数或直接锁题 |
| Controller / Evaluator | 在另行授权后执行安全门禁，并独立验证效应、业务影响、因果机制和恢复 | 接受本 Agent 的自证作为最终结果 |

`tasks/catalog/resilience-defect-classes.v0.1.yaml` 是缺陷语义的唯一事实源。`templates/defect-matchers.v0.1.yaml` 只保存操作化识别规则；增加语言或框架支持时追加 matcher，新增缺陷类型时先新增并校验 DefectSpec。`templates/episode-design-templates.v0.1.yaml` 保存不同缺陷族共用的 Episode 设计策略，不复制具体缺陷的触发与证据描述。

## 模型和工具边界

生产路径默认使用独立配置 `config/model.yaml`：

```text
model = gpt-5.5
protocol = Responses API
reasoning_effort = xhigh
store = false
```

这不是 `harness/models.yaml`。后者定义 Benchmark 被测模型；前者只服务于我们自己的分析 Agent。默认网关已经配置为 `https://api.nexustokenai.com`。API Key 优先读取独立环境变量，其次读取 macOS Keychain 服务 `resilience-agent-llm`；密钥不会写入配置、日志或产物。

推荐在 macOS 上只执行一次：

```bash
make resilience-agent-configure-key
```

该命令会由 Keychain 提示安全输入。之后可执行 `make resilience-agent-check-model`；它只显示非敏感的模型和凭据来源。

环境变量可用于临时覆盖：

```bash
export RESILIENCE_AGENT_LLM_API_KEY='<runtime-secret>'
# 可选覆盖默认网关
export RESILIENCE_AGENT_LLM_BASE_URL='https://<gateway>/v1'
# 可选覆盖，默认 gpt-5.5
export RESILIENCE_AGENT_LLM_MODEL='gpt-5.5'
```

可复制 `config/model.env.example` 作为临时变量清单，但不要把真实密钥写回仓库。

模型不能直接访问文件系统或执行命令，只能调用六个只读工具：列文件、文字搜索、限行读取、Kubernetes YAML 资源解析、读取 DefectSpec、读取系统上下文。路径穿越、凭据文件、超大文件、超长读取、未知工具和超过轮次预算都会被拒绝。模型给出的证据在进入候选前必须重新读取并验证文件和行号。

源码、部署快照和应用契约位于不同目录时，必须逐个显式授权只读 evidence root；额外根使用别名前缀，不能扩大到整个工作区。例如：

```bash
uv run python -m resilience_agent run \
  --project ../benchmark-sources/materialized/train-ticket-upstream \
  --evidence-root benchmark-config=environment/kubernetes/train-ticket \
  --evidence-root benchmark-app=environment/applications \
  --evidence-root benchmark-workload=environment/workloads/train-ticket \
  --context artifacts/resilience-agent/train-ticket-static-20260823/system-context.yaml \
  --output-dir artifacts/resilience-agent/train-ticket-model-run
```

## 两阶段接口

`candidate-defects.v0.1` 包含候选 ID、DefectSpec 引用、目标组件、置信度、逐行证据、缺失保护、替代解释和运行时验证要求。

`episode-designs.v0.1` 包含完整 Episode 草案：

- 固定应用快照与不可变运行时目标；
- 可重复工作负载、基线窗口和业务 SLO；
- 执行侧可使用的证据契约；
- 允许的触发类型、候选执行器、参数和实验预算；
- 基线对照与候选假设验证两个实验；
- Episode validity、Safety、Fault effect、SLO、Causal mechanism、Diagnosis、Recovery 七道独立 Oracle 门禁；
- 清理、业务恢复、停止条件和锁题前校准要求。

实验计划现在是 `experiment_sequence`，只是 Episode 的一个组成部分。静态候选只会生成 `truth_status: hypothesis_not_independently_confirmed` 的内部设计；在控制实验、独立 Oracle 和恢复可重复性通过前，`ready_for_lock` 始终为 `false`。

## 最小运行示例

以下命令只读取示例源码、配置和上下文，不访问集群，也不执行故障：

```bash
uv run python -m resilience_agent run \
  --project resilience_agent/examples/minimal \
  --context resilience_agent/examples/minimal/system-context.yaml \
  --output-dir artifacts/resilience-agent-minimal
```

默认 `run` 必须取得上述模型配置；缺少凭据时会明确失败，不会悄悄退回正则结果。仅做离线诊断或测试时，必须显式声明：

```bash
uv run python -m resilience_agent run \
  --project resilience_agent/examples/minimal \
  --context resilience_agent/examples/minimal/system-context.yaml \
  --output-dir artifacts/resilience-agent-minimal-offline \
  --reasoning-mode deterministic
```

模型模式输出 `candidate-defects.json`、`episode-designs.json`、`model-defect-assessment.json`、`model-episode-review.json` 和 `agent-run.json`。运行清单记录请求模型、网关返回模型、推理强度、工具轨迹和 token 用量；离线模式只输出前三类确定性产物中的候选、Episode 与运行清单，并明确记录 `model: null`，不能冒充模型分析。
