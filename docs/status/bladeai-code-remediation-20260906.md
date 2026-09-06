# BladeAI 代码整改交付

状态：代码整改；用户已要求暂停测试与部署。本轮只做源码核对、代码和测试代码
修改、静态差异检查。没有运行 pytest、编译、镜像构建、模型调用或集群故障。
以下不代表 BladeAI 已通过真实资格，不更新现网能力文件。

## 已处理的阻断

| 层次 | 实际问题 | 本轮处理 |
|---|---|---|
| 请求入口 | CampaignRequest 仍永久拒绝 BladeAI 自主目标/故障选择 | 删除旧特判；仍由资格、权限与执行合同决定能否进入任务 |
| MCP 工具守卫 | SDK 将动态 MCP 工具归为 UNKNOWN，连确认/提交工具也可能被挡住 | 在隔离 worker 内使用“仓库工具目录 ∩ 本轮实际已连接工具”的明确名单；保留其他目标守卫和服务端授权 |
| SDK 生命周期 | 初始化或执行失败后清理不完整，MCP 图初始化异常可能遗留连接 | 同一个事件循环内管理图与 MCP，异常/取消也尝试清理，恢复临时 SDK 补丁 |
| 确认 | 旧提案字段可能串入下一轮，SDK 回调与 Controller 调用缺少关联 | 提案消费后清空；确认事件关联真实 controller_call_id，错误不默认为批准 |
| 工具事件 | 普通 step/finish 被误计为工具；SDK 截断输入输出、丢调用 ID | 用原始 LangGraph run_id/数据和实际 callback 采集工具往返；控制事件作为 Checkpoint；恢复图保留原 ainvoke 语义 |
| 故障时长 | SDK 将显式短时长自动扩大到至少 600 秒 | 在 worker 适配边界保留显式整数时长；未指定时长仍由 SDK 提议，最终均受 Controller 确认与执行合同约束 |
| 创建/对账 | 结果未知被包装成创建成功，本地缓存被当作当前状态 | OPERATION_OUTCOME_UNKNOWN 明确非成功并保留操作 ID；真实查询区分 Absent、Destroyed 与未知状态 |
| 受控命令 | SDK 的 status/query/kubeconfig 参数形态无法完整映射 | 补齐到受控 MCP 的命令路由；不执行原生 kubectl exec、不给集群凭据、不扩 RBAC |
| 可选代码窗口 | BladeAI MCP 模板没有 code_sandbox | 仅在当前用例实际配置时渲染；不改变 L0–L4 默认能力或 D7/D8 总体准入 |
| 全链资格 | BASE 不能证明 BladeAI 的创建/销毁，但缺少独立晋级链 | 增加 WP8 runner、证据评估器、归档读取/重算及发布接线；不把 BASE 自动升级 |

这里的 SDK READONLY sentinel 只表示不直接操作本地 K8s 目标。Harness 通道和
沙箱的实际状态写入仍经过 Controller；未知 MCP 工具、未连接工具、直连
chaos_control/chaos_mesh_control 不因此放行。没有关闭整体 target_guard。

## 资格证据边界

Controller 在启动前记录 `bladeai-launch.json`。受控 shim 保存实际返回的
调用 ID、操作 ID 与目标信息，Controller 收集为 `bladeai-shim-evidence.json`。
这些文件不是单独的成功证明：发布时必须重新对齐原始 MCP 调用/结果、SDK
确认、通知回执、有效结果提交、独立清理与业务恢复，以及当前网关记录。
所有必需文件须属于同一 Trial 归档；修改 `passed=true` 或补一个
`via_shim=true` 不能晋级。

WP8 入口为 `scripts/qualify_bladeai_task.py`，必须显式 `--execute`，只接受已有、
带 `resiliencebenchmark.io/qualification=bladeai-wp8` 标签的 Ready canary Pod。
它不部署或删除 Pod，不自动发布资格。当前资格故障合同为 network-delay、
30 秒、1ms，由 MCP 的执行合同实际限制，而不只是写在提示里。SDK 接收的仍是
task 模式，不含 managed_fault 或预选 target。该资格说明不是 L1 自主评分，
也不替代 D0 或 D7/D8。

运行器归档完成后，可由 `scripts/build_bladeai_qualification.py` 重新生成资格
记录，再交给已有 `scripts/publish_harness_capabilities.py`。这些入口本轮都未执行。
缺任何必需证据，BladeAI 保持未资格通过。

## 未执行与保留边界

- 新增和更新的测试代码尚未运行；静态差异检查不能证明运行正确。
- 真实模型、SDK 镜像、canary 全链、D1–D8 与 L0–L4 回归均待恢复测试后验证。
- 没有修改 L0–L4 Prompt 文件、节点评分表或默认 C0–D6 用例集合。
- 没有修改 upstream BladeAI 源仓或安装环境，也没有操作新集群。
- 旧 SDK 的跨 namespace 工具 Pod 发现没有被授予新权限；正常受控 shim 路径
  不依赖这项发现。主路径失败时，不能借旧兜底扩大权限。
- 原临时 `127.0.0.1:18080` 转发已按暂停测试要求关闭。

改动范围说明：新增 BladeAI 专属事件、守卫、时长边界与资格模块，是为了闭合
WP8 的生产调用链；公共 contracts、runtime、finalization 和 publisher 仅做
必要入口、身份/证据接线。新增维护脚本已列入镜像构建输入，但本轮没有构建或部署。
