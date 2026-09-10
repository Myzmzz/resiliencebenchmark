# Coroot API 与只读身份资格（WP6）

当前结论：**旧集群原生指标、Trace、日志接口已实读；独立只读身份资格尚未通过。**
接口形态探测使用了当前匿名 Admin 访问，不是 Viewer 资格，不是实际故障效果证据。

## 实测发现与代码修正

旧服务为 `coroot-coroot.coroot.svc.cluster.local:8080`，镜像
`1.94.151.57:85/observe/coroot:v4.0.5-isobserver`，项目 `9auios5b`。
Controller 发现的 cart application id 为 `9auios5b:otel-demo:Deployment:cart`。
本轮仅 GET 查询，没有创建用户或修改 Coroot 配置。

`GET /api/user` 在无凭据和携带无效 Bearer 时均返回
`role: Admin, anonymous: true`。CR 的 `authAnonymousRole` 也为 `Admin`。
本机对应源码的匿名角色分支优先于 session cookie。因此旧 Bearer 配置既不是
有效用户认证，也不会使匿名 Admin 降为只读；已从 MCP 实现删除，不保留回退。

| MCP 功能 | 实际原生接口 | 关键格式 |
| --- | --- | --- |
| 指标值 | `GET /api/project/{project}/panel/data` | DashboardPanel JSON 放在 `query`；`from/to` 为毫秒；响应 `chart.ctx + chart.series[].data` |
| 指标身份 | `GET /api/project/{project}/prom/api/v1/series` | 同一作用域 PromQL 的 `match[]`、秒级 `start/end`；返回结构化 labels |
| Trace | `GET /api/project/{project}/app/{application}/tracing` | 响应为 `context/data` 包装；timestamp、duration 均为毫秒 |
| 日志 | `GET /api/project/{project}/app/{application}/logs` | JSON `query`；响应为 `context/data` 包装，条目 timestamp 为毫秒 |

原 `/api/project/{project}/prom/api/v1/query_range` 并非该版本支持的 UI
proxy 路径。另一个 `/api/v1/query_range` 使用项目 `X-API-Key`，该 key
还用于采集写入，本方案不使用它冒充全功能只读用户身份。

指标的 Chart 系列名是 Coroot 的字符串格式，不能凭文本猜测 Pod UID。
适配器使用同一次查询的原生 `series` labels，与 Chart 系列名精确且唯一对应，
然后才给 matrix 写入后端实际返回的标签。无法唯一匹配时不绑定 UID；不能用
请求中的 UID 伪造响应身份。样本时刻来自后端 `ctx.from + index * ctx.step`，
保留截断状态，拒绝非有限数值和越出后端时间范围的样本。

cart 的当前原生默认数据源是 eBPF Trace/容器日志。强制 `source=otel` 时
实际返回了空样本；使用原生自动选择后，同一五分钟查询范围取得70条 Trace，
以及按请求上限返回2条日志。因此适配器不强制 OpenTelemetry 数据源，保留
后端实际 source/status/message。空样本不能被解释为“效果已验证”。

指标格式探测使用 `kube_pod_info` 并得到真实 namespace、pod、uid 标签；
它仅证明元信息查询可用，**不能作为网络、CPU 或内存故障的效果证据**。

## 只读身份与权限边界

MCP 只接受 Controller 私下提供的 `RESBENCH_COROOT_SESSION_COOKIE`
（裸 `coroot_session` cookie value）。它不是 Task API/Postman 参数，不进入
被测 Agent 的环境；公共记录会对 cookie 脱敏。过期或格式错误直接失败，不改用
匿名身份。每次观测前 `/api/user` 必须明确返回 `anonymous: false` 和
`role: Viewer`；缺字段、匿名、Admin 均拒绝。

Coroot 内置 Viewer 是全局只读角色，不是项目级 RBAC。MCP 再固定 project、
application、namespace、service 和时间窗，调用者不能覆盖 URL 或项目。
不能对外宣称 Coroot 本身已实施了项目级最小权限。

**仍待用户批准：关闭现有匿名 Admin 并启用登录式只读身份。** 这会改变现有
匿名访问行为。操作前必须确认管理员仍有可用登录路径；不直接关闭入口导致
用户失去管理访问。当前没有执行这项变更，也没有发布 Coroot 资格为通过。

## 进入 D7 前仍需完成

1. 以专用 Viewer 身份重新查询目标指标、Trace、日志，并证明管理员接口被拒。
2. 验证错误项目、应用、service、namespace 和超范围时间窗在 MCP 边界被拒。
3. 用真实受控故障窗口验证故障相关指标或其他充分证据，不能只使用 Pod Ready
   或 `kube_pod_info` 证明效果。
4. 验证策略停用与恢复、身份过期、空结果和后端错误均保留真实失败信号。

本地契约测试使用忠实原生格式的测试后端；不能代替上述 Viewer 与真实故障资格。
