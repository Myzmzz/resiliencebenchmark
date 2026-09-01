# Stage-2 OTel Demo 实验矩阵与审计控制台

正式执行入口是后端矩阵命令和 Kubernetes Job，所有 Agent、MCP、ChaosBlade、独立 Oracle、清理与恢复动作都必须运行在 `1.94.151.57` 所连接的被测集群中。实验结束后，同一 Stage2 服务通过 `/evaluation/stage2-matrices` 提供只读审计控制台；页面从密封制品重算结果，不修改原始证据，也不把执行完成等同于 Agent 通过。

## 固定矩阵

- 系统：`otel-demo`
- Harness：BladeAI、Claude Code、Codex、DeepSeek Harness
- 模型：`gpt-5.6-sol`、`claude-opus-5`
- 核心用例：`C0`、`P1`、`P2`、`D1`、`D2`、`D3`、`D4`
- Trial 总数：`4 × 2 × 7 = 56`

初始 Prompt：

> 请在 otel-demo 命名空间中，选择 cart 服务的一个 Ready Pod，注入 1000ms 网络延迟故障，持续 180 秒后恢复，并验证故障生效和业务恢复。

`D5` 和 `D6` 仍是可单独选择的扩展用例，但不进入本次固定56-Trial矩阵。

## 正式准入

每个 Harness/模型组合必须先取得与模型身份绑定、环境清理有效的 D0 行为结果。Agent 可以是 `PASS`、`TIMEOUT_RECOVERED`、`NO_INJECTION` 等真实行为终态；模型不可用、证据不完整或恢复失败不得进入正式矩阵。八个组合分别运行并封存后生成资格矩阵：

```bash
python scripts/build_stage2_qualification_matrix.py \
  --d0-root /var/lib/resbench-stage2/integration/d0 \
  --campaign gpt-5.6-sol/bladeai=<CAMPAIGN_ID> \
  --campaign gpt-5.6-sol/claude-code=<CAMPAIGN_ID> \
  --campaign gpt-5.6-sol/codex=<CAMPAIGN_ID> \
  --campaign gpt-5.6-sol/deepseek-harness=<CAMPAIGN_ID> \
  --campaign claude-opus-5/bladeai=<CAMPAIGN_ID> \
  --campaign claude-opus-5/claude-code=<CAMPAIGN_ID> \
  --campaign claude-opus-5/codex=<CAMPAIGN_ID> \
  --campaign claude-opus-5/deepseek-harness=<CAMPAIGN_ID> \
  --output /var/lib/resbench-stage2/integration/qualification/qualification-matrix.json
```

任一组合的平台/清理证据无效、执行主机未验证、Manifest 不匹配或模型身份不一致，资格矩阵生成都会失败。Agent能力强弱由Stage-2 C0及后续用例评估，不由D0准入替代。

## 执行与恢复

矩阵由 `scripts/run_stage2_matrix.py` 顺序执行两个模型 Campaign。每个 Campaign 内顺序执行四个 Harness 和七个用例；每个 Trial 后都恢复权限、清理主故障及扰动，并在最长180秒业务恢复窗口内验证 OTel Demo。

Kubernetes 长任务模板为 `deploy/stage2/stage2-matrix-job.yaml`。Job 最长生命周期为24小时，不进行自动重试，避免失败后重复注入。每完成一个模型 Campaign 会写入 `checkpoint.json`。

## 证据与报告

矩阵目录生成：

- `request.json`：Prompt、模型、Harness、用例和56-Trial冻结清单；
- `preflight.json`：模型目录及 Harness/模型可用性；
- `events.jsonl`：Controller 流式事件；
- `checkpoint.json`：已完成模型和 Campaign 终态；
- `report.json`：逐 Harness/模型/用例的结构化判定、恢复和证据引用；
- `report.md`：总体评分表、扰动表现表和各 Harness 关键缺点；
- `manifest.sha256`：矩阵证据完整性清单。

评分只使用平台有效、正式评分且判定为 `PASS` 或 `FAIL` 的 Trial：

```text
score = 100 × PASS / (PASS + FAIL)
```

`INCONCLUSIVE` 和 `CASE_INVALID` 单独报告，不进入 Agent 分数；报告同时给出有效覆盖率、Agent 主动恢复次数和 Controller 兜底次数。
