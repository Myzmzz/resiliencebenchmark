# Stage2 单智能体任务 API

## 接口

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/stage2/options` | 查询当前可选系统、Harness/Agent、模型矩阵、主故障、安全预算、模式和扰动选项 |
| GET | `/api/v1/stage2/cases` | 查询 C0、D1-D6 的人话说明、触发条件、Agent 目标、Oracle 和 reset 语义 |
| GET | `/api/v1/stage2/autonomy/cases` | 查询 L0-L4 自主性分级的手测 Prompt、Oracle 和推荐 POST body |
| POST | `/api/v1/stage2/tasks` | 按选择创建单项或多项 `C0,D1,D2,D3,D4,D5,D6` 单智能体测试任务 |
| GET | `/api/v1/stage2/tasks` | 列出已有任务，便于手动验证时找回最近任务 |
| GET | `/api/v1/stage2/tasks/{task_id}` | Summary 模式，查询任务、Trial、交互摘要、故障、扰动、评测与恢复状态 |
| GET | `/api/v1/stage2/tasks/{task_id}?mode=timeline` | Timeline 模式，返回简化分类事件流 |
| GET | `/api/v1/stage2/tasks/{task_id}?mode=debug` | Debug 模式，返回完整脱敏事件和更完整的 Agent 运行材料 |
| POST | `/api/v1/stage2/tasks/{task_id}/abort` | 中止任务并执行完整恢复 |
| POST | `/api/v1/stage2/tasks/{task_id}/environment/reset` | 停止当前任务并重启被测应用 |
| POST | `/api/v1/stage2/tasks/{task_id}/permissions/restore` | 将 Agent 权限恢复到 Trial 基线或完全回收 |

## 创建参数

```json
{
  "application": "otel-demo",
  "target": {
    "namespace": "otel-demo",
    "component": "cart",
    "resolution": "single-ready-pod"
  },
  "prompt": "请执行受控故障实验。",
  "model": "gpt-5.6-sol",
  "harness": "codex",
  "main_fault": {
    "fault_type": "cpu-load",
    "duration_seconds": 300,
    "intensity": {"cpu_percent": 80}
  },
  "prompt_mode": "compiled",
  "interaction_mode": "guided",
  "autonomy_level": "L0_COMPLETE_TASK",
  "d6_variant": "D6-A",
  "cases": ["C0", "D1", "D2", "D3", "D4", "D5", "D6"]
}
```

当前端到端 Stage2 Episode/runtime 只有 `otel-demo` 可运行。`GET /api/v1/stage2/options` 会把 `otel-demo` 标为 `runnable=true`；`train-ticket` 和 `sock-shop` 可以出现在候选列表里，但会标为 `runnable=false`，原因是还缺少 Stage2 Episode 和 runtime adapter。`POST /api/v1/stage2/tasks` 仍只接受真正可运行的系统；传不可运行系统会得到 422。

接口中的智能体选择字段沿用现有名称 `harness`：`codex`、`claude-code`、`deepseek-harness` 或 `bladeai`。可选模型及每个 Harness/模型组合当前是否可运行，以 `/api/v1/stage2/options` 返回的 `model_matrix` 为准，不能只凭模型名称判断。

`prompt_mode`、`interaction_mode`、`autonomy_level`、`d6_variant`、`cases` 和 `disturbance` 都是可选字段。默认使用全套 `C0,D1,D2,D3,D4,D5,D6`，并采用 `compiled`、`guided`、`L0_COMPLETE_TASK` 和 `D6-A`。

`main_fault` 是独立的可执行合同，不从 `prompt` 猜测，也不再从固定 Episode 继承：

- L0-L2 必须提供 `main_fault`；缺少时直接返回 422；
- L3-L4 必须省略 `main_fault`，由 Agent 在 Controller 发布的安全策略空间内选择；
- `fault_type`、`duration_seconds` 和 `intensity` 必须同时通过 Controller 预算校验，系统不会静默截断或替换参数；
- 当前可用故障及各自强度字段以 `GET /api/v1/stage2/options` 的 `main_faults` 为准。

这是有意的 fail-closed 合同变更。过去只有自然语言 Prompt 的 L0-L2 请求不再接受，避免用户写 `cpu-load`、Controller 却执行 `network-delay` 的错位结果。

`main_fault` 与 `disturbance` 不是一回事：前者定义本轮真正注入的 CPU、内存、延迟或丢包故障；后者定义 D1-D6 在执行过程中额外制造的权限、目标、观测、通道或操作结果扰动。

`target` 也是显式合同，不从 Prompt 或固定 Episode 选择。调用方提交逻辑目标
`namespace + component`，Controller 每轮再解析当前唯一 Ready Pod 名称和 UID。
当前 `GET /api/v1/stage2/options.targets` 只会把已有独立 Oracle 的
`otel-demo/cart` 标为可运行；未登记目标返回 422，不会回退到 cart。

单项测试有两种写法：

```json
{ "cases": ["D2"] }
```

或：

```json
{ "disturbance": "D2" }
```

`disturbance` 是单值快捷入口，可选 `none,D1,D2,D3,D4,D5,D6-A,D6-B`。`none` 映射到 `C0`；`D6-A` 和 `D6-B` 都映射到 `D6`，同时自动设置对应 `d6_variant`。如果同时传 `cases` 和 `disturbance`，二者必须选择同一个单项 case，例如 `cases:["C0"]` 可以配 `disturbance:"none"`，`cases:["D6"]` 可以配 `disturbance:"D6-B"`；`cases:["D2"]` 配 `disturbance:"D3"` 会返回 422。

`autonomy_level` 是端到端评测目标，不只是展示标签。它会写入任务、传入 Campaign/runtime，并在 Summary 的 `autonomy_result` 中聚合为 `level/mode/eligible/agent_outcome/reason_codes`。如果 Harness 给了 `SEMANTIC_NUDGE` 或需要人替 Agent 做目标、故障、参数、恢复条件选择，`autonomy_result.eligible` 应变为 `false`。

## 查询参数

```text
mode            默认 summary；可选 summary、timeline、debug
after_sequence  默认 -1，只用于 timeline/debug，返回该序号之后的事件
limit           默认 200，最大 1000
```

查询结果只展示本次任务选择的 case。默认任务会把 C0、D1-D6 分别放入 `trials`；单项任务只返回对应一个 trial。每项包含：

- Agent 输入与最终响应；
- Harness 进程和输出解析状态；
- 主故障当前状态与历史注入情况；
- 扰动触发、应用和回滚状态；
- Agent、Harness、Controller、Oracle、Evaluator 事件；
- 逐规则评测结果；
- 权限恢复、环境重置和中止操作状态。

Summary 会聚合结构化反馈，重点看三类信息：

- `facts`：Harness/Controller 告诉 Agent 的事实，例如目标已替换、能力已重绑定、通道已恢复；
- `auth_confirmations`：对原始 Prompt 范围内继续执行的确认；
- `semantic_nudges`：推动 Agent 继续执行的语义提示；出现这类事件时，结果应按 assisted，而不是完全自主。

Timeline 只返回简化分类事件，适合手动轮询。Debug 返回完整脱敏事件，适合排查 Harness 与 Agent 的具体交互，但不会暴露 token、secret、password、authorization、cleanup handle 或 operation ID 等受控运行标识。

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

如目标主机禁止 TCP 转发，可在仓库根目录启动本地代理：

```bash
source /Users/mymz/.bashrc
export SSHPASS="$node1pwd"
uv run python scripts/stage2_postman_proxy.py
```

代理只监听 `127.0.0.1`，不会把实验控制接口暴露到外部网络。
