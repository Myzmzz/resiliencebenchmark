# WP-A.2：MCP 挂载验证 + 错误三分类真样本（2026-09-12）

两件事一趟做完，都在 `1.94.151.57` 上用**独占的服务实例**完成，
不碰集群、不注入故障。收尾核对：我起的进程全部停止，原有 8199/8089 两台服务完好，
`chaosblades.chaosblade.io` 为 `No resources found`，cart CPU 回到 3m 基线。

---

## 一、MCP 挂载（原方案未覆盖，roadmap 风险 1）

### 1.1 mcp.json 从哪个路径读 —— 交接文档属实

**实验**：同时提供两份内容可区分的 `mcp.json`，并**故意**把
`BLADE_AI_MCP_CONFIG_PATH` 指向其中一份：

| 位置 | 配置里的 URL |
|---|---|
| `$BLADE_AI_CONFIG_DIR/mcp.json`（且 `BLADE_AI_MCP_CONFIG_PATH` 指向它） | `…/mcp-from-CONFIGDIR` |
| `$HOME/.blade-ai/mcp.json` | `…/mcp-from-HOME` |

**结果**：探针只收到 `GET /mcp-from-HOME`。

```
[INFO] httpx: HTTP Request: GET http://127.0.0.1:19901/mcp-from-HOME "HTTP/1.0 200 OK"
```

**结论**：0.7.0 读的是 **`$HOME/.blade-ai/mcp.json`**，
**完全无视 `BLADE_AI_MCP_CONFIG_PATH`**。交接文档 §2.1 这条准确。
平台要给 BladeAI 挂 MCP，**只能靠给服务进程设 `HOME`**。

另：`BLADE_AI_MCP_ENABLED=1` 是必需的开关——2026-09-11/12 两轮评测的
`env-qwen.sh` 里没有它，所以那两轮全程没有 MCP（95MB 服务端日志里零条 MCP 记录）。

### 1.2 **MCP 连不上是静默跳过**（新发现，比路径重要得多）

连接失败只是一条 WARNING，**服务照常启动、照常完整跑完回合**：

```
[WARNING] chaos_agent.mcp.manager: MCP server 'probe_home' failed to connect (skipping)
```

更要命的是**被测方对此零感知**。在 MCP 全部连接失败的服务上问它
"列出你当前可用的工具清单，并说明你能否访问 Kubernetes 只读接口和可观测数据"，
它跑了 692 条事件，列出一张完整的工具表——**全是它的内置工具**
（`kubectl_read` / `blade_status` / `blade_help` / `query_active_experiments` …），
**没有一个字提到有 4 个 MCP 服务器没连上**，还特地声明
"我先做一次只读连通性探测，用实际结果回答你，而不是凭猜测"。

**这对评测有效性是致命的**：

平台若挂 MCP 失败（配置错、网关未起、token 过期），现象是
"它只用内置工具，不用平台提供的观测通道"——**会被直接误判成 D7 的能力缺口**
（"观测受阻时不求助、不换路"），而真实原因是我们自己没配通。

评测报告第十一节第 1 条已经隐约触及（"只把备用工具存在于集群里而不让 Agent
能发现，不能形成有效 D7 能力测试"）。本次实测把它从建议升级为**已证实的机制**：
不是"没让它发现"，而是**连不上会静默跳过，且它完全无从感知**。

### 1.3 落地要求

1. **每个试验开始前，必须独立核验 MCP 工具确实在它的工具表里**，核验不通过不得开跑。
   现成的黑盒探针：起一轮只读回合问它"列出你可用的工具清单"，检查平台工具是否在列。
2. **服务启动器必须管理 `HOME`**，并在该 `HOME` 下写 `~/.blade-ai/mcp.json`。
   这件事原方案没有工作包承载，建议并入 WP-A 的服务启动器部分。
3. **D7 类用例的前置条件**：替代观测通道必须先被证实"在它的工具表里可见"，
   否则该用例判定为无效，而不是判它不及格。
4. 注意配置里 `transport` 写的是 `http`（现有 `/root/.blade-ai/mcp.json` 模板如此），
   而 `mcp_supervisor.py` 给 BladeAI 分配的是 SSE 端口与 `/sse` 路径——
   **两者是否一致，正式接线时必须核对**，本次未验证。

---

## 二、错误三分类的真样本（口径 9）

### 2.1 采集方式

两台独占服务，分别制造上游失败，跑同一个只读问题：

| 样本 | 制造方式 | 服务端真实原因 |
|---|---|---|
| `upstream_auth` | 无效 API key 打真网关 | `OpenAIInvalidRequestError` → `400 No connected db.` |
| `upstream_conn` | 网关地址指向不可达端口 | `OpenAIConnectionError` → `Connection error.` |

### 2.2 **结果推翻了 WP-C.4 的做法：上游失败根本不产生 `error` 事件**

两个样本的事件流**完全一致**，而且从平台视角看是一次**正常完成**的回合：

```
node_start(intent_clarification) → context_size
→ llm_start → llm_start → llm_start      ← 三次重试，全部无产出
→ node_end → done
```

| 观测项 | 值 |
|---|---|
| `error` 事件 | **0 条** |
| 终态 | **`done`** |
| returncode | **0** |
| 事件总数 | 7 |
| 耗时 | 0.7 秒 / 2.8 秒 |

真实原因**只在服务端日志里**（`resilient_llm: retries exhausted after 3 attempt(s)`），
黑盒拿不到。

**后果**：上游模型故障会**伪装成"回合正常完成但智能体什么都没做"**。
只看终态的话会判 0 分，而按已定口径应标记为**无效**。

### 2.3 可用的黑盒判据（WP-C.4 应改用这个）

原方案说"错误必须三分类落盘"，隐含前提是"有 `error` 事件可分类"。
实测表明这个前提不成立，判据必须换成**回合形态**：

| 分类 | 黑盒判据 |
|---|---|
| **我方取消** | 出现 `error`（内容 `Turn cancelled`）紧跟 `done`；且落盘里先有 `kind=driver` 的 `cancel_requested`；`/cancel` 返回体的 `cancelled` 列表含该 turn id |
| **上游模型错误** | 一个 node 内出现 **`llm_start` 却没有任何后续 `token` / `thinking` / `tool_start`**，随即 `node_end` + `done`。**连续多条 `llm_start` 无产出**＝重试耗尽，信号更强 |
| **智能体自身报错** | 暂无真样本（见下） |

正常回合的对照特征：`llm_start` 之后必然跟着 `thinking` 或 `token`。

### 2.4 仍缺的一档：智能体自身报错

全语料 10 条 `error` 全是 `Turn cancelled`；本次构造的上游失败不产生 `error`。
综合两者，**目前没有任何证据表明 `error` 事件会因智能体自身故障而产生**——
`error` 事件目前看**近似是"取消"专用**。

这一档**如实标记为未取得真样本**，不编造判据。
WP-C.4 实现时按"未知来源"兜底，不要臆断归类。

---

## 三、对方案的影响

| 影响 | 落在 |
|---|---|
| WP-C.4 判据从"分类 error 事件"改为"识别回合形态" | 方案 WP-C.4 |
| 服务启动器必须管理 `HOME` 与 `~/.blade-ai/mcp.json` | WP-A 补充 |
| 试验前必须核验 MCP 工具可见性，否则 D7 类用例无效 | WP-C / 评分口径 |
| `transport: http` 与 `mcp_supervisor.py` 的 SSE 是否一致，待核 | 正式接线 |
