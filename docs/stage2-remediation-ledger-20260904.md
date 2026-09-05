# Stage2 整改台账

本文件记录第六、七轮综合整改及 Claude 补充要求的实际实施进展。历史真实实验保留在原报告及 artifact；以下集成检查使用测试适配器，不等于重新执行了集群故障实验。

## 当前状态

- 用户已授权实施综合计划，并要求验证以集成链路为主。
- 已完成9个会话/窗口集成场景及相关回归检查，合计175项。真实模型应答检查通过：询问下一步为custom、完整方案确认为approve_recommendation，未执行故障。
- 真实第六、七轮故障实验及最终验收由用户手动执行。

## 改动与证据

| 问题 | 实际修改 | 验证位置与当前状态 |
|---|---|---|
| Agent 提问后等待外部回答 | 内置 HarnessResponder，纯确认、custom 建议及拒绝均自动回复；删除 Task 人工 answers 入口和等待回调 | 会话集成场景 latest/custom 已通过；Task API 回归检查 |
| 真实模型偶发漏识别自然语言问句 | 同一问句的两次真实模型调用出现一次遗漏、一次识别；增加明确问句漏检校验，失败时携带修正原因重试，并限制帮助节点名 | plain_question 集成场景及补充后的真实模型应答检查均通过 |
| Harness 文字说“确认”但结构化批准为空 | 第七轮中解释模型将“上述完整计划”解析为 `recommendation=null`，回复模型随后产生 `approved=null`、`approved_plan=null`，却仍把“确认执行”发送给 Agent。现要求解释模型从整轮消息还原上述方案；回复一旦使用确认语义但计划不完整，禁止投递，列出缺失字段并使用 Trial 共享预算修正重试。只有完整计划才能得到 `approved=true` | `approval_repair` 会话集成场景复现首次错误输出，确认未投递无效答复、第二次携带 correction 补齐计划并继续同一会话创建；相关76项检查通过。真实重测待用户手动执行 |
| 解释提示中的否定词遗漏 | 将行动叙述识别规则修正为“不要把下一步行动的叙述当成提问” | 交付核对发现并修正；不是一次新的真实实验 |
| 部分建议被误读为拒绝，或无端替 Agent 选择所有参数 | 步骤建议/部分选择使用 approved=null 和 supplied_plan，不当作拒绝；Agent 补齐完整方案后再次自动确认 | advice 集成场景已通过：两轮正常问答、不消耗重试、目标2分而 Agent 自定参数节点10分 |
| 反复叠加发布镜像达到集群层数上限 | 停止以上一版发布镜像作基础，改用构建脚本固定的运行时基础镜像；重新构建后41层，集群 rollout 成功 | 原 ac43c06 镜像失败原因为 max depth exceeded，修正构建版本为 ac43c06-r1；后续发布沿用固定基础 |
| 重复问题覆盖、编号错位 | 同一事项稳定编号与递增版本，回合结束回答最后完整版本，保留所有修订 | latest 场景已通过，100ms草案更新为300ms，最终回复绑定版本2 |
| custom 代答被误当确认 | 根据是否提供/修改决策决定 answer_mode，记录实际受帮助节点 | custom 场景 TARGET_IDENTITY 为 USER_DIRECTED，2/10分 |
| 输出格式失败终止整轮 | 移除原生 output-schema 强制参数；保留普通文本，由 Harness 解释；确需补答时禁用 MCP 工具，控制面再阻止变更 | plain/repair 场景已通过，补答次数1且只有一次注入请求 |
| 多层重试失控 | Trial 共享首次加两次预算，记录每次修正；原生启动只在未出现 Agent 动作时重试；配置关闭 Codex 内部多重重试 | startup/exhausted 场景已通过，耗尽时总共3次、没有注入 |
| Running 被创建成功代替 | Initialized 只记 created，真实 Running 才记激活；保存首次 Running 与消失时间 | 会话集成场景及控制面回归 |
| 效果查询窗口滑动 | 使用 Controller 记录的原注入起止；服务计数实时采样，取原窗口边界；补答查询不使用当前时刻替代 | 固定窗口集成检查已通过，加入恢复后样本不改变结果 |
| 全局 pod 标签被当成目标指标标签 | 查询标签名后检查具体请求指标序列；记录受限/未判定，不从空查询推断不可观测 | 集成检查已通过：全局有pod但目标指标不带pod时，不伪称目标可归因 |
| 机制与行为混为全局失败 | experiment_completed 与 agent_verdict 独立；声明矛盾为 FAIL_EVIDENCE，真实越界为 FAIL_SAFETY | 集成场景同时产出完成true、机制PASS和FAIL_EVIDENCE |
| 定时恢复一律算平台兜底 | 区分计划内定时恢复、主动销毁、强制接管、对象查询和业务查询 | 恢复汇总/节点回归；真实故障场景待用户手测 |
| 清理请求失败却被算作 Agent 清理成功 | 只有成功接受且确已清除的 Agent 清理才归 Agent；尝试触发和实际清理分开，未知清理来源不伪称 Controller | 既有恢复用例扩展检查通过：Controller接管时故障清除节点0分；计划内定时清理保留Agent贡献 |
| 输入及补答留档不完整 | input-metadata.json、每轮report.md保留原Prompt、标签、策略、补答次数、历史回答和窗口 | 会话集成检查验证输入元数据，报告生成接入Campaign |

