#!/usr/bin/env python3
# 用途：由 tools/sources.tsv + tools/fetch-log.json 生成 documents.yaml；回填 stack.md 的组件表；
#       由 rules.yaml + advisories.yaml 生成 stats.md。第 2、5 步使用。
import json
import os
import re
from collections import Counter, defaultdict

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAYERS = {
    1: "容器编排", 2: "网关、代理与服务网格", 3: "RPC 与 HTTP 框架", 4: "容错库",
    5: "服务发现与配置中心", 6: "消息与流", 7: "数据存储客户端与连接池", 8: "语言运行时",
    9: "健康端点与应用框架", 10: "跨层准则与模式目录", 11: "已有的检查规则集",
}


def load(name):
    p = os.path.join(ROOT, name)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


def gen_documents(log):
    docs = []
    for doc_id in sorted(log):
        v = log[doc_id]
        e = {
            "doc_id": doc_id,
            "layer": v["layer"],
            "layer_name": LAYERS[v["layer"]],
            "component": v["component"],
            "title": v["title"],
            "url": v["url"],
            "source_type": v["source_type"],
            "version": v.get("version_hint") or "unstated-on-page",
            "retrieved": v["retrieved"],
            "fetch_status": v["status"],
        }
        if v["source_type"] == "archived-snapshot" and "/https://" in v["url"]:
            e["archived_from"] = "https://" + v["url"].split("/https://", 1)[1]
            e["archive_note"] = "官方站点已改为前端渲染/拒绝脚本访问，正文取自 Wayback 快照；按口径不计入 official-doc"
        if v["status"] == "ok":
            e["local_copy"] = v["cache"]
            e["chars"] = v["chars"]
            if v.get("last_modified"):
                e["http_last_modified"] = v["last_modified"]
            if v.get("final_url") and v["final_url"] != v["url"]:
                e["final_url"] = v["final_url"]
        else:
            e["error"] = v.get("error", "")
            e["local_copy"] = None
        docs.append(e)
    header = ("# 用途：文档清单——每份韧性相关官方文档/准则页面的来源、版本线索、取回日期、抓取状态与本地副本路径。\n"
              "# 由第 2 步产生，内容由 tools/gen_tables.py 依据 tools/sources.tsv 与 tools/fetch-log.json 生成，请勿手改。\n"
              "# version 字段是页面/URL 自报的版本线索；unstated-on-page 表示页面未标版本，不臆造。\n")
    with open(os.path.join(ROOT, "documents.yaml"), "w", encoding="utf-8") as f:
        f.write(header)
        yaml.safe_dump({"documents": docs}, f, allow_unicode=True, sort_keys=False, width=200)
    return docs


def comp_table(log, rules):
    rules_by_comp = Counter()
    for r in rules or []:
        for inst in r.get("instantiations", []) or []:
            rules_by_comp[inst["component"]] += 1
    by_layer = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for v in log.values():
        cell = by_layer[v["layer"]][v["component"]]
        cell[0] += 1
        if v["status"] == "ok":
            cell[1] += 1
    out = ["| 层 | 层名 | 组件 | 文档数(成功/总) | 已有规则数 |", "|---|---|---|---|---|"]
    for layer in sorted(by_layer):
        for comp in sorted(by_layer[layer]):
            tot, ok = by_layer[layer][comp]
            n = rules_by_comp.get(comp, 0)
            if n:
                cell = str(n)
            elif layer == 10:
                cell = "n/a（准则层，不作为 instantiations 的组件）"
            else:
                cell = "0 ⚠"
            out.append("| %d | %s | %s | %d/%d | %s |" % (
                layer, LAYERS[layer], comp, ok, tot, cell))
    return "\n".join(out)


def patch_stack(block):
    p = os.path.join(ROOT, "stack.md")
    s = open(p, encoding="utf-8").read()
    s = re.sub(r"(?s)(<!-- BEGIN:components -->\n).*?(<!-- END:components -->)",
               lambda m: m.group(1) + block + "\n" + m.group(2), s)
    open(p, "w", encoding="utf-8").write(s)


