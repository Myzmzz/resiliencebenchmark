<!-- 用途：rules-v1/ 各文件的用途说明、相对任务书的字段与流程扩展（含理由和出处）、以及需要人拍板的问题清单。 -->

# CHANGES.md — 文件清单、扩展说明与待拍板问题

分支：`rules-v1-buildout`（从 `codex/stage2-env3-bladeai-20260912` 切出）。所有改动只在 `rules-v1/` 目录内，未触碰其他文件。

## 1. 文件用途

| 文件 | 由哪一步产生 | 用途 | 手写还是生成 |
|---|---|---|---|
| `stack.md` | 第 1 步 | 韧性机制清单（十组 + 新增项 + 排除项 + 归纳来源）与技术栈分层组件表 | 正文手写；末尾组件表由 `tools/gen_tables.py` 生成 |
| `documents.yaml` | 第 2 步 | 文档清单：每份文档的层、组件、标题、网址、版本线索、取回日期、来源类型、抓取状态、本地副本路径 | 全部由 `tools/gen_tables.py` 依据 `tools/sources.tsv` + `tools/fetch-log.json` 生成 |
| `docs-cache/*.txt` | 第 2 步 | 文档的归一化文本副本，供逐字引用与 `validate.py` 回查 | 由 `tools/fetch.py` 生成 |
| `docs-cache/raw/*.html` | 第 2 步 | 抓取到的原始页面，改解析规则时可离线重放，不必重抓 | 由 `tools/fetch.py` 生成 |
| `rules.yaml` | 第 4 步 | 规则登记表，69 条 | 手写 |
| `advisories.yaml` | 第 4 步 | 写不出可核对检查条件的条款，8 条 | 手写 |
| `validate.py` | 第 5 步 | 校验脚本：字段齐全、doc_id 存在、quote 逐字可查、机制与组件都在 `stack.md` 里 | 手写 |
| `stats.md` | 第 5 步 | 统计与覆盖情况 | 由 `tools/gen_tables.py` 生成 |
| `tools/sources.tsv` | 第 2 步 | 待抓文档清单（抓取的输入） | 手写 |
| `tools/fetch.py` | 第 2 步 | 抓取 + 文本归一化 + `--reparse` 离线重解析 | 手写 |
| `tools/fetch-log.json` | 第 2 步 | 抓取结果原始记录（状态、字符数、版本线索、Last-Modified） | 由 `tools/fetch.py` 生成 |
| `tools/gen_tables.py` | 第 2、5 步 | 生成 `documents.yaml`、回填 `stack.md` 组件表、生成 `stats.md` | 手写 |
| `CHANGES.md` | 本文件 | 文件用途、扩展说明、待拍板问题 | 手写 |

## 2. 相对任务书的扩展（都不改"原文逐字引用"和"不用事故报告当来源"这两条）

### 2.1 `rules.yaml` 新增/改形的字段

