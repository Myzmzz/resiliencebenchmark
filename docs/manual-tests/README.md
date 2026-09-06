# 四智能体逐项 Postman 参数

每次只复制一个 JSON 文件，使用 `POST {{base_url}}/api/v1/stage2/tasks`，Body 选 raw → JSON，Header 为 `Content-Type: application/json`。这里没有批量执行脚本，也没有自动发起任务。

模板共 68 份（4 家 × 17 项），不是 68 条已执行结果。请求前先在同一个 `base_url` 上查询 `GET /api/v1/stage2/options`，确认当前 Harness/model/case 可运行；D7/D8 要等四家所需能力与替代服务全部就绪，不能靠修改请求绕过准入。

`harness` 是被测框架，`model` 是模型别名；这里统一以 `gpt-5.5` 为初始值，不代表 DeepSeek Harness 必须使用 DeepSeek 模型。切换模型时以实时可用列表为准，并在结果中记录实际模型。

L0–L4 请求直接采用服务现有 `recommended_post_body`，只替换 Harness，没有改 Prompt、交互方式、决策策略或预期结果。它们是五类提示用例，不是额外扰动编号。C0–D8 沿用同一个自然语言网络延迟任务，只改变 `disturbance`；A/B 变体由接口转换，不要求用户填写权限 Profile 或原生工具权限。

| 用例 | Codex | Claude Code | DeepSeek Harness | BladeAI |
|---|---|---|---|---|
| L0 | [JSON](requests/codex/L0.json) | [JSON](requests/claude-code/L0.json) | [JSON](requests/deepseek-harness/L0.json) | [JSON](requests/bladeai/L0.json) |
| L1 | [JSON](requests/codex/L1.json) | [JSON](requests/claude-code/L1.json) | [JSON](requests/deepseek-harness/L1.json) | [JSON](requests/bladeai/L1.json) |
| L2 | [JSON](requests/codex/L2.json) | [JSON](requests/claude-code/L2.json) | [JSON](requests/deepseek-harness/L2.json) | [JSON](requests/bladeai/L2.json) |
| L3 | [JSON](requests/codex/L3.json) | [JSON](requests/claude-code/L3.json) | [JSON](requests/deepseek-harness/L3.json) | [JSON](requests/bladeai/L3.json) |
| L4 | [JSON](requests/codex/L4.json) | [JSON](requests/claude-code/L4.json) | [JSON](requests/deepseek-harness/L4.json) | [JSON](requests/bladeai/L4.json) |
| C0 | [JSON](requests/codex/C0.json) | [JSON](requests/claude-code/C0.json) | [JSON](requests/deepseek-harness/C0.json) | [JSON](requests/bladeai/C0.json) |
| D1 | [JSON](requests/codex/D1.json) | [JSON](requests/claude-code/D1.json) | [JSON](requests/deepseek-harness/D1.json) | [JSON](requests/bladeai/D1.json) |
| D2 | [JSON](requests/codex/D2.json) | [JSON](requests/claude-code/D2.json) | [JSON](requests/deepseek-harness/D2.json) | [JSON](requests/bladeai/D2.json) |
| D3 | [JSON](requests/codex/D3.json) | [JSON](requests/claude-code/D3.json) | [JSON](requests/deepseek-harness/D3.json) | [JSON](requests/bladeai/D3.json) |
| D4 | [JSON](requests/codex/D4.json) | [JSON](requests/claude-code/D4.json) | [JSON](requests/deepseek-harness/D4.json) | [JSON](requests/bladeai/D4.json) |
| D5 | [JSON](requests/codex/D5.json) | [JSON](requests/claude-code/D5.json) | [JSON](requests/deepseek-harness/D5.json) | [JSON](requests/bladeai/D5.json) |
| D6-A | [JSON](requests/codex/D6-A.json) | [JSON](requests/claude-code/D6-A.json) | [JSON](requests/deepseek-harness/D6-A.json) | [JSON](requests/bladeai/D6-A.json) |
| D6-B | [JSON](requests/codex/D6-B.json) | [JSON](requests/claude-code/D6-B.json) | [JSON](requests/deepseek-harness/D6-B.json) | [JSON](requests/bladeai/D6-B.json) |
| D7-A | [JSON](requests/codex/D7-A.json) | [JSON](requests/claude-code/D7-A.json) | [JSON](requests/deepseek-harness/D7-A.json) | [JSON](requests/bladeai/D7-A.json) |
| D7-B | [JSON](requests/codex/D7-B.json) | [JSON](requests/claude-code/D7-B.json) | [JSON](requests/deepseek-harness/D7-B.json) | [JSON](requests/bladeai/D7-B.json) |
| D8-A | [JSON](requests/codex/D8-A.json) | [JSON](requests/claude-code/D8-A.json) | [JSON](requests/deepseek-harness/D8-A.json) | [JSON](requests/bladeai/D8-A.json) |
| D8-B | [JSON](requests/codex/D8-B.json) | [JSON](requests/claude-code/D8-B.json) | [JSON](requests/deepseek-harness/D8-B.json) | [JSON](requests/bladeai/D8-B.json) |

`C0` 的 `disturbance=none` 仅表示不附加权限/目标/工具扰动；仍会按 Prompt 创建主故障。L4 按现有合同期待安全拒绝，不授权实际扩大影响范围。

所有请求都不要求用户填写 Episode、schema_version、request_id、permission_profile 或 bladeai_native。任务 ID 由服务生成；权限由用例决定。`qualification.mode=diagnostic` 的任务可用于真实调试，但不能当作正式矩阵成绩。

提交一项后保存 task_id，先看 Summary，再按需要查看 `?mode=timeline`、`?mode=debug`；后两者按分页游标读取完整记录。未结束前不要再次提交或切到下一个扰动。有问题保留任务 ID，必要时使用该任务的 `/abort`，并核对实际清理与恢复结果。
