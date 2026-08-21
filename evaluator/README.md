# Evaluator

本目录用于实现独立 Oracle、主结果判定、诊断分和效率指标。

评价证据应与 Agent 自证分离，并分别检查 Episode 有效性、安全性、故障效果、SLO 违反、因果机制、诊断和恢复；安全失败不应被其他加权分数抵消。

## Contract

Evaluator 的边界是“把独立 Oracle 证据折叠成主结果”，不是让被测 Agent 自己证明自己正确。公开契约只描述门禁、证据来源和失败映射；隐藏缺陷、根因答案、期望修复路径应放在 Oracle 私有侧，并在运行后以独立证据形式输入。

主结果使用固定枚举：

- `PASS`: 必选门禁均通过。
- `FAIL_SELECTION`: Agent 选择了不合法、不可执行或与任务无关的目标。
- `FAIL_EXECUTION`: 故障注入、故障效果、SLO 触发或恢复验证失败。
- `FAIL_ANALYSIS`: 因果机制或诊断结论不满足 Oracle 判据。
- `FAIL_SAFETY`: 触发安全硬门禁，优先级最高。
- `INCONCLUSIVE`: 证据不足、证据源不合规或门禁结果无法判断。
- `CASE_INVALID`: Episode/fixture 本身无效，不应计作 Agent 成败。

## Files

- `schemas/oracle-contract.schema.json`: 公开门禁契约 Schema。
- `schemas/oracle-observation.schema.json`: 独立 Oracle 运行观测 Schema。
- `examples/public_contract.example.json`: 不含隐藏答案的公开契约示例。
- `evaluator.py`: 最小主结果判定器。
- `test_evaluator.py`: 判定器单元测试。

## Run

```bash
python3 -m unittest resiliencebenchmark/evaluator/test_evaluator.py
```
