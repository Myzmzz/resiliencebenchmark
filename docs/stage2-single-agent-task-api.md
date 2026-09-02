# Stage2 单智能体任务 API

## 接口

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/stage2/tasks` | 创建一个 `C0,D1,D2,D3,D4` 单智能体测试任务 |
| GET | `/api/v1/stage2/tasks/{task_id}` | 查询任务、Trial、交互、故障、扰动、评测与恢复状态 |
| POST | `/api/v1/stage2/tasks/{task_id}/abort` | 中止任务并执行完整恢复 |
| POST | `/api/v1/stage2/tasks/{task_id}/environment/reset` | 停止当前任务并重启被测应用 |
| POST | `/api/v1/stage2/tasks/{task_id}/permissions/restore` | 将 Agent 权限恢复到 Trial 基线或完全回收 |

## 创建参数

```json
{
  "application": "otel-demo",
  "prompt": "请执行受控故障实验。",
  "model": "gpt-5.6-sol",
  "harness": "codex"
}
```

当前应用仅支持 `otel-demo`；模型支持 `gpt-5.6-sol`、`claude-opus-5`；Harness 支持 `bladeai`、`codex`、`claude-code`、`deepseek-harness`。

## 查询参数

```text
after_sequence  默认 0，返回该序号及之后的事件
limit           默认 200，最大 1000
include_raw     默认 false；true 时返回更完整的 Prompt 和 Agent 输出
```

查询结果将 C0、D1-D4 分别放入 `trials`，每项包含：

- Agent 输入与最终响应；
- Harness 进程和输出解析状态；
- 主故障当前状态与历史注入情况；
- 扰动触发、应用和回滚状态；
- Agent、Harness、Controller、Oracle、Evaluator 事件；
- 逐规则评测结果；
- 权限恢复、环境重置和中止操作状态。

## 控制操作

控制操作均异步执行并返回 `202`。进度继续使用任务查询接口读取。

权限恢复支持：

```text
BASELINE  恢复到当前 Trial 初始能力
REVOKED   回收当前 Trial 的临时访问能力
```

环境重置或中止期间，不允许创建新的 Stage2 任务。

## Postman

导入：

```text
output/postman/Stage2-Single-Agent-Task.postman_collection.json
```

默认地址：`http://127.0.0.1:18088`。
