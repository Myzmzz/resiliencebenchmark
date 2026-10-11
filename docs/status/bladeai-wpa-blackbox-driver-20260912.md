# WP-A 黑盒 HTTP/SSE 驱动器 — 实施与验收记录（2026-09-12）

对应方案 `docs/design/bladeai-070-blackbox-integration-plan-20260912.md` 的 WP-A。
本文只记录**传输层**结论；语义映射（WP-B）、确认桥（WP-C）尚未实施。

---

## 一、做了什么

新增 `harness/bladeai_http/`，与 `harness/agent_exec/`（subprocess 旁路传输）平级：

| 文件 | 内容 |
|---|---|
| `protocol.py` | 公开线契约：端点、事件词表、SSE 分帧、批准白名单 |
| `client.py` | 驱动器：建会话 / 起轮（SSE）/ 两级确认通道 / 取消 / 落盘 |

接入方式是 `HarnessSession` **已有的** `turn_executor` 缝隙
（`stage2_service/session.py:439`，`harness/agent_exec/client.py:186` 已用同一缝隙）。
**未改动任何共享代码**：`session.py`、`simulated_user.py`、`node_evaluation.py`、
`fault_inventory.py` 均未修改，因此其他三家不存在回归面。

---

## 二、验收结论

方案对 WP-A 的验收要求：*不接评分逻辑，能把一次 L0 的完整事件流落盘，每条带接收时间戳*。

### 2.1 真实 L0 重放（离线）

夹具 `tests/fixtures/harness_streams/golden/bladeai_L0.sse`，
取自评测机 `/root/bladeai-eval/runs/L0/events.jsonl`（会话 `sess_8a686003edf5`）。

- L0 实为**五个回合**（582 条事件，各以自己的 `done` 收束），一次 `POST /turn` 只返回一个回合
- 五回合逐条转发、顺序一致、字节一致；落盘每条带 `received_at` 与 `elapsed_ms`
- 在 1 / 13 / 997 / 65536 / 整块五种分块下，转发字节流**完全一致**

### 2.2 活服务联调（在线，旧环境 `1.94.151.57`）

用本驱动器驱动一台专用 BladeAI 0.7.0 服务（自起 8299 端口、独立配置目录）：

| 项 | 结果 |
|---|---|
| 会话 | `sess_93620ae643e0` |
| 事件 | **250 条**，约 28 秒 |
| 终态 | `done`，returncode 0，未超时未取消 |
| 落盘 | 250 条全部带 `received_at`，`elapsed_ms` 单调 |
| 工具调用 | 5 次 `kubectl_read`，**只读** |
| 集群影响 | 无注入、无对象修改；chaosblade CR 为空 |

题面为只读查询并明确要求不注入故障，被测方答"未注入任何故障、未修改任何集群对象"，
与独立核验一致。验收通过。

---

## 三、与交接文档不符之处（**影响 WP-C，务必先读**）

以下三条按 17 个用例、197,687 条事件的全量实测为准，**不是推测**。

### 3.1 `interrupt_id` 在事件流里根本不存在

交接文档 §2.2 / §2.4 称意图关卡是"事件带 `interrupt_id`"。
**全语料 `interrupt_id` 出现 0 次**，今天的活服务联调同样 0 次。

真实情况：要传给 `/interrupt` 的 `interrupt_id`，**取自事件自身的 `task_id` 字段**
（形如 `turn-1b4f6902e392`）。评测机 `runs/L0/driver.log` 正是这样取用的——
它记录的 `"interrupt_id": "turn-1b4f6902e392"` 与该 confirm 事件的 `task_id` 逐字相同。

> **踩坑后果**：按字面去找 `interrupt_id` 键的实现会一个都找不到，
> 于是永远不应答意图关卡，表现为静默等待（默认 6 小时）。

### 3.2 两级关卡都是 `type=confirm`，靠 `node` 区分；而且有第三级

| `node` | 全语料出现 | 通道 |
|---|---|---|
| `intent_confirm` | 20 | `POST /api/v1/sessions/{sid}/interrupt` |
| `confirmation_gate` | 17 | `POST /api/v1/confirm/{task_id}` |
| `tool_screener` | 1（D8-B） | **文档未提及** |

第三级 `tool_screener` 的 `payload.type` 是 `target_change`，内容是"目标漂移"复核
（D8-B 实例：`scope drift: approved=pod effective=chaosblade`），带 `original` / `proposed`
两个目标结构。WP-C 必须决定这一级怎么答，否则 D8 类用例会卡住。

### 3.3 语料里 10 条 `error` **全部**是 "Turn cancelled"

即全部是评测方自己的 `/cancel`，**没有一条是被测方失败**。
（交接文档称 D2 的 error 是上游模型 400；那条在 `runs/D2-incomplete-20260911-2234/` 里，
补跑后的 `runs/D2/` 已是 "Turn cancelled"。）
这坐实了 WP-C.4 错误三分类的必要性：不分类就会把平台介入 100% 误记成被测方失败。

**本驱动器已为此留好依据**：落盘文件用 `kind` 区分 `event`（服务端原样）与
`driver`（我方动作），我方 `cancel_requested` 记录先于它引发的 `error` 事件，
归因证据在同一条时间线上。

---

## 四、运维注意

- **`cancel` 是服务级而非会话级**（交接文档 §2.2 属实）。因此每个试验必须独占一台服务实例；
  本次联调专门另起 8299 端口、独立配置目录，未复用评测机上已有的 8199 / 8089 服务。
- **BladeAI 只发 `data:` 字段，不发 `event:` 名**，事件种类在 JSON 体的 `type` 里；
  行尾 LF，空行分帧。驱动器仍实现了完整 SSE 规范（CR/CRLF、多行 data、注释行保活），
  因为这是上游可以随时改而不算破坏契约的部分。
- **分块解码必须跨块保持状态**。事件流含中文提示与工具输出，逐块独立 `decode` 会把
  跨分块边界的多字节字符变成 U+FFFD，且**事件条数不变**——只有逐字节比对才看得见。
  已修复并加回归测试（逐个切点 + 逐字节喂入）。

---

## 五、测试与部署

- 本包 44 项测试：`tests/test_bladeai_http_protocol.py`(19)、
  `tests/test_bladeai_http_client.py`(16)、`tests/test_bladeai_http_replay.py`(9)，全绿
- 全量 `tests/` 仅 `test_system_snapshot.py::test_observation_adapter_uses_fixed_service_proxy_queries`
  失败；已在基线提交 `07b3e9d` 的干净 worktree 上复现同样失败
  （`configured kubeconfig does not exist`），属本机环境问题，与本次改动无关
- **部署情况**：未部署。驱动器尚未接入 `run_harness_trial.py` 的正式链路，
  这一步属于 WP-B（适配器重写）之后的接线工作
