# 基础通道资格与发布

这条链路补齐 C0–D6 的平台接入证明，不替代 D0 受控注入资格、BladeAI WP8
全链资格或 WP11 替代工具资格。L0–L4 Prompt、评分权重与原 Task API 参数均不改。
本文是执行说明，不是任何智能体已资格通过的声明。

本地验证：专项 87 项通过；全量 1534 项通过、10 项跳过、0 失败，52.910 秒。
证据分别为 `artifacts/remediation/20260905/base-publisher-focused.xml` 和
`base-publisher-verified-full.xml`。联通测试覆盖资格 evaluator 的实际输出、
发布器、既有 preflight 读取器；真实 CLI 正常与错误配置路径也有单独测试。

## 为什么拆开

原通道资格脚本只支持 WP11，必须调用 Coroot 和沙箱；原准入读取器只接受
`stage2-harness-capabilities.v1`，却没有生产发布程序。把 WP11 当作 C0 的唯一
入口，就会让 Coroot 准备阻塞无关用例。D0 自身又需要基础运行能力资格，不能
反过来把 D0 结果当成启动 D0 的唯一前置证据。

现在同一个生产 NativeHarnessRunner 支持两个内部资格配置：

- `base`：真实目标/基线读取、确认往返、中性求助回复、通知投递与确认回执、
  合规结果提交、真实网关证据；只暴露五个基础服务，禁止故障写操作。
- `substitution`：保留 WP11 的停用遥测、固定提示、Coroot、沙箱、通知与提交
  全部要求，不降低门槛。两类记录使用不同文件名，不互相覆盖。

每家使用同一套配置和检查规则。基础资格不强迫 BladeAI 直接调用
`chaos_control`：它的写路径按 WP8 设计经过 shim，应由对应真实全链验证。
只有 base 记录时，发布器对 BladeAI 明确保留
`bladeai_full_chain_qualification_required`，不把它标成可运行。

## 执行顺序

以下是 Controller 内部的运维命令，不是发给被测 Agent 的指令，也不增加
Postman 请求字段。先确认旧集群无活动任务、无故障残留；每次只运行一家，
每次资格尝试使用新的受保护输出目录。Harness 导致的失败保留原记录，单轮
最多三次，不以换目录重置失败预算。

```bash
/app/.venv/bin/python /app/scripts/qualify_agent_channel.py \
  --profile base --model gpt-5.5 --harness codex \
  --protected-root /var/lib/resbench-stage2/integration/private \
  --output-dir /var/lib/resbench-stage2/integration/qualification/<attempt>
```

通过后，再发布该次真实记录：

```bash
/app/.venv/bin/python /app/scripts/publish_harness_capabilities.py \
  --record /var/lib/resbench-stage2/integration/qualification/<attempt>/base-channel-qualification-codex.json \
  --artifact-root /var/lib/resbench-stage2/integration/artifacts \
  --gateway-config /etc/litellm/config.yaml \
  --output /var/lib/resbench-stage2/integration/private/harness-capabilities.json
```

重复 `--record` 明确列出本次要保留的全部已验证 Harness 记录；发布器不会隐式
补齐其他智能体，不会将旧文件自动合并进来。任何输入验证失败，原发布文件
保持不变。Controller 已有的 `STAGE2_HARNESS_CAPABILITIES_FILE` 指向该产物后，
原 preflight/options/任务入口按现有规则读取，不需要用户填写资格引用。

## 发布检查与边界

发布时重新读取并核对实际归档，不能只凭 `passed=true`：

- 七项基础检查、Harness 完成状态和清理结果都必须成立。
- 网关请求记录必须对应同一 Trial/Harness/model，以及当前路由配置。
- 原生工具调用与 Controller MCP 调用均须配对闭合，覆盖基础读取和四个通道
  工具；Controller 调用还须与资格记录中的 `ordered_exchanges` 对应。
- 创建/销毁尝试不得用于无故障基础资格；未知原生工具和资源发现操作不贡献
  资格证据。任何沙箱或替代服务能力都不能从基础资格推断。
- 拒绝链接、越界或可被其他身份写入的证据文件。发布采用原子替换，文件为
  `0600`；网关证据和原生记录必须来自同一个归档目录。

由实际原生事件判定流式或事后记录能力；基础运行没有验证 resume，因此不
声称具备该项能力。已验证的中途交互使用 `in_band_mcp`。基础发布始终为
`code_execution=none`，WP11 沙箱资格和 BladeAI WP8 晋级仍需后续完整接线。

当前单任务 POST 内部仍使用 `qualification_mode=diagnostic`。基础发布不会
把它自动变成正式矩阵计分；正式计分还需服务器内部绑定有效 D0 证据。不能
把“任务能启动”“实验执行完成”和“正式计分资格通过”混为一谈。
