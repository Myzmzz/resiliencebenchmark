# 原始交互夹具与来源

## DeepSeek 带内求助往返

- 文件：`deepseek_consult_roundtrip.tool-events.jsonl`、`deepseek_consult_roundtrip.summary.json`。
- 来源：`artifacts/qualification/dsh-mcp-followup-20260905/evidence/` 中同名原始内容，在 2026-09-05 整改基线阶段原样提取。
- 环境：`1.94.151.57` 的隔离资格容器；DSH `0.1.0-rc.7`、模型 `gpt-5.6-sol`，单个 headless 进程，无 resume，29.95 秒。
- 证据性质：服务端三次 MCP 调用及进程摘要。`session_files` 为空，**不是 DSH 原生 session 流或 zstd 回放夹具**，不得据此声称实现了工具流适配。
- 范围：受指令驱动的通信资格验证；未执行 Kubernetes 故障，不代表自主发现替代工具或 D7/D8 评测得分。
- 隐私：这两份文件不含上游密钥、MCP Bearer、集群凭据或私有配置。`ticket` 与 `FOLLOWUP_OK_*` 是资格工具临时生成的传递校验数据，不是可访问任何服务的凭据。未复制进程环境或原始配置。

这批历史错误文本是 `TOOL_WITHDRAWN`，与整改目标的 `TOOL_DISABLED` 不同。保留历史原文，不把旧证据改写为新实现通过。

## 交互链路方案黄金夹具

方案表格列出的七份历史原生流现已脱敏导入 `golden/`，来源任务与原始行数见 `golden/provenance.json`。导入只读既有记录，未重新执行任何模型或故障。原始私有副本和脱敏夹具分开保存；导入程序为 `scripts/import_stage2_harness_fixtures.py`，复用已有凭据脱敏规则，并处理嵌套 JSON 字符串、去除私有思维字段。DSH 文件保留全部 635 条原生记录，但经过脱敏后重新压缩，不声称保留原压缩帧布局。

`tests/test_stage2_golden_replay.py` 已验证 Claude L1 的 100 对、L3 的 46 对、Codex L3 的 29 对、DeepSeek L3 的 35 对调用/结果闭合，以及 L4 的三次创建尝试。L4 原始流有三个独立 call ID，计划文字中的两次遗漏了第一次 selector 拒绝；不为贴合旧文字删掉真实尝试。DSH 含一次内置 exit_plan_mode 的文本错误，不强行改成 MCP 成功结果。

源方案写“8 份”，其表格实际列 7 份；前述 DeepSeek 求助日志是另一独立资格运行。`synthetic_canonical_adapters/` 仍只用于协议边界单元测试，不冒充历史证据。
