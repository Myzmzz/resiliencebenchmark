# 旧集群环境资格记录（UTC 2026-09-06）

范围固定为 `coroot-config`、`kubernetes-admin@kubernetes`，节点 `tcse-v100-03`；新集群未部署、未测试。不是正式扰动评测结果。

运行版本 `db8e03e`，integration Pod `resbench-stage2-integration-7fb855c7c5-69nfc` 三容器就绪、零重启；主服务/e2e仍为旧版本。后续沙箱补丁的部署结果另行记录。

| 实际检查 | 结果 | 原始证据 |
| --- | --- | --- |
| 普通AgentExec子进程 | 11项通过：UID10002、无能力集、无SA文件、私有cgroup namespace；允许18090可达，禁止18091/4000/K8s API均阻断 | `artifacts/remediation/20260905/linux-agent-boundary-033635.json` |
| 三种Kubernetes身份与授权 | 80项通过；Controller、executor、finalizer身份符合预期，无故障变更 | `artifacts/remediation/20260905/identity-qualification-db8e03e.json` |
| 网关六模型探针 | gpt-5.5、claude-opus-5、两种DeepSeek、两种Qwen均supported，基础/streaming/tool/结构化响应检查通过 | `artifacts/remediation/20260905/old-gateway-six-models-db8e03e.json` |
| 代码沙箱+Unix broker | 失败：隔离初始化PermissionError，用户代码未执行，broker无残留socket | `artifacts/remediation/20260905/sandbox-qualification-sq7eea1c.json` |

六模型探针生成时间为03:50:37 UTC；它不包含四家原生智能体资格，不能替代D0或D1-D8实跑。普通子进程检查也不等于资源耗尽、取消/清理、sandbox或真实MCP资格。

Coroot匿名Admin改为独立Viewer仍待用户确认；现状态不计只读资格。Chaos Mesh组件已就绪，但尚未做真实金丝雀注入。八个D0 Campaign、68格正式验收均未执行。
