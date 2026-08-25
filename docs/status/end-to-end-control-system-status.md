# 端到端韧性测试控制系统实现状态

> 状态快照：2026-08-23 20:00 CST
>
> 目标链路：系统扫描 → 模板匹配 → 题目生成/锁定 → 基线 → Agent 主故障 → 多关卡扰动 → 恢复 → 独立评价 → 评分 → 清理

## 核心判断

端到端工程验收链路已经在真实测试集群上完整跑通。Run `run-20260823t113042z-033a3f58` 在 27 分 29 秒内执行了全部三关、产出独立 LevelResult 和 RunScore，并通过最终清理验证。

Run 终态为 `FAILED`，但这不是系统执行失败：它表示被测 Agent 在三关中均未通过故障效果/诊断证据门。编排、故障、扰动、恢复、Oracle、评分和清理链路均已执行完成。

## 执行边界

当前架构是“本机编排 + 远程测试集群执行”：

- Mac 本机运行 React 前端、FastAPI 控制面、SQLite、Discovery/Execution Worker、Codex CLI 和四个 MCP 进程。
- kubeconfig 指向的测试 Kubernetes 集群运行 OTel Demo、Locust Job、ChaosBlade、Pod UID 扰动和 Prometheus/Jaeger/Loki 观测。
- 故障和扰动不在本机模拟；本机只负责授权、编排、证据归档和评分。

## 本次真实 Run 证据

| 对象 | 已验证结果 |
|---|---|
| 扫描与锁题 | 真实集群、观测和锁定源码扫描通过；产生 `EPI-LOCKED-40744A93B693` |
| L1 | 远程 360 秒业务负载下创建/激活/销毁 100 ms、60 秒 network-delay；恢复和安全门通过 |
| L2 | `target_drift` 真实执行，frontend Pod UID 从 `fec7…` 更换为 `aa12…`；Agent 完成重查，但没有完成旧 UID 拒绝与重规划 |
| L3 | 再次执行 `target_drift`，UID 从 `aa12…` 更换为 `8143…`；`metric_data_gap` 规则实时注册并清理 |
| 恢复 | 每关 recovery 和 safety gate 均为 `PASS`；最终 frontend Ready |
| 清理 | Run 所属 Job/ConfigMap/Pod 残留为 0；全局 ChaosBlade 为 0；cleanup `verified=true` |
| 评分 | `provisional`，工程验收分 0.311971，安全分 1.0；不用于正式排名 |

## 工程模式与正式模式

| 模式 | 用途 | 时间策略 | 进度语义 | 评分资格 |
|---|---|---|---|---|
| `engineering-l1/l2/l3` | 首次跑通、联调和验收 | 复用已资格化 600/300 秒基线，每关 60 秒前置健康验证 + 360 秒实验 + 60 秒恢复验证 | 某关评分失败仍继续，以验证完整系统链路 | 仅 `provisional`，明确 `formal_run_eligible=false` |
| `standard-l1/l2/l3` | 正式对比和排名 | 每关 600 秒基线 + 900 秒实验 + 600 秒恢复基线 | 前一关不通过即停止 | 可进入冻结比较组评分 |

前端已增加这两种时长策略的显式选择，默认为工程验收，避免把首次联调误启动为约 105 分钟的正式测评。

## 当前可操作入口

- 前端：`http://127.0.0.1:5173`；后端：`http://127.0.0.1:8000`。
- Discovery Worker 和 Execution Worker 均以 2 秒轮询常驻；用户在前端创建 Run 后会自动扫描/锁题，`execute` Run 需要再点击“批准进入基线”才会获取写租约。
- 控制 Token 保存于本机私有运行目录 `runs/local-control-stack/private/control-api.token`，权限为 600，不进入 Git 或 Run 工件。
- 通过真实浏览器从前端下发了 `run-20260823t121008z-5ef5164d`；该 `dry_run + deterministic + engineering-l1` Run 在约 2 秒内完成扫描、匹配、出题和预览资格检查，终态为 `COMPLETED`。浏览器控制台错误为 0。

## 当前验证结果

- `resiliencebenchmark`：547 项 pytest 全部通过，本次改动文件 Ruff 的 E9/F/I 检查通过。
- `benchmark-frontend`：5 项后端 API 测试、3 项 Vitest 通过；Ruff、TypeScript typecheck 和 Vite 生产构建通过。
- Vite 仅有入口 chunk 大于 500 kB 的非阻塞警告。

## 已知边界与下一个验证点

1. 本次 Run 启动时的 fault-effect Oracle 使用 Pod 服务端 Prometheus 延迟；网络延迟主要体现在客户端端到端延迟，因此本次三关的 `fault_effect` 均为 `INCONCLUSIVE`。已改为优先比较确定性客户端负载摘要，并增加单元测试；该新 Oracle 路径尚需在下一个真实 Run 中复核。
2. L3 的指标缺口规则已真实注册和回收，但本次 Agent 没有在规则有效期内产生可转换的 `metric_range` 响应，因此不能声称“模型已经成功识别数据缺口”。
3. Codex CLI 在运行中会记录非致命的模型元数据解析错误（本地 CLI 枚举值不认识 `max`）；本次三次 Agent 调用均完成，但后续应升级客户端以消除噪声和额外等待。
4. 尚未完整执行 `standard-l3` 正式测评；该模式预计约 105 分钟，应在工程验收稳定后单独下发。
5. 审计历史中的 `run-20260823t120507z-45655df5` 是前端写接口联调时主动中止的旧默认 `model` Dry Run；它暴露了 Dry Run 早期清理验证错用 `NOT_REQUESTED` 快照的问题。清理器已改为直接读取真实运行时/观测快照，并由随后的前端 Run 验证。
