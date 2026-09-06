# 四智能体原生入口实测记录（2026-09-06 UTC）

## 结论与边界

已经进入真实模型运行：Codex、Claude Code、DeepSeek Harness 的只读闭环已通过；BladeAI 修复后的原生复验尚未完成。四智能体完整接入资格尚未全部通过，不能宣称正式扰动评测已通过。以下均为旧集群的只读入口检查，不是 C0、D7、D8，也不替代八个 D0 Campaign 或 68 格验收。未访问新集群。

范围：`coroot-config` / `kubernetes-admin@kubernetes`，应用命名空间 `otel-demo`，集成服务节点 `tcse-v100-03`。所有只读检查都关闭实际故障执行，并禁用 `chaos_control`；各次前后故障清单为空。

此前的 Chaos Mesh NetworkChaos 引擎金丝雀已真实执行，观测到延迟效果、业务恢复，并验证故障及临时 Pod 清理；证据为 `artifacts/remediation/20260905/chaos-mesh-canary-a1/final-adjudication.json`。这修正早期环境记录中的“尚未做金丝雀”，但不代表 D8 或其他故障类型已验证。

## 已提交的入口修复

| 提交 | 修复 | 验证边界 |
| --- | --- | --- |
| `607793f` | BladeAI 代理拒绝 Kubernetes 投影 token 链接；资格任务编号与 token registry 不匹配；资格入口读取错误的实验标识字段 | 全量 1455 通过、9 跳过；部署后以真实 Controller 身份查询 Pod，HTTP 200 |
| `02c7855` | 四家实际启动环境与执行代理白名单不一致；DeepSeek/BladeAI 继承了不必要的旧控制面字段；启动异常被缺少网关证据掩盖 | 全量 1463 通过、9 跳过；真实镜像中的三家版本/导入检查成功，Codex 暴露缓存链接问题 |
| `a54d566` | 不完整的原生总结被直接保存为有效 `agent-result.json` | 31 项相关回归通过；完整和不完整终态分别覆盖四家；部分判断保留为 assessment，不伪装为符合终态合同的结果 |
| `b9a9bee` | BladeAI worker 接入完整 MCP 生命周期；修正我方 blade 垫片缺少 Path 导入；验证并保留固定运行时缓存链接，失败时也完成安全权限归一化 | 全量 1478 通过、10 跳过；另在真实 BladeAI 0.3.0 / MCP 1.27 镜像中验证连接、调用、同循环执行、关闭和缺服务时禁止启动模型 |
| `cdaf1e9` | 支持 Claude 关闭原生工具所需的空参数，仍拒绝空 executable、NUL 和超限值；结果提交工具公开完整 v4 入参说明，保持参数包装与服务端判据 | 全量 1484 通过、10 跳过；实际四家 argv 与环境一起通过执行协议；工具 schema 的引用展开与原判据一致 |
| `e6fd44a` | 支持固定 Claude 客户端的精确 `/v1/messages?beta=true` 请求并保留 Anthropic 协议头；仍拒绝其它查询参数、模型或凭据覆盖 | 全量 1491 通过、10 跳过；有效转发与错误路径、重复/附加参数拒绝均有测试 |

最近核实的部署：主服务 `397a4f8`，集成服务 `e6fd44a`，e2e 仍为 `0f63bff`。本地后续提交不代表已经部署。原始失败记录不覆盖、不追改为通过。

## BladeAI：模型和受控读取已运行，原生 MCP 未接入

Trial：`campaign-native-readonly-bladeai-26d2569d`，05:29:22–05:32:54 UTC。

- 网关记录验证通过，12 个模型请求。
- 8 次 Kubernetes 只读代理查询，调用和结果均闭合；查询到了真实 cart Pod。
- 这些查询参数为 `requested_path`，是代理映射进统一审计的记录，不是模型自主调用外部 MCP 的证据。
- 直接 MCP 工具调用为 0，未出现 `harness_submit_result`。
- 原运行器生成的 `agent-result.json` 只有 `explicit_claims`，不符合终态 schema。原报告虽然写了 `completed`，只证明进程结束，不能用于资格升级。
- 故障清单前后为空，组件清理无报错。

固定 BladeAI release 的 L4 pool 调用 `create_agent(...)` 时没有创建或传入 `McpManager`；官方 CLI 则先连接 MCP，再把 manager 传入 factory。因此写对配置文件还不够，worker 必须补上实际初始化、执行及关闭的生命周期。该修复仍需独立 SDK 和原生验证。

后续 `b9a9bee` 已修复该调用链并通过独立 SDK 检查，尚未用新的模型运行替换上述失败历史；不能把 SDK 检查写成模型已自主调用 MCP。

证据目录：`artifacts/remediation/20260905/native-bladeai-26d2569d/`。其中 `adjudication.json` 单独标记 `platform_integration_incomplete`，原 `result.json`、`harness-report.json`、轨迹与网关记录保持原样。