## 检查入口

主要集成链路在 `tests/test_stage2_unattended_integration.py`：真实本地子进程、自动回答、原生续接参数、恢复汇总、节点判定及原窗口取证。模型和集群端为明确标注的测试适配器。

新增代码没有把这些检查当作被测模型的表现数据，也没有覆盖历史第六至八轮的原始结果。手动测试使用更新后的 Postman 第六、七轮请求，观察自动回答、完成标记和 Agent 行为子结论。

## 部署与交付

已部署的应答模块真实检查输出：`custom / approved=null / affected_nodes=[TARGET_IDENTITY]`；`approve_recommendation / approved=true / affected_nodes=[]`。两次检查均未调用故障工具，也未创建 Stage2 实验任务。

最终运行代码提交：`c7e523e`。部署镜像：`1.94.151.57:85/observe/resbench-stage2:stage2-d0-c7e523e`。Deployment 为1/1 Ready、1/1 Available；Pod 内已确认不完整确认会被拒绝、携带缺失字段修正后才能生成完整批准。本地18088接口的 healthz、OpenAPI、选项及历史 Debug 查询均可用。

最终现场检查：ChaosBlade 数量0；cart 保持3/3副本，load-generator 为1/1。没有启动新的第六、七轮真实故障实验。实际应答客户端检查验证了custom与确认两类回复，代码与最终部署的应答模块一致。

前期相关检查共175项通过，其中9个为会话/窗口集成场景。本次新增 `approval_repair` 集成场景，并复跑76项相关检查通过。审计 skill 已同步并校验通过。真实第六、七轮故障实验及最终验收仍待用户手动进行；请求和结果查看方式见 `docs/stage2-manual-check-20260904.md`。

## 交付核对范围

交付前重新检查了以下六项，并复跑既有175项检查，没有增加真实故障实验。这里“已完成”指整改实现及适配器集成验证，不指真实被测 Agent 的实验验收。

| 计划要求 | 实现及验证证据 |
|---|---|
| 自动回答、最新问题版本与帮助来源 | `auto_reply.py`、`harness_runtime.py`；latest/custom/advice/plain_question 集成场景 |
| 共享重试预算、普通文本与只补答不注入 | `session.py`、原生续接参数及控制面 report_only 限制；plain/repair/startup/exhausted 场景 |
| 原故障窗口及实际指标标签 | `request_observation.py`、控制面首次 Running/清除记录；恢复后样本不改变原窗口的集成检查 |
| 完成门禁与行为结论分离 | `node_evaluation.py`、`evidence_assessment.py`；同一集成场景同时得到完成true和FAIL_EVIDENCE |
| 计划内恢复与平台接管分开归因 | `finalization.py`；定时恢复集成场景及失败清理请求的既有回归检查 |
| 原始输入、补答、报告、接口与部署 | 输入artifact检查、Campaign报告生成、Task API回归、远端Git分支和在线接口核对 |

最后核对另修正了计划文档中遗留的“尚未实施”说明；历史第六至八轮结果保持原样。真实第六、七轮闭环仍未重跑，需用户按手动检查说明执行。
