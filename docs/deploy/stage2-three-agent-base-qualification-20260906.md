# 三家基础资格与共享运行锁

旧集群 integration 已部署 `47f4f04`，Pod 为
`resbench-stage2-integration-57576c4c76-mc9mt`。本次没有访问新集群，也没有
发起正式扰动任务。原 Codex 资格文件与历史任务记录保留。

## 运行互斥

API 与资格 CLI 共用 `/run/resbench/stage2-active-run.lock`。它是同一
AgentExec/MCP 实例的 OS 文件锁，不是跨 Pod/SUT 分布式锁，锁文件存在也不
代表正在运行，锁释放不代表故障清理完成。

部署后实际执行了跨进程竞争检查，并在部署镜像中的 API 代码上使用无操作
runner 检查 409/202 路径：持锁时另一进程不能获取，API 拒绝执行；释放后可
执行并再次释放。没有模型调用或故障操作，也不是向常驻任务 API 提交了实验。
证据：`artifacts/remediation/20260905/runtime-lock-live-proof.json`。

## 新完成的真实基础资格

| Harness | Trial | 真实模型请求 | 原生会话/回合 | 七项检查 | 清理 |
|---|---|---:|---|---|---|
| Claude Code | `campaign-63b983fd0d5e430a-claude-code-d0-1` | 12 | 1 / 1 | 全部通过 | 无错误，无残留目录/令牌 |
| DeepSeek Harness | `campaign-6937e685faf3458c-deepseek-harness-d0-1` | 11 | 1 / 1 | 全部通过 | 无错误，无残留目录/令牌 |

两者都使用 `gpt-5.5`、同一 base 资格脚本，完成真实读取、确认、求助、通知
回执、结果提交及网关验证；运行前后四类故障资源清单为空。这里证明基础
通道工作，不证明 D1–D8 扰动或故障效果/业务恢复已经通过。

发布时显式同时输入 Codex a3、Claude a1、DeepSeek a1 三份真实记录，发布器
重新核对各自原生归档和网关请求记录，保留三家资格。没有为 BladeAI 补造记录；
它仍需 WP8 原生确认钩子、shim 创建/销毁与独立清理证明。

证据均在 `artifacts/remediation/20260905/`：

- `base-claude-a1.stdout.json`、`base-claude-a1-native.tar.gz`、`base-claude-a1-cleanup.json`。
- `base-deepseek-a1.stdout.json`、`base-deepseek-a1-native.tar.gz`、`base-deepseek-a1-cleanup.json`。
- 两家各自的 `before/after-chaosblade.json` 与 `before/after-chaosmesh.json`。
- `base-three-harness-publication.json`。

## 代码与未完成边界

共享锁、D0 可评估失败结果准入及手动参数模板的全量回归：1593 通过、10 跳过，
0 失败（58.058 秒）。BladeAI MCP 配置边界修复的后续全量回归：1596 通过、
10 跳过，0 失败（53.812 秒）。测试文件分别为 `runtime-lock-manual-requests-full.xml`
和 `bladeai-mcp-boundary-full.xml`。

BladeAI 启动器曾把模板中关闭的两个执行器 MCP 强行启用；修复代码已尊重
模板关闭状态，写入仍保留给受控 shim。这项修复尚未经过新的真实 BladeAI
全链运行，不能作为资格通过证据。

正式 D0 组合、BladeAI 全链、Coroot Viewer 与 WP11 替代/沙箱资格，以及
68 项人工验收仍未完成。手测参数见[四家单项参数索引](../manual-tests/README.md)。
