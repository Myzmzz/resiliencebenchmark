# literature-v1：十二篇论文的精读笔记

这一轮读的是**"机制已经存在，为什么仍然失效"**这条线的论文，目的是给 `rules-v1`（规则库）和 `manifestations-v1`（表现目录）补方法论和证据。

**怎么用**：先看 [`synthesis.md`](synthesis.md)（跨论文综合：分类骨架、顶层叙述、实验方法、判据设计、可量化关系），再看 [`actions.md`](actions.md)（落到具体文件的动作清单，含已逐字验证的引文）。单篇笔记在 `notes/`。

## 一、十二篇的状态

| # | 论文 | 会议/年份 | doc_id | 缓存位置 | 笔记 |
|---|---|---|---|---|---|
| 1 | WASABI：重试缺陷 | SOSP 2024 | `STU-RETRY-STOICA24` | manifestations-v1 | [01](notes/01-wasabi-retry-sosp24.md) |
| 2 | Cancellation in Systems | OSDI 2022 | `STU-CANCEL-SETHI22` | manifestations-v1 | [02](notes/02-cancellation-osdi22.md) |
| 3 | Simple Testing Can Prevent... | OSDI 2014 | `STU-SIMPLETEST-OSDI14` | manifestations-v1 | [03](notes/03-simple-testing-osdi14.md) |
| 4 | Who Watches the Watchers? | NSDI 2026 | `LIT-OPERATOR-GU26` | **本目录（新抓）** | [04](notes/04-operator-nsdi26.md) |
| 5 | Towards Soft Circuit Breaking | arXiv 2021 | `LIT-SOFTCB-ARXIV21` | **本目录（新抓）** | [05](notes/05-soft-circuit-breaking-arxiv21.md) |
| 6 | Breakwater | OSDI 2020 | `LIT-BREAKWATER-OSDI20` | **本目录（新抓）** | [06](notes/06-breakwater-osdi20.md) |
| 7 | TopFull | SIGCOMM 2024 | `LIT-TOPFULL-SIGCOMM24` | **本目录（新抓）** | [07](notes/07-topfull-sigcomm24.md) |
| 8 | Microreboot | OSDI 2004 | `LIT-MICROREBOOT-OSDI04` | **本目录（新抓）** | [08](notes/08-microreboot-osdi04.md) |
| 9 | Gray Failure | HotOS 2017 | `STU-GRAYFAIL-HOTOS17` | manifestations-v1 | [09](notes/09-gray-failure-hotos17.md) |
| 10 | Metastable Failures in the Wild | OSDI 2022 | `STU-METASTABLE-OSDI22` | manifestations-v1 | [10](notes/10-metastable-osdi22.md) |
| 11 | ORCAS | ISSRE Workshops 2018 | `LIT-ORCAS-ISSREW18` | **本目录（新抓）** | [11](notes/11-orcas-issrew18.md) |
| 12 | Basic Concepts and Taxonomy | TDSC 2004 | `LIT-TAXONOMY-TDSC04` | **本目录（新抓）** | [12](notes/12-taxonomy-tdsc04.md) |

其中 1/2/3/9/10 五篇 `manifestations-v1/docs-cache/` 里已经有了，没有重复抓取；剩下 7 篇用同一套 `tools/fetch.py` 抓进本目录的 `docs-cache/`，抓取记录在 `tools/fetch-log.json`，**本轮 7 篇全部成功、无 unreachable**。

抓取需要 `pdfminer`，系统 python 没装，用仓库的 venv：

```bash
.venv/bin/python literature-v1/tools/fetch.py
```

## 二、这一轮最值得记住的六件事

1. **机制内部有通用的三阶段**（感知 → 判定 → 执行）。四篇独立研究不同机制的论文各自拆出了同样的结构。"该动不动"可以出在三个完全不同的位置，实验证据和修复方式都不同。→ `synthesis.md` §一
2. **你测的东西有个 2004 年就定型的名字：coverage**，而且 coverage 缺失分三层，正好对上灰色失效和亚稳态失效。→ `synthesis.md` §二
3. **强度是二维的**。2% 的 CPU 差、1 秒的时长差，能分开"完全恢复"和"永久失效"。单点强度的实验对"不恢复"类条目没有意义。→ `synthesis.md` §三.1
4. **判据必须超出平台 API**。OAT 找到的 86 个缺陷里 43% 只能靠应用特有的状态监视器和过程中可用性抓到；常规判据（崩溃、错误日志、K8s 状态对象）完全看不见。→ `synthesis.md` §四.2
5. **服务复苏 ≠ 数据正确**。这两件事必须分别判定，不能用前者代替后者。→ `synthesis.md` §四.3
6. **`cancellation b` 和 `retry b` 两个空缺格子其实有证据**，gaps.md 里那两条结论要订正。→ `actions.md` §A

## 三、目录结构

```
literature-v1/
├── README.md          本文件
├── synthesis.md       跨论文综合（先读这个）
├── actions.md         落到具体文件的动作清单（再读这个）
├── notes/             12 篇单篇笔记
├── docs-cache/        本轮新抓的 7 篇归一化文本 + raw/ 原始 PDF
└── tools/
    ├── fetch.py       从 manifestations-v1 复制，ROOT 指向本目录
    ├── sources.tsv    本轮抓取清单
    └── fetch-log.json 抓取结果
```

## 四、引文约定

沿用 `manifestations-v1` 的硬约束：**每条 quote 必须能在对应的 `docs-cache/<doc_id>.txt` 里逐字找到**。`actions.md` 里给出的所有引文都已用 `grep -o` 验证过。

两个已知的排版坑：

- **连字**：`ﬁxed`、`ﬂow` 等在 PDF 提取文本里是连字字符，`validate.py` 的折叠排版字符路径会以 WARN 通过；
- **数学斜体 Unicode**：`LIT-TOPFULL-SIGCOMM24` 的正文里公式变量是 `𝑘`、`𝐴𝑃𝐼1`、`𝑀𝐴` 这类数学字母符号，**现有的两种归一化都处理不了**。引用 TopFull 时要避开含这些字符的句子，或者给 validate.py 加一条数学字母符号到 ASCII 的映射。
