# 落到本仓库的具体动作

按"改哪个文件"组织。每条都给了理由和可直接使用的引文，引文已用 `grep -o` 在 docs-cache 里逐字验证过。

---

## A. `manifestations-v1/gaps.md` —— 两处订正

### A1. `cancellation b` 不是空缺

现文："Sethi OSDI 2022 全文、gRPC cancellation、Go context | 论文的五类反模式全是"取消没做到"，没有"误取消"的类别"

**问题**：五个**反模式**（§7）确实都是"没做到"，但**根因分类**（§5.1，Table 4）里有 `Excess cancel`，Java 7 条 + C# 1 条 = 8 条。

**可用引文**（`STU-CANCEL-SETHI22`，§5.1.3 Excess cancel，逐字验证通过）：

> sometimes triggers are correctly sensed and yet tasks are wrongly or unnecessarily canceled

三种形态与案例号：不冲突的任务被误取消（CASSANDRA-13142、CASSANDRA-15024）；冲突但优先级更高的任务被取消（HBASE-17674）；任务完成时取消了结果仍被需要的关联任务（roslyn-11470、HADOOP-6762）。

### A2. `retry b` 有具体案例了

现文："Stoica 的 IF 类里有'wrong retry policy'，但论文给的是统计分类不是可复现表现……**这个格子最接近可补**，缺的是一个具体案例"

**可用引文**（`STU-RETRY-STOICA24`，§2.2.1，逐字验证通过）：

> other code paths in Hadoop may wrap AccessControl Exception inside the more general HadoopException, with the latter always getting retried

注意 `AccessControl Exception` 中间那个空格是 PDF 排版造成的，缓存里就是这样，**照抄即可通过 validate.py 的空白折叠**。

另外三个案例：HADOOP-16580（重试 `IOException` 把子类 `AccessControlException` 裹进去）、ElasticSearch-53687（把任务取消当可恢复错误无限重试）、HIVE-23894（被取消的 TezTask 被重新入队）。后两个同时是 `retry × cancellation` 的交叉，更适合放 `interaction`。

---

## B. `manifestations-v1/` —— 可新增的条目（附现成引文）

以下每条的引文都已逐字验证。按你的字段约定还需要补 `trigger`、`intensity_lower_bound`、`upper_bound_note`、`observable_signals` 等，这里只给最难的那部分：可追溯的表现描述。

| 目标格子 | 来源 | 逐字引文 | 备注 |
|---|---|---|---|
| **`probe b`**（不该动乱动） | `LIT-OPERATOR-GU26` §4.2 | `This query statement is broken when MariaDB is configured with max_prepared_stmt_count=1, causing MariaDBOp to keep restarting healthy MariaDB instances` | MariaDBOp-1096。**探针的有效性依赖被测应用的配置**，这是个新形态 |
| **`probe b`**（另一条） | `LIT-OPERATOR-GU26` §4.2 | `the liveness probes reported false alarms due to timeout of the probes when applications were running slow` | 3 例。修法是加大超时、降低探测开销，论文明说这是绕开而非根治 |
| **`probe a`** | `LIT-OPERATOR-GU26` §4.2 | 就绪探针用近似信号：容器启动 [33]、DNS 解析 [46]、**TCP 连接成功但应用还在 booting**（MongoOp-1334） | 补你现有 probe a 的"近似信号"形态 |
| **`interaction c`**（你标注为找不到证据的格子） | `LIT-OPERATOR-GU26` §4.2 / Figure 4d | `resulting in infinite restarts` | KafkaOp：期望 `timeout.ms=1000`，读回 `"1000"`，`"1000" != 1000` → 写配置 + 重启，**每两分钟一轮**。这是"状态编码"与"重启"两个机制叠加造成的无界，单看任一机制解释不了 |
| **`interaction e`** | `LIT-OPERATOR-GU26` §4.4 | `permanent errors can cause the operator to hang, preventing critical operations like upscaling that could mitigate failures` | TiDBOp 把所有错误当瞬时的，无限等待，**挡住了本可缓解故障的扩容**。纯机制内部造成的不恢复 |
| **`retry a`**（控制器层） | `LIT-OPERATOR-GU26` §4.4 | `the operator did not handle errors—when the application returned an error code, the operator chose to exit` | CassOp-48：decommission 失败后**用户已经解决了磁盘问题，operator 再也没重试过**，缩容永久卡住 |
| **`interaction d`**（局部限流导致饥饿） | `LIT-TOPFULL-SIGCOMM24` §2 | `44.4% of APIs among those involved in overloaded microservices were potentially vulnerable to starvation` | ⚠️ **不要引 Figure 1 那段**，它含数学斜体 Unicode（𝑘、𝐴𝑃𝐼1、𝑀𝐴），你的 validate.py 归一化处理不了。这条统计结论是干净 ASCII |

