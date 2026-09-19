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
| `rules.yaml` | 第 4 步 | 规则登记表，66 条 | 手写 |
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
- **`validate.py` 的 quote 匹配**：先做逐字（精确子串）匹配，失败再做"空白归一化后"匹配并报警告。当前 202 条引用全部是逐字命中，0 条走了归一化通道。

### 2.4 机制清单与层次的扩展

见 `stack.md` 第 2、3 节。新增三个机制（冷启动容量与预热、连接保活与死连接探测、容器重启策略），各自注明了出处；十组分组与 11 层分层都没有增删。

## 3. 需要人拍板的问题清单

1. **回退路径到底推不推荐**：AWS Builders' Library 的《Avoiding fallback in distributed systems》说 "we now almost always prefer alternatives to fallback"，而 Azure 的 Circuit Breaker 模式和各容错库都把 fallback 当标准配置——当前处理是保留"回退路径"这个机制，但规则写成 `R-FALLBACK-001`（回退必须常态演练）而不是"必须有回退"，要不要更进一步，把"存在未经演练的 fallback"直接判为缺陷？
2. **副本数阈值取几**：kube-score 默认 ≥2，kube-linter 建议 ≥3，Polaris 只在等于 1 时告警。`R-REPL-001` 现在把三个阈值都记在 `defaults` 里，实际判定留白，需要定一个。
3. **HPA `minReplicas` 阈值**同上：kube-linter 要求 ≥3，Kubernetes 官方文档没给建议值。
4. **`maxEjectionPercent` 默认 10% 在小副本数下等于不生效**：3 副本时 10% 连一个都摘不掉。`R-OUTLIER-002` 只写了"要有上限"，要不要额外加一条"剔除比例上限 × 副本数必须 ≥ 1"？这条会和 `R-HEALTH-002`（全体不健康要放行）有张力。
5. **"需求相对型"规则怎么落地**：`R-TIMEOUT-002`、`R-POOL-001`、`R-RES-002` 这类要拿到被测系统自己的 SLO/预算才能判。是要求被测系统提供一份预算声明，还是退而求其次只做"有没有显式配置"的弱判定？
6. **Dubbo 的官方文档现状**：`cn.dubbo.apache.org` 上的 XML 配置参考页面自带"此文档已经不再维护。您当前查看的是快照版本"的提示，但它是目前唯一能拿到带默认值的配置表的官方页面（`.../overview/mannual/java-sdk/reference-manual/config/properties/` 那份不带默认值）。默认值继续引它，还是标为不可引用？
7. **Istio 的 `istioctl analyze` 基本用不上**：147 条分析器消息里只有 IST0130/IST0131 沾韧性的边，其余是 schema 校验和引用有效性。要不要把它从第 11 层的"现成规则集"里降级为参考？
8. **AWS Builders' Library 走的是 Wayback 快照**：`aws.amazon.com/builders-library/*` 现在 302 到 `builder.aws.com` 的前端渲染页，curl 拿不到正文，5 篇都引的 `https://web.archive.org/web/2024/...` 快照。快照算不算"官方文档"，还是要标成另一种来源类型？
9. **缺陷类别 2（需求相对型）和 3（组合型）的边界**：`R-QUORUM-001`（replication.factor / min.insync.replicas / acks 三者组合）现在记为 3，但它也可以说是"相对系统的持久性要求"的 2。需要一条判定口径。
10. **中文技术栈的官方文档覆盖偏薄**：Sentinel、Nacos、Dubbo、RocketMQ 加起来只有 11 份文档、规则数明显少于 Kubernetes/Envoy 系。是接受这个偏差，还是补抓 Spring Cloud Alibaba、Seata、Apache ShenYu 等再做一轮？