def parse_mechanisms():
    """从 stack.md 的机制表里取 (组, 机制) 清单。"""
    p = os.path.join(ROOT, "stack.md")
    s = open(p, encoding="utf-8").read()
    mechs, group = {}, None
    for line in s.split("\n"):
        m = re.match(r"^### 组 \d+ (\S+)（", line)
        if m:
            group = m.group(1)
            continue
        if group and line.startswith("| ") and not line.startswith("| 机制") and not line.startswith("|---"):
            name = line.split("|")[1].strip()
            name = re.sub(r"^\*\*\[新增\]\s*(.*?)\*\*$", r"\1", name)
            if name and name != "机制":
                mechs[name] = group
        if line.startswith("## ") and group:
            group = None
    return mechs


def gen_stats(log, rules, advisories):
    mechs = parse_mechanisms()
    docs_ok = [v for v in log.values() if v["status"] == "ok"]
    L = []
    A = L.append
    A("<!-- 用途：规则库的统计与覆盖情况。由第 5 步产生，内容由 tools/gen_tables.py 生成，请勿手改。 -->\n")
    A("# stats.md — 统计与覆盖\n")
    A("## 1. 各层组件数与文档数\n")
    A("| 层 | 层名 | 组件数 | 文档数(总) | 抓取成功 | 抓取失败 |")
    A("|---|---|---|---|---|---|")
    per_layer_docs = defaultdict(list)
    for v in log.values():
        per_layer_docs[v["layer"]].append(v)
    for layer in sorted(per_layer_docs):
        vs = per_layer_docs[layer]
        comps = len({v["component"] for v in vs})
        ok = sum(1 for v in vs if v["status"] == "ok")
        A("| %d | %s | %d | %d | %d | %d |" % (layer, LAYERS[layer], comps, len(vs), ok, len(vs) - ok))
    A("| **合计** | | **%d** | **%d** | **%d** | **%d** |" % (
        len({v["component"] for v in log.values()}), len(log), len(docs_ok), len(log) - len(docs_ok)))

    nclause = sum(len(r.get("sources", [])) for r in rules) + sum(len(a.get("sources", [])) for a in advisories)
    A("\n## 2. 条款、规则、advisory 总数\n")
    A("| 项 | 数 |")
    A("|---|---|")
    A("| 抓取成功的文档 | %d |" % len(docs_ok))
    A("| 引用到的条款（rules + advisories 的 sources 条目） | %d |" % nclause)
    A("| 规则（rules.yaml） | %d |" % len(rules))
    A("| advisory（advisories.yaml） | %d |" % len(advisories))
    A("| 被规则引用到的文档 | %d |" % len({s["doc_id"] for r in rules for s in r.get("sources", [])}))
    A("| 规则里出现的组件实例（instantiations 条目） | %d |" % sum(len(r.get("instantiations", []) or []) for r in rules))

    A("\n## 3. 按机制组分布\n")
    A("| 机制组 | 规则数 | advisory 数 |")
    A("|---|---|---|")
    g_r = Counter(r["mechanism_group"] for r in rules)
    g_a = Counter(a.get("mechanism_group", "?") for a in advisories)
    order = []
    for g in mechs.values():
        if g not in order:
            order.append(g)
    for g in order:
        A("| %s | %s | %d |" % (g, g_r.get(g, 0) or "0 ⚠", g_a.get(g, 0)))
    extra = set(g_r) - set(order)
    for g in sorted(extra):
        A("| %s (不在 stack.md 的组里!) | %d | %d |" % (g, g_r[g], g_a.get(g, 0)))

    A("\n## 4. 按缺陷类别分布\n")
    A("| 缺陷类别 | 含义 | 规则数 |")
    A("|---|---|---|")
    names = {1: "规范明示型", 2: "需求相对型", 3: "组合型", 4: "实现错误型"}
    c = Counter(r["defect_class"] for r in rules)
    for k in (1, 2, 3, 4):
        A("| %d | %s | %d |" % (k, names[k], c.get(k, 0)))

    A("\n## 5. 按来源类型分布（规则的 sources 条目落在哪类文档上）\n")
    A("| 来源类型 | 条款条目数 | 涉及规则数 |")
    A("|---|---|---|")
    st_of = {k: v["source_type"] for k, v in log.items()}
    cnt = Counter()
    rules_of = defaultdict(set)
    for r in rules:
        for s in r.get("sources", []):
            t = st_of.get(s["doc_id"], "?")
            cnt[t] += 1
            rules_of[t].add(r["rule_id"])
    for t in sorted(cnt):
        A("| %s | %d | %d |" % (t, cnt[t], len(rules_of[t])))

    A("\n## 6. 规则的支撑构成\n")
    A("| 支撑构成 | 规则数 | 说明 |")
    A("|---|---|---|")
    only_linter, has_official, only_guideline, only_archived = 0, 0, 0, 0
    for r in rules:
        ts = {st_of.get(s["doc_id"], "?") for s in r.get("sources", [])}
        if "official-doc" in ts:
            has_official += 1
        elif ts & {"curated-guideline", "pattern-catalog"}:
            only_guideline += 1
        elif ts == {"linter-ruleset"}:
            only_linter += 1
        elif ts == {"archived-snapshot"}:
            only_archived += 1
    A("| 有官方文档原文支撑 | %d | sources 里至少一条 official-doc |" % has_official)
    A("| 只有准则/模式目录支撑 | %d | 没有 official-doc，只有 curated-guideline / pattern-catalog |" % only_guideline)
    A("| 只有现成规则集支撑 | %d | 只有 linter-ruleset |" % only_linter)
    A("| 只有归档快照支撑 | %d | 只有 archived-snapshot（AWS Builders Library 等，按口径不算官方文档） |" % only_archived)
    used_archived = {r["rule_id"] for r in rules
                     if any(st_of.get(s["doc_id"]) == "archived-snapshot" for s in r.get("sources", []))}
    A("| 用到了归档快照（不论是否还有别的支撑） | %d | |" % len(used_archived))
    used_linter = {r["rule_id"] for r in rules
                   if any(st_of.get(s["doc_id"]) == "linter-ruleset" for s in r.get("sources", []))}
    A("| 用到了现成规则集（不论是否还有别的支撑） | %d | |" % len(used_linter))

    A("\n## 7. 每个机制组 / 每个机制的规则数（空缺用 ⚠ 标出）\n")
    A("| 机制组 | 机制 | 规则数 |")
    A("|---|---|---|")
    m_r = Counter(r["mechanism"] for r in rules)
    for name, g in mechs.items():
        n = m_r.get(name, 0)
        A("| %s | %s | %s |" % (g, name, n if n else "0 ⚠"))
    unknown = set(m_r) - set(mechs)
    for name in sorted(unknown):
        A("| (不在 stack.md 的机制清单里!) | %s | %d |" % (name, m_r[name]))

    A("\n## 8. 每层的规则数（按 instantiations 命中的组件所在层统计）\n")
    A("| 层 | 层名 | 规则数 | 空缺 |")
    A("|---|---|---|---|")
    layer_of = {}
    for v in log.values():
        layer_of.setdefault(v["component"], v["layer"])
    per_layer = defaultdict(set)
    for r in rules:
        for inst in r.get("instantiations", []) or []:
            lay = layer_of.get(inst["component"])
            if lay:
                per_layer[lay].add(r["rule_id"])
    for layer in sorted(LAYERS):
        n = len(per_layer.get(layer, ()))
        if layer == 10:
            note = "n/a——准则层，它的文档作为 sources 出现（见第 5 节），不作为 instantiations 的组件"
        else:
            note = "⚠ 无规则" if n == 0 else ""
        A("| %d | %s | %d | %s |" % (layer, LAYERS[layer], n, note))

    A("\n## 9. 抓取失败的文档\n")
    fails = [v for v in log.values() if v["status"] != "ok"]
    if not fails:
        A("无。")
    else:
        A("| doc_id | 组件 | 网址 | 失败原因 |")
        A("|---|---|---|---|")
        for v in sorted(fails, key=lambda x: x["doc_id"]):
            A("| %s | %s | %s | %s |" % (v["doc_id"], v["component"], v["url"], v.get("error", "")))
    open(os.path.join(ROOT, "stats.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")


def main():
    log = json.load(open(os.path.join(ROOT, "tools", "fetch-log.json"), encoding="utf-8"))
    gen_documents(log)
    rules = (load("rules.yaml") or {}).get("rules", []) or []
    advisories = (load("advisories.yaml") or {}).get("advisories", []) or []
    patch_stack(comp_table(log, rules))
    gen_stats(log, rules, advisories)
    print("documents.yaml: %d docs (%d ok)" % (len(log), sum(1 for v in log.values() if v["status"] == "ok")))
    print("stack.md component table patched; stats.md written")
    print("rules: %d, advisories: %d" % (len(rules), len(advisories)))


if __name__ == "__main__":
    main()
