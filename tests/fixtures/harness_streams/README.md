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

## BladeAI 0.7.0 黑盒 SSE 夹具

- 文件：`golden/bladeai_L0.sse`（原始 SSE 线格式）、`golden/bladeai_L0.recv.jsonl`（接收时刻侧车）。
- 来源：评测机 `1.94.151.57` 的 `/root/bladeai-eval/runs/L0/events.jsonl`，会话 `sess_8a686003edf5`，
  2026-09-11 16:26:32–17:20:26，模型 qwen3.8-max。只读既有记录，**未重新执行任何模型或故障注入**。
- 裁剪：原 16,655 条裁到 582 条。结构类事件（confirm / tool_start / tool_end / result / done /
  node_message / node_start / node_end / llm_start / usage / context_size）**一条不删**；
  只对 `thinking` / `token` 逐字流抽样（前 12 条保留，其后每 400 条留 1 条），
  保留首轮连续段以便重放时仍是真实的流式节奏。
- 线格式事实：BladeAI 只发 `data:` 字段，**不发 `event:` 名**，事件种类在 JSON 体的 `type` 里；
  行尾是 LF，帧间空行分隔。`tests/test_bladeai_http_replay.py` 按 997 字节乱切重放，
  验证分帧不依赖网络分块边界。
- 隐私：未经 `import_stage2_harness_fixtures.py` 脱敏（该程序针对 CLI 原生流），改为逐模式扫描核验：
  私钥、JWT、`sk-`/`AKIA` 密钥、Bearer/Authorization、`client-certificate-data`、
  password/secret/credential 均 **0 命中**；18 处 `kubeconfig` 全部是命令行参数与文件路径，非凭据内容。
- 证据边界：这是**传输层**夹具，证明分帧与落盘完整，**不**代表适配器语义映射（WP-B）或确认桥（WP-C）已完成。

## BladeAI 上游模型失败样本（错误三分类真样本）

- 文件：`golden/bladeai_upstream_auth_failure.events.jsonl`、
  `golden/bladeai_upstream_conn_failure.events.jsonl`（驱动器落盘的原始事件日志）。
- 来源：2026-09-12 按已定口径 9 **专门构造采集**，在 `1.94.151.57` 的独占 BladeAI
  服务实例上跑一个只读问题；不碰集群、未注入任何故障。
  两份分别制造「无效 API key」与「网关地址不可达」。
- 为什么要它：全语料 10 条 `error` 全是评测方自己的 `/cancel`，
  「上游模型错误」这一档**没有真样本**，无法验证分类器。
- 关键事实（这两份夹具的全部价值所在）：**上游模型失败根本不产生 `error` 事件**。
  两份的事件流完全一致——`node_start → context_size → llm_start ×3 → node_end → done`，
  终态 `done`、`returncode 0`、仅 7 条事件、不到 3 秒。
  从平台视角这是一次「正常完成但什么都没做」的回合；只看终态会判 0 分，
  而按口径应判**无效**。真实原因只在服务端日志里（`resilient_llm: retries exhausted`），
  黑盒拿不到。可用的黑盒判据是「`llm_start` 之后没有任何 `token`/`thinking`/`tool_start`」。
- 隐私：逐模式扫描核验不含凭据（私钥、JWT、`sk-`、Bearer 均 0 命中）。