## DeepSeek Harness：真实 MCP 往返与提交成功，目录回收失败

Trial：`campaign-native-readonly-deepseek-harness-dfbdb11c`，05:34:58–05:36:45 UTC。

服务端记录显示：两次 `k8s_list_resources` 查询；五次 `harness_submit_result` 调用，前面的不合格提交收到校验错误后继续修正，最后的提交符合终态 schema。它确实能在同一次 headless 运行中收到工具反馈后继续调用工具，无需会话续接。

这仅证明相应交互行为和结构验证，不等于证据语义、完整资格或扰动用例通过。实际末尾先出现执行代理异常，随后 Controller 回收工作目录时又报 `PermissionError`，最终未交付完整 Harness 报告。故障清单仍为空，但不能把组件 `cleanup_errors=[]` 扩大解释成工作目录也已回收。

已查明其 `profiles/node_modules` 内存在指向固定安装目录的包链接，部分 Agent 自建目录仍为 2700。共享目录归一化遇到链接提前中断，使 Controller 不能完整读取归档和回收目录。需要支持经过严格校验的运行时缓存，同时保持不跟随链接访问私有文件的边界。

后续已用修正后的 no-follow 归一化逻辑恢复该次目录的可读/可清理状态，归档 234 条脱敏原生会话记录，并于 06:11:25 UTC 验证临时工作目录不存在。证据为 `deepseek-dfbdb11c-workdir-cleanup.json` 与该 Trial 的 `recovered-native-trace/`；这不追改原次运行失败的判定。

### DeepSeek 修复后只读闭环通过

`b9a9bee` 上的 `campaign-native-readonly-deepseek-harness-9806cb28` 于 06:19:37 UTC 完成。9 个模型请求、3 次真实 MCP Pod 查询、6 次提交尝试，最终结果符合 schema；网关证据验证通过，无 Harness 错误，临时目录已不存在，前后故障清单为空。证据摘要：`deepseek-9806cb28-summary.json`。

这是只读技术闭环通过，不是完整 WP11、D0 或扰动通过。多次提交修正也暴露出工具原本只公开泛型 `result: object`；`cdaf1e9` 已让四家从工具列表获得完整字段约束，不通过额外提示或改变判据弥补。

## Codex 与 Claude Code

早期真实镜像版本/导入检查记录在 `native-launch-contracts-all-02c7855.jsonl`：BladeAI、Claude Code、DeepSeek Harness 成功；Codex 自身创建的 `tmp/arg0` 可执行别名链接被归一化规则拒绝。版本检查不调用模型、不证明 MCP 资格。当时 Claude Code 尚未执行真实任务，Codex 尚未进入模型运行。

修复后的 `native-launch-contracts-b9a9bee.jsonl` 在 06:16:48 UTC 记录四家全部成功，所有检查通过真实执行代理。这仍只是版本/导入检查，不能代替各自的任务运行。

后续 Codex 的 `campaign-native-readonly-codex-22c43721` 于 06:36:00 UTC 完成真实只读闭环：12 个模型请求、14 次 MCP 调用，网关证据、终态 schema、Harness 状态和清理均通过。原始证据归档在 `native-codex-22c43721/`。

Claude 的首次任务 `campaign-native-readonly-claude-code-e0e9514d` 在模型前被执行协议拒绝：合法参数 `--tools ""` 含空字符串。该问题已在 `cdaf1e9` 修复，未通过开放原生工具绕开限制。第二次 `campaign-native-readonly-claude-code-527e9bad` 则被转发层拒绝 `query_parameters_not_allowed`；固定客户端实际使用 `/v1/messages?beta=true`。`e6fd44a` 已精确修复这一路径，不开放任意 query；新的原生复验结果另列。

第三次 `campaign-native-readonly-claude-code-e8b93565` 在 `e6fd44a` 上于 06:55:31 UTC 完成真实只读闭环：6 个模型请求、2 次 MCP Pod 查询、2 次结果提交，终态 schema 有效、网关证据验证通过、无 Harness 错误。前后故障清单为空，临时工作目录已不存在。证据归档在 `native-claude-e8b93565/`。这是只读技术闭环，不是正式扰动通过。

## 尚未完成的准入与验收

原任务 API 的 C0 仍要求 Harness 的真实资格记录。只读 smoke 明确不写资格文件，不能手工把 `qualification_passed` 改成 true。现有完整 WP11 检查还包含 Coroot、沙箱、带内通知与结果提交，尚未通过。

后续需完成：BladeAI 新版本的原生模型复验；四家完整资格及其证据接入；Coroot 独立 Viewer 准备；旧服务剩余部署；八个 D0 与 68 格验收。缓存与回收修复已经在上述通过的只读运行中验证，但不能据此替代完整资格。正式任务仍按单智能体、单扰动通过原接口逐项执行，不以这些诊断记录代替。
