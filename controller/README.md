# Controller

本目录是 Benchmark 的运行时控制面，用于实现 Episode 阶段状态机、安全门禁、实验预算、运行时扰动、停止条件、超时和兜底清理。

控制器必须独立于被测 Agent：即使 Agent 超时、崩溃或失去权限，它仍应能停止扰动并将环境收敛到可验证状态。

## 当前落地范围

控制面先实现“准备阶段”的不可变契约和本地安全校验，不直接连接 Kubernetes 或 ChaosBlade：

- `schemas/controller-plan.schema.json` 定义 Controller Plan 的公开配置形态；
- `examples/controller-plan.example.json` 给出无凭证、无隐藏答案的最小样例；
- `safety.py` 提供生命周期、ChaosBlade 动作预算和 Agent 失联清理判定；
- `tests/test_safety.py` 覆盖关键拒绝条件。

## 生命周期

标准阶段顺序为：

```text
prepare -> qualify -> baseline -> plan -> execute -> observe -> recover -> evaluate -> cleanup
```

任意非终态阶段触发安全失败、预算耗尽、Agent 失联或停止条件时，可以直接转入 `cleanup`。控制器不能依赖被测 Agent 自行恢复环境。

## ChaosBlade 安全门禁

一次故障动作必须满足：

- `run_id` 合法，并写入 `benchmark.run_id` 标签；
- namespace 在 Episode 白名单内；
- 目标是单个 Pod，且必须带有 Kubernetes UID；
- 不允许 selector 直接作为注入目标，避免漂移到多个副本；
- 故障类型、强度和持续时间必须在预算内；
- 每轮默认只允许一个活跃扰动；
- abort gate 与 cleanup gate 必须开启，清理必须按 `run_id` 和目标 UID 收敛，并验证本轮扰动不存在。

这层校验只决定“是否允许创建计划或执行动作”，真实效果、SLO 违反、因果机制和恢复结果仍由独立 `evaluator/` 判定。

## 本地验证

```bash
cd resiliencebenchmark
python -m unittest discover -s controller/tests -p 'test_*.py'
```
