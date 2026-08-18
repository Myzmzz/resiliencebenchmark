# Resilience Benchmark

面向 AI Agent 的端到端韧性测试 Benchmark 仓库。项目将题目、被测环境、Agent 接入、运行时控制、独立评价和批量运行拆分为可独立演进的模块。

> 当前状态：仓库骨架已初始化，尚未提供可运行的 Episode、Harness 或评分器。

## 核心目标

Benchmark 不仅判断 Agent 是否成功执行一条故障命令，而是评估它能否在安全约束和有限实验预算下，完成“选择实验 → 执行实验 → 分析证据”的闭环，并识别、验证和定位真实的系统韧性问题。

一次有效的 Episode 至少需要同时具备：固定的系统快照、可重复的工作负载、Agent 可见证据、受约束的动作空间、实验预算、对 Agent 隐藏的因果真值、独立 Oracle 以及可验证的恢复条件。

## 仓库结构

```text
resiliencebenchmark/
├── tasks/          # 题目、Episode 定义与 Ground Truth 契约
├── environment/    # 被测系统、观测数据与环境重置
├── harness/        # Agent 接入、工具接口与运行记录
├── controller/     # 阶段控制、安全门禁、运行时扰动与兜底清理
├── evaluator/      # 独立 Oracle、指标、评分与结果验证
└── scripts/        # 初始化、单次运行与批量评测入口
```

### 模块边界

| 模块 | 负责的核心对象 | 不应承担的职责 |
| --- | --- | --- |
| `tasks/` | Episode 输入、约束、预期输出和隐藏因果真值 | 直接操作运行环境 |
| `environment/` | 系统生命周期、负载、可观测数据和重置 | Agent 策略和评分规则 |
| `harness/` | 模型适配、工具调用、权限边界和完整轨迹 | 代替 Oracle 判定成功 |
| `controller/` | 阶段状态机、预算、安全门禁、扰动和超时清理 | 使用 Agent 的自证作为最终结论 |
| `evaluator/` | 故障生效、SLO 违反、因果机制、诊断和恢复判定 | 向 Agent 泄露 Ground Truth |
| `scripts/` | 组装并调用各模块 | 承载不可复用的核心业务逻辑 |

`controller/` 是运行时控制面：它根据 Episode 约束推进阶段，在执行前完成安全检查，在执行中管理扰动、停止条件和实验预算，并在 Agent 失联或超时时仍能触发独立清理。这一边界使得“被测 Agent 的能力”与“Benchmark 自身的安全性”可以分开验证。

## 预期执行链路

```mermaid
flowchart LR
    T["tasks: Episode 契约"] --> C["controller: 阶段与安全控制"]
    S["scripts: 运行入口"] --> C
    C --> H["harness: Agent 与工具"]
    C --> E["environment: 系统与扰动"]
    H --> E
    H --> R["运行轨迹与 Agent 结果"]
    E --> O["独立观测证据"]
    T --> V["evaluator: Oracle 与评分"]
    R --> V
    O --> V
```

建议的 Episode 生命周期为：

```text
prepare → qualify → baseline → plan → execute/observe
        → recover → evaluate → cleanup
```

安全失败、超过预算或命中停止条件时，控制器应直接转入 `cleanup`，而不是继续尝试获取更高分数。

## Ground Truth 与公开仓库

`tasks/` 保留 Ground Truth 的数据契约，但正式评测中的隐藏因果真值不应直接放在 Agent 可读的公开路径。公开仓库可提供 schema、示例 Episode 和已解锁样例；隐藏测试的 Ground Truth 应由评测服务或受控制的私有数据包交给 `evaluator/`。

## 开始使用

```bash
git clone https://github.com/Myzmzz/resiliencebenchmark.git
cd resiliencebenchmark
find . -maxdepth 2 -type f | sort
```

当前只能检查仓库骨架，还不应执行真实故障注入。

## 下一阶段

1. 定义 Episode、Agent 输出和运行轨迹的版本化 schema。
2. 实现一个可重复、可恢复的最小 Episode，并标定无 Agent 对照组。
3. 实现 Harness 和 Controller 状态机，先打通超时、停止和兜底清理。
4. 实现独立 Oracle，分别验证 Episode 有效性、安全性、故障效果、SLO、因果机制和恢复。
5. 增加单次运行、批量运行和结果导出脚本，再进行跨 Agent/模型对比。