| 字段 | 改动 | 理由 |
|---|---|---|
| `checks.static[].checkability` | 取值集合扩到 `static-code / static-config / static-manifest / static-cross-object / needs-requirement / needs-runtime` | 任务书给了 `static-code / static-config / needs-requirement` 三种。`static-manifest` 是为 Kubernetes 这类"既不是代码也不是应用配置、而是编排清单"的对象单列；`static-cross-object` 是因为有一批规则必须同时看两个对象才能判（PDB 与 Deployment 的副本数、PDB 与 HPA 的下限、探针端口与 containerPort、客户端连接池上限与服务端连接上限），这类检查的实现方式和单对象检查完全不同，混在一起会让后续做静态检查器时分不清。 |
| `parameter_relation.defaults` | 值从裸标量改成 `{value, doc_id}` | 任务书要求"只写文档里明确给出的默认值，并注明 doc_id"。写成结构体后 `validate.py` 能逐条强制校验这个要求，而不是靠人自觉。 |
| `instantiations[].config_or_code` | 取值加 `manifest` 和 `both` | `manifest` 对应 Kubernetes 清单；`both` 对应"配置和代码两处都要改才生效"的情况（例如多层重试的清点、幂等键既要服务端实现也要客户端传）。 |
| `fault_types` | 取值集合固定为 10 种（`dependency-delay / dependency-unavailable / dependency-error / packet-loss / instance-kill / cpu-pressure / memory-pressure / disk-pressure / network-partition / traffic-surge`），由 `validate.py` 强制 | 任务书只给了 `dependency-delay` 一个示例。固定取值集合是为了后面按故障类型反查"哪些规则会被这种注入触发"时不出现同义异名。 |
| `notes` | 保留，用于记录跨文档冲突、易错点、组合型规则的说明 | 任务书标为可选。 |
| `checks.static[].strong_form` / `strong_form_needs` | 新增一对字段 | 2026-09-19 定：需求相对型的检查一律降级为"有没有显式配置"的弱判定。原来那句需要 SLO/容量/对端配置才能判的强条件不能就此丢掉，挪到 `strong_form`，并用 `strong_form_needs` 写清缺的是哪项外部输入，将来拿到预算声明可以直接升回强判定。`validate.py` 强制：写了 `strong_form` 必须写 `strong_form_needs`；`checkability` 再出现 `needs-requirement` 直接报错。 |
| `checks.static[].adopted_threshold` | 新增字段 | 三个规则集的阈值不一致时（副本数、HPA minReplicas），记录最终采用哪个口径、以及不采用哪些，避免以后有人按 defaults 里记的别的阈值去判。 |

### 2.2 `advisories.yaml` 的字段

比 `rules.yaml` 少 `checks`、`parameter_relation`、`instantiations`、`defect_class`、`fault_types`（既然核对不了，这些字段填了也是假的），多一个 `why_not_checkable`，写清楚为什么核对不了、以及这条条款里可核对的那一面是否已经落进某条 rule。

### 2.3 流程上的扩展

- **加了 `tools/` 子目录**：抓取脚本、文档清单、抓取日志、生成脚本。任务书只点名了 `validate.py`，但抓 156 份文档不写脚本没法做，而且"引用必须能在本地副本里逐字找到"这条只有把抓取、解析、校验串成一条可重跑的链路才守得住。
- **`docs-cache/` 存了 raw HTML**：任务书只要求存文本副本。存原始页面是因为解析规则在过程中改过三次（段内硬换行要合并、标题要标记、纯文本文件不能走 HTML 解析），每次都能用 `fetch.py --reparse` 从本地重放，不用重新抓站。三次重解析后 202 条引用全部仍然逐字命中，也反过来说明解析是稳定的。
- **文本归一化的三条规则**（影响"逐字"的含义，必须说明）：
  1. HTML 源码里的换行按 HTML 语义当作空白合并，一个段落落在一行里；`<pre>` 块内的换行保留。
  2. `h1`–`h6` 标题前加 `[[Hn]]` 标记，用来给 `location` 字段定位；引用正文时不会碰到这个标记。
  3. 表格 `<td>/<th>` 用 ` | ` 分隔，一行一 `<tr>`，这样参数默认值表能整行引用。
  URL 后缀是 `.md/.adoc/.txt/.rst` 或来自 `raw.githubusercontent.com` 的按纯文本处理，保留原有换行。
- **来源类型新增 `archived-snapshot`**：2026-09-19 定，Wayback 快照不算官方文档。AWS Builders' Library 5 篇与 MySQL Connector/J 1 篇改成这个类型（官方站点已改成前端渲染或拒绝脚本访问，正文只能从快照取）。`documents.yaml` 里额外记 `archived_from`（原始网址）和 `archive_note`；`stats.md` 的"支撑构成"表给它单列了两行，不再混进"有官方文档原文支撑"。这 6 份仍可引用，只是不计入 official-doc。
- **`validate.py` 的 quote 匹配**：先做逐字（精确子串）匹配，失败再做"空白归一化后"匹配并报警告。当前 202 条引用全部是逐字命中，0 条走了归一化通道。

### 2.4 机制清单与层次的扩展