### 关于 `interaction d` 这条的重要性

它是一个**你目录里现在完全没有的缺陷形态**：下游的保护动作把上游的资源变成浪费。上游成功率看着正常（它确实处理完了），下游拒绝率也正常（它在正确限流），**只有端到端 goodput 是低的，而且没有任何单个组件为此负责**。可观测信号是"上游完成数 > 端到端成功数"的差额。

---

## C. `manifestations-v1/taxonomy.md` —— 四处建议修改

### C1. 给 a/b 挂上谱系（§二，第一层的判据说明）

现在写的是"a 与 b 是一对镜像，区别在'机制动了没有'"。建议补一句来源：

`LIT-TAXONOMY-TDSC04` §3.3.1 明确写出检测机制自身的两种失效模式——**false alarm**（没有真的失效却发出功能丧失的信号）与 **unsignaled failure**（没有为功能丧失发出信号）。你的 b 对应前者，a 对应后者。同一篇的 **omission fault / commission fault**（该做的没做 / 做了错的）是另一条谱系。

### C2. e 类判据要拆成两级

`LIT-MICROREBOOT-OSDI04` §5.2 的逐字表述（验证通过）：

> restoring the system to a point from which it can resume the serving of requests for all users, without necessarily having ﬁxed the resulting database corruption

（注意 `ﬁxed` 是连字，你的 validate.py 会以 WARN 通过。）

**建议在 e 类判据里加**：e 类的"恢复"指服务复苏（resuscitation）；数据是否正确属于 f/g，两者必须分别判定。一个系统完全可能撤除故障后 SLO 全绿而账上留着重复扣款。论文还指出金融机构常常只追求复苏，营业日结束时用补偿事务修复不一致——**所以这不是理论区分，是行业实践**。

### C3. 给 d 类一个形式基础

`LIT-TAXONOMY-TDSC04` §3.5：**A 的服务失效对 B 而言表现为一个外部故障**，经 B 的使用接口把错误传进 B。这条递归关系正是"错误出现的位置和注入位置不一致"的精确解释——中间每一跳都在做"失效 → 故障"的转换，每次转换都可能改变错误的形态。你的 `amplification_chain` 记录的就是这条链的实例。

### C4. `experiment_hazard` 一节补一条

WASABI §3.1.4 的发现：**约 10% 覆盖重试逻辑的单元测试里，开发者手动把重试上限改成了 0、1 或 2**，所以工具专门写脚本改回默认值。

对应到你这里：**被测应用在 demo / 测试环境下的机制参数可能和生产不是一回事**。如果 harness 沿用示例 values 部署，"有没有重试"测出来的是装置的结论。建议作为 `experiment_hazard` 的第二个已知实例记下来。

---

## D. 新字段与新机制组

### D1. 新增正交字段 `defect_stage`（值域 `sense` / `decide` / `act`）

理由见 `synthesis.md` §一：四篇独立研究不同机制的论文各自把机制拆成了同样的三阶段（WASABI 的 IF/WHEN/HOW、Sethi 的发起/传播/落实、Larsson 的传感器/决策/执行器、Avižienis 的错误检测/错误处理+故障处理）。

