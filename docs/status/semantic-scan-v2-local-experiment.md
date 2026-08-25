# CodeGraph + Kubernetes + LangChain语义扫描本地实验状态

> 状态日期：2026-08-24 CST
>
> 范围：只读本地扫描，不注入故障，不部署到node3

## 核心判断

V2语义扫描主体已实现：Controller可由显式代码库路径创建/同步CodeGraph索引，同时解析Kubernetes/Helm期望配置；LangGraph编排12个模板专用LangChain子Agent和独立反证Verifier；确定性证据账本、D类、故障类型、目标和置信度门决定是否允许出题。

RD-14正负对照已通过：相同CodeGraph调用链下，“1副本+无等价冗余”输出0.98高置信匹配，“2副本+PDB+强制拓扑分散”不匹配，未产生误报。

## 实现合同

- 代码库入口：`SemanticScanConfig.codebase.path`。
- Controller独占CodeGraph `init/index/sync`；Agent只有`query/callers/callees/context`。
- Kubernetes扫描保留Workload、Resources、Probe、HPA、PDB、Service、拓扑和非敏感Env；Secret值不进入上下文。
- 每个模板一个无状态子Agent，不共享消息历史。
- Verifier使用独立上下文和新查询尝试推翻候选。
- LangChain `ToolCallLimitMiddleware=8`、`ModelCallLimitMiddleware=12`，加单Agent 300秒硬超时、输入/工具结果字符预算。LangGraph步数上限为48，用于容纳中间件与最终结构化收尾，不放宽工具/模型调用上限。
- Provider原生JSON解析失败时回退到LangChain ToolStrategy；请求超时和结构解析最多重试一次，`insufficient_quota`不自动重试。
- 每个模板完成后立即持久化Draft、Evidence Ledger和Checkpoint；恢复时强制校验CodeGraph/Kubernetes Hash。

## 固定输出

`resilience-template-match.v1`必须包含：

1. `defect_name`；
2. `evidence_explanation`；
3. `mechanism_chain`；
4. `available_fault_types`；
5. `fault_injection_target`。

额外包含D类、置信度、反证检查、CodeGraph/Kubernetes Hash和Verifier已确认证据ID。Pydantic生成的JSON Schema随每个Run落盘。

## 本地实验证据

### OTel Demo完整扫描

Run：`semantic-otel-demo-local-v3`

- CodeGraph：311文件、8,770节点、12,667边、15种语言。
- Kubernetes：4个输入文件、86个资源。
- 覆盖12/12模板，最终无`failed`。
- RD-01、RD-05、RD-08、RD-09为`inconclusive`；其中RD-09的Verifier确认了`grafana`容器`GOMEMLIMIT == memory limit`候选，但Agent置信度0.78低于0.85出题门，且仍有JVM替代假设未解，故不出题。
- RD-02、RD-06、RD-07、RD-10、RD-11、RD-12、RD-13、RD-14为`not_matched`；系统没有为了强行出题越过证据门。

### RD-14阳性对照

Run：`semantic-rd14-positive-v4`

- `publicCheckoutApi -> checkoutRoute -> CartService::addItem`调用链。
- Controller可信注解将`CartService/checkout.ts/source-identity`映射到Deployment/cart。
- Deployment为1副本，权威配置范围内无等价副本。
- Verifier=`confirmed`，置信度=0.98，故障=`pod-delete/pod-fail`，`question_eligible=true`。

### RD-14阴性对照

Run：`semantic-rd14-negative-v2`

- 与阳性对照相同的CodeGraph业务链。
- Deployment为2副本，带PDB `minAvailable=1`和`DoNotSchedule`拓扑分散。
- `not_matched`，误报数=0，不出题。

结构化总结：`artifacts/semantic-scan/local-experiment-summary-v2.json`，`status=passed`。

## 尚未完成的验证

- 当前只对RD-14完成真实模型正负对照；其他11类已有合同/单元/集成覆盖，但尚未各自达到10阳性+10阴性的精度校准门。
- 本阶段未部署到1.94.151.57，也未执行故障；符合“先本地充分验证再部署”的边界。
