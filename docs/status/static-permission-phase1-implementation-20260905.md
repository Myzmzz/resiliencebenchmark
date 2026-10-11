# 四类被测智能体静态权限控制第一阶段实施状态

> 状态：代码实现完成，本地单元验证通过，远端手动集成待执行。
> 日期：2026-09-05。
> 本轮未构建或部署远端 Stage2，未启动被测 Agent，未执行真实故障实验。

## 1. 本阶段完成范围

本阶段实现“权限在 Trial 启动前确定”的静态权限控制，不实现运行中动态撤权、MCP Server 内逐工具授权或正式扰动 Campaign。

已完成：

1. `stage2-permission-profile.v1` 权限契约与 Schema；
2. P0-P5 六个静态权限 Profile；
3. Campaign Request 携带完整 `permission_profile`；
4. `trial_kinds: [control]` 可只执行无额外 Controller 扰动的 Control Trial；
5. Permission Manager 按 Profile 编译每个 Harness 的有效权限；
6. MCP Supervisor 只启动本轮选择的 MCP Server；
7. Codex、Claude Code 和 DeepSeek Harness 的 Trial 配置按 Profile 裁剪；
8. BladeAI 改用 Trial 独立 Server 和 Session/Turn/SSE；
9. BladeAI 使用独立 Home、MCP 配置、ServiceAccount、kubeconfig 和原生权限；
10. 每个 Trial 保存 `permission-surface.json`；
11. Trial 结束清理临时 Home、MCP Token 状态、kubeconfig 和 RBAC；
12. Stage2 API 提供 `GET /api/v1/permission-profiles`。

## 2. 六个内置 Profile

| Profile | MCP 变化 | BladeAI 原生权限变化 |
| --- | --- | --- |
| `p0-full-authorized` | 四个 MCP 均允许 | Kubernetes 读取、Metrics、ChaosBlade 均允许 |
| `p1-no-telemetry-mcp` | 移除 `telemetry_ro` | 原生 Metrics 保留 |
| `p2-no-k8s-mcp` | 移除 `k8s_ro` | 原生 Kubernetes 读取保留 |
| `p3-read-only` | 移除 `chaos_control` | 原生 ChaosBlade 执行关闭 |
| `p4-no-native-k8s-metrics` | MCP 保持 | 原生 Metrics 关闭 |
| `p5-no-mcp` | 移除全部 MCP | BladeAI 原生能力保留 |

BladeAI 当前不接入 `chaos_control` MCP；其故障执行继续走原生 ChaosBlade，并由 `bladeai_native.chaosblade_execute` 对应的 RBAC 控制。其他三个 Harness 按 Profile 使用 `chaos_control` MCP。

## 3. 四类 Harness 的配置结果

### BladeAI

- 创建 Trial 独立 `HOME`；
- 配置写入 `$HOME/.blade-ai/mcp.json`；
- 只读 MCP 挂到 `clarification` 和 `verifier`；
- 使用独立 BladeAI Server 和 Session/Turn/SSE；
- 不继承宿主机 `KUBECONFIG` 或全局 BladeAI MCP 设置；
- 原生 Kubernetes Read、Metrics 和 ChaosBlade 分别由 RBAC 控制。

### Codex

- 使用 Trial 独立 `CODEX_HOME`；
- 未授权 MCP Section 不进入 `config.toml`；
- 非 MCP 执行工具继续保持关闭；
- `p5-no-mcp` 不向进程传递 MCP Token。

### Claude Code

- 使用 Trial 独立 `CLAUDE_CONFIG_DIR`；
- MCP JSON 与 `--allowedTools` 同时按 Profile 裁剪；
- 内置工具继续保持关闭；
- `p5-no-mcp` 不向进程传递 MCP Token 或 MCP URL。

### DeepSeek Harness

- 使用 Trial 独立 `DSH_HOME`；
- Cordis Patch 只插入 Profile 允许的 MCP Client；
- Shell、文件、Web 和 Subagent 等插件继续保持关闭；
- `p5-no-mcp` 不生成 MCP insert 块，也不传递 MCP Token。

## 4. 证据与清理

每个正式 Trial 新增 `permission-surface.json`，记录：

- Profile ID；
- 有效 MCP Server 和工具；
- BladeAI 原生权限；
- Kubernetes 规则；
- 允许的故障类型；
- 权限到期时间。

该文件不包含 Token、API Key 或 kubeconfig 内容。

权限恢复阶段会：

- 删除 BladeAI Trial RBAC 和 ServiceAccount；
- 删除 BladeAI 临时 kubeconfig；
- 删除 MCP Token 状态文件及其 Trial 目录；
- 关闭 Trial MCP 进程；
- 关闭 BladeAI Trial Server；
- 删除四类 Harness 的临时 Home。

## 5. 本地验证

已执行与第一阶段相关的 Stage2、Harness、Profile、配置渲染、事件、权限和清理单元测试，共 `134 passed`。测试同时报告 7 个既有 Pydantic V2 弃用警告，没有权限控制测试失败。还完成了 Python 编译检查、Git diff 格式检查和 Wheel 内容检查。

这些结果只证明代码契约、配置生成和进程适配逻辑；不证明远端 Agent 已经按新 Profile 实际运行。

## 6. 用户手动集成边界

手动测试前必须先构建并部署包含本次代码的 Stage2 版本，并由执行环境提供四类 Harness 运行时。BladeAI 还需要：

- `STAGE2_BLADEAI_PYTHON`；
- 当 `bladeai_native.chaosblade_execute=true` 时提供 `STAGE2_BLADEAI_CHAOSBLADE_ROOT`。

建议先读取：

```text
GET /api/v1/permission-profiles
```

然后在 Campaign Request 中选择一个完整 `permission_profile`，并设置：

```yaml
trial_kinds: [control]
```

这里的 `control` 只表示不增加目标变化或运行时权限扰动，不表示 Episode 不会执行主故障。若手动测试必须只读，应选择另行批准的只读 Episode。

每个 Profile 的手动验收至少检查：

1. 实际工具面与 `permission-surface.json` 一致；
2. 允许的工具至少真实调用一次；
3. 被移除的工具不可调用；
4. BladeAI 的合法原生替代路径按 Profile 保留或关闭；
5. Agent 最终输出、Harness 轨迹和独立 Oracle 分开保存；
6. Trial 结束后不存在本轮临时权限、进程、配置或故障残留。

任何一项无法确认时，不应把该轮写成权限 Profile 已通过。