**它让 a 类的判据能写死**：`sense` 阶段判 a 要证明信号根本没到机制；`decide` 阶段要证明信号到了但阈值没跨；`act` 阶段要证明判定已翻转但行为没变。三种在实验里要采不同证据，修复方式也完全不同——这正是评测智能体诊断能力的评分点。

成本低：56 条里大多数已经隐含写清了阶段，是标注而非重新抽取。

### D2. 考虑新增机制组 `reconcile`（调谐与管理操作）

现有 11 组全是"应用侧或平台侧的防护动作"，没有一组覆盖**执行恢复动作的控制器本身**。而 K8s 环境里副本数、滚动更新、故障转移、扩缩容的执行者就是控制器。

Gu 等（NSDI'26）412 起失效的支撑数据：交互缺陷占 52.2%（内部程序缺陷 25.2% 的两倍）；交互失效里 62.3% 后果灾难性；最大一类是与被管应用的交互（42%），而**现有测试与验证工具全是应用无关的，完全不覆盖这一块**。

它的缺陷形态归不进现有任何一组：违反应用操作语义（63.7%，细分配置 36.2% / 顺序 31.0% / 前置条件 19.0% / 执行环境 13.8%）、看不见应用内部状态（16.5%）、版本不兼容（12.1%）、误处理应用错误（7.7%）。

**如果这一轮不扩组，至少在 gaps.md 里记一笔**："存在一类本轮范围之外的机制——执行恢复动作的控制器，有 412 起实证失效支撑，见 literature-v1/notes/04-operator-nsdi26.md"。

### D3. `intensity_lower_bound` 建议补时长维度

Metastable 的实验证据（`STU-METASTABLE-OSDI22` §5.2.2，逐字验证通过）：

> a 2%-decrease in available CPU or a 1-second increase in duration separated successful recovery from a metastable failure

**单点强度的实验对 e 类条目没有意义。** 建议 e 类条目的 `intensity_lower_bound` 改成二元组 `(强度, 时长)`，或新增 `duration_lower_bound` 字段，并在实验约定里要求 e 类产出"稳定区 / 脆弱区 / 亚稳态区"的二维分区图。

---

## E. `harness/` 与 `evaluator/` —— 四条实验方法改进

1. **同一注入点跑两种注入次数**（K=1 和 K=100+）。K=1 让保护动作之后的代码真的执行，暴露"状态没清干净"；K=100+ 撑满上限与退避，暴露"无界 / 无退避"。一轮兼顾不了。（WASABI §3.1.2）

2. **e 类用"持久故障 → 声明新期望状态 → 撤除故障 → 检查最终收敛"协议**，而不是"注入 → 撤除 → 观察"。前者保证故障期间确实有未完成的工作。（OAT，NSDI'26 §A.3）

3. **判据分两层**：平台可见的状态对象一层；应用内部状态 + **状态转换全过程中的**可用性一层。后者需要为每个被测应用写状态监视器（例：`SHOW VARIABLES` → 解析 → 与期望配置比对）和周期性读写工作负载。判据：普通操作期间可用性不下降，注入瞬时故障时不低于 95%。成本参考：88–208 行 Python / 应用，接一个新应用约 8 人时。（OAT §5.1.3、§A.4）

4. **考虑引入 action-weighted throughput 作为 scoring 指标**：会话 → 用户动作 → 操作序列，动作以提交点结束，**提交点失败则该动作的所有操作追溯标记为失败**。它捕捉到普通成功率捕捉不到的事：故障期间"大部分请求成功但关键那步失败"和"少部分请求失败"，在用户看来完全不同。"提交点"还天然把 f 类（副作用）的判定挂了进来。（Microreboot §4.2）

---

## F. `rules-v1/` —— 三条

