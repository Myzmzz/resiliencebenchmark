# Coroot 只读 API 资格记录（WP6）

状态：**代码与假后端契约已验证；旧集群真实 API 资格未完成。**

本记录只针对旧集群的既有 Coroot 实例。它不是部署记录，也不代表 `coroot_ro` 已经接入任何 Trial。

## 已确认的边界

旧集群存在 `coroot/coroot-coroot` 服务（HTTP 8080），运行镜像
`1.94.151.57:85/observe/coroot:v4.0.5-isobserver`。本次只进行了
无状态 HTTP 可达性检查：`/health` 返回成功，而以普通 GET 访问 `/mcp`
得到 HTML 页面。后者不能证明 Streamable MCP 是否可用；正式资格必须以一个
**只读身份**执行 MCP 初始化或对应 HTTP 查询来判定。

当前 Coroot CR 配置为匿名 `Admin`。因此它不能作为 `coroot_ro` 的安全凭据：
即使服务本身只发 GET，也不能把一个管理员匿名访问路径描述成平台控制的
只读身份。本期没有创建用户、角色、令牌或修改 Coroot 配置。

官方 Coroot 当前源码与文档表明以下只读语义存在，但不能据此假定定制的
v4.0.5 镜像具备相同接口：

| `coroot_ro` 工具 | 代码采用的只读 Coroot 路径 | 下游实际作用域 |
| --- | --- | --- |
| `coroot_metrics_range` | `GET /api/project/{project}/prom/api/v1/query_range` | 服务端生成 PromQL，强制 `namespace="<Controller scope>"` |
| `coroot_traces_find` | `GET /api/project/{project}/app/{application}/tracing` | `project`、`application` 都由 Controller 固定；仅输出获准 `service` 的 span |
| `coroot_logs_range` | `GET /api/project/{project}/app/{application}/logs` | `project`、`application` 都由 Controller 固定；仅输出有界日志，`pattern` 仅用于本地结果筛选 |

指标的 namespace 不是调用方传入的 labels 过滤条件：工具拒绝调用方传入任何
namespace 标签，并在向 Coroot 发出的 PromQL 中构造精确匹配器。Trace 和日志
使用 Coroot 的 application 路径作为下游作用域；调用方没有项目、应用或 URL
参数。服务工具说明不包含“备用、替代、测试”或相应英文词。

## 环境阶段必须完成的资格检查

在任何 D7 Trial 前，使用 Controller 预置的独立只读身份完成以下检查并把结果
记录到 Trial 外的部署证据中：

1. 确认当前镜像是否支持上述三个 GET 路径及所需参数；其中 app 路径的
   `from`/`to` 必须按毫秒解释，Prometheus proxy 的 `start`/`end` 必须按秒解释。
2. 为单一 Coroot project 配置身份，并证明该身份仅具备 metrics、traces、logs
   的读取权限；不采用匿名 Admin、浏览器管理员 cookie 或 Agent 可见凭据。
3. 用 `otel-demo` cart 的 Controller application id 在一个固定历史窗口内查询，
   分别确认指标、Trace、日志有可解析结果。Trace 与日志响应中不得泄露其他
   application 的条目。
4. 用同一身份验证错误 project、错误 application 和未获准 service 被拒绝或
   不可见；验证范围扩大不会被应用路径绕过。
5. 验证每次试验策略门可拒绝三项 `coroot_ro` 工具；策略恢复后服务重新可用。

若任一 GET 路径在该定制镜像中不存在，`coroot_ro` 会返回
`backend_endpoint_unsupported`，而不是伪造结果、改走 `telemetry_ro`，或把
未资格化的路径当作观测证据。届时应基于已确认的 Coroot 版本 API 调整适配器，
再重新运行本记录的本地和环境资格测试。

## 已运行的本地验证

`tests/test_coroot_ro_mcp.py` 使用假 Coroot HTTP 后端并直接调用 MCP SDK v2 的
`list_tools` 和 `call_tool`，覆盖：

- 三个工具均被列出并标记为只读；
- Prometheus 请求中有 Controller 注入的 namespace matcher；
- 调用方不能覆盖 namespace，超过六小时的窗口被拒绝；
- Trace/日志请求的 project/application 路径由 Controller 固定，未获准 service
  在发送 HTTP 前被拒绝；
- Trace duration 和日志 pattern 的结果过滤可审计；
- 工具描述中没有提示其是替代、备用或测试通道。