见 `stack.md` 第 2、3 节。新增三个机制（冷启动容量与预热、连接保活与死连接探测、容器重启策略），各自注明了出处；十组分组与 11 层分层都没有增删。

## 3. 已定的口径（2026-09-19）

| # | 问题 | 结论 | 落在哪里 |
|---|---|---|---|
| 1 | 回退路径推不推荐（AWS 说几乎别用，Azure 与各容错库当标配） | 更狠一点：**只在故障时才会被走到的 fallback，本身就判为缺陷**，要么让它承担常态流量/有定期演练/有覆盖它的集成测试，要么去掉它去加固主路径 | `R-FALLBACK-001` 的 statement 与 S1 已改写 |
| 2 | 副本数阈值取几 | 采用 Polaris 口径：**只判"等于 1"**，不套用 kube-score 的 ≥2 与 kube-linter 的 ≥3 | `R-REPL-001` S1；两个更严的阈值仍记在 `defaults` 里备查 |
| 3 | HPA `minReplicas` 阈值 | 同上，**只判"等于 1"** | `R-HPA-001` S1；`defect_class` 随之从 2 改为 1（判据不再需要外部输入） |
| 4 | 需求相对型规则怎么落地 | **降级为弱判定**：执行的检查只问"有没有显式配置"，需要 SLO/容量/对端配置的强条件记进 `strong_form` + `strong_form_needs` | 16 处 `needs-requirement` 全部改写；`validate.py` 加了守卫 |
| 5 | Dubbo 自称"不再维护的快照版本"的配置页还引不引 | **不引**。4 份 XML 配置页已从文档清单删除，本地副本一并删掉 | 连带删掉了 3 个 Dubbo 默认值与 1 条 Dubbo 引用；维护中的 `DOC-DUBBO-CONFIG` 与 `DOC-DUBBO-API-CONFIG` 里没有 timeout/retries 默认值，这些默认值就不写了，`instantiations` 改成"需按所用版本确认" |
| 6 | AWS Builders' Library 的 Wayback 快照算不算官方文档 | **不算**，改用新来源类型 `archived-snapshot` | 6 份文档改型；`stats.md` 支撑构成表单列 |
| 7 | 中文栈文档偏薄要不要补 | **补**。第二轮加了 13 份：Seata 5、ShenYu 3、Sentinel 集群流控与网关限流 2、Nacos Java SDK 容灾与配置项 2、Dubbo 维护中的 API 配置页 1 | 新增 3 条规则（`R-ADMIT-004`、`R-DISCOVERY-002`、`R-SAGA-002`）与 8 处实例化 |
| 8 | 缺陷类别 2 和 3 的边界口径 | 写成四问判定表，优先级 2 > 3 > 1 > 4：先问"判定所需信息是不是全在被测系统的代码与配置里"，不是就判 2；是、但要跨对象才判得出就判 3 | 口径正文在 `stack.md` 第 0 节；按它复核后只改了 `R-HPA-001`（2→1），`R-QUORUM-001` 确认为 3，`R-FD-001` 补了边界说明 |

口径 8 有一点要强调：**类别是按规则的正确性判据定的，与检查条件后来被弱化与否无关**。所以口径 4 把 16 处检查降级之后，那些规则的 `defect_class` 仍然是 2——判据依旧需要外部输入，只是暂时不执行强判定。`R-HPA-001` 改成 1 是因为口径 2/3 换掉了判据本身（从"够不够容错"换成"是不是 1"），不是因为检查被弱化。

## 4. 仍需要人拍板的

1. **`maxEjectionPercent` 默认 10% 在小副本数下等于不生效**：3 副本时一个都摘不掉。要不要在 `R-OUTLIER-002` 上再加一条"剔除比例上限 × 副本数 ≥ 1"？这条会和 `R-HEALTH-002`（全体不健康要放行）形成张力，两条都加需要说明谁优先。
2. **Istio 的 `istioctl analyze` 要不要从"现成规则集"降级为参考**：147 条分析器消息里只有 IST0130/IST0131 沾韧性的边，其余是 schema 校验和引用有效性，目前它在第 11 层占一个位置但没贡献规则。