1. **"多数实践"型规则**。WASABI 的 IF 类判据用**代码库自身的多数行为当规范**：某异常在 N_E 个重试循环里被重试了 R_E 次，R_E/N_E ≥ 2/3 或 ≤ 1/3 的离群点就报。9 例报出 8 例为真。
   迁移：同一集群里同一依赖被 N 个调用方引用，N−1 个配了超时/重试、剩下那个没配 → 告警。**比"必须配超时"误报低得多，且能自动从被测系统挖出来。**你已经把"需求相对型"规则降级为弱判定了，这个思路给它一个更强的形式：不是相对于需求，而是相对于**同一系统内的多数实践**。

2. **Aspirator 的三条静态规则可以直接落**（Yuan, OSDI'14）：catch 块为空或只有一行日志；高层异常（`Exception`/`Throwable`）的 catch 块里调 `abort`/`System.exit()` 且实际会兜住多个低层异常；catch 块源码含 `TODO`/`FIXME`。
   配套的降误报启发式同样值得抄：try 块改了变量 V 且 catch 之后紧跟的块检查了 V → 不报；try 块最后一条是 `return`/`break`/`continue` 且 catch 之后的块非空 → 不报；允许忽略指定异常（论文忽略全部 `FileNotFound`）和排除 `shutdown`/`close`/`cleanup` 方法。
   **注意**：这些规则在 9 个系统上报出 121 个新缺陷，而那些系统已经在用 FindBugs 和自建的错误注入框架了。

3. **给"文档来源"的置信度打个折**。Gu 等（NSDI'26）Finding 14：46 个语义违反里 **13 个根本没有文档描述**（例如改 Cassandra 已有集群 `num_tokens` 的要求只存在于博客和经验报告里）；其余 33 个有文档但含糊（MariaDB 文档说重启前要"把所有客户端连接转移到其他节点"，没说具体怎么做；真正的前置条件是先做 primary stepdown）或**散落各处**（MariaDB 恢复操作的一个前置条件写在"已知问题"一节）。
   对你 rules-v1 的"从官方文档建规则"路线：**凡涉及"操作的前置条件"的规则，文档来源的置信度应该打折**，因为文档系统性地不足以定义正确的管理操作语义。

---

## G. `tools/sources.tsv` —— 下一轮该抓的

| doc_id 建议 | 文献 | 为什么 |
|---|---|---|
| `LIT-FISURVEY-CSUR16` | Natella, Cotroneo, Madeira, *Assessing dependability with software fault injection: A survey*, ACM CSUR 48(3), 2016 | 故障注入的权威综述，你目前完全没有这条线 |
| `LIT-RESBENCH-2012` | Vieira, Madeira, Sachs, Kounev, *Resilience benchmarking*, Springer 2012 | 韧性评测的方法学章节，ORCAS 的定义来源 |
| `LIT-LDFI-SOCC16` | Alvaro 等, *Automating failure testing research at internet scale*, SoCC 2016 | Netflix LDFI，相关工作绕不开的另一条线；ORCAS 说要与之定量比较 |
| `LIT-ACTO-*` | Sun 等，Acto（xlab-uiuc） | operator 测试的直接前作。若要扩 `reconcile` 组，应先读它 |

---

## H. 一个执行顺序建议

优先级从高到低：

1. **A1 + A2**（订正 gaps.md 两处判断）—— 最便宜，而且现在 gaps.md 里的两条结论是错的；
2. **C2**（e 类拆成服务复苏 / 数据正确两级判定）—— 影响所有 e 类条目的判定正确性；
3. **D3**（e 类补时长维度）—— 影响实验设计，越早改越好，不然已跑的 e 类实验都要重跑；
4. **B**（补 7 条新条目）—— 引文都已备好；
5. **E3**（判据分两层）—— 工作量最大但决定了 f/g 类能不能真的产出；
6. **D1**（`defect_stage` 字段）—— 标注工作，可以和 B 一起做；
7. 其余按需。
