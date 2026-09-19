#!/usr/bin/env python3
# 用途：校验 rules.yaml / advisories.yaml——字段齐全、doc_id 存在且抓取成功、每条 quote 在
#       docs-cache/ 的本地副本里逐字可查、instantiations 的组件与 mechanism 都在 stack.md 里有。
# 由第 5 步产生。运行：python3 rules-v1/validate.py
import json
import os
import re
import sys

import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "docs-cache")

REQUIRED = ["rule_id", "mechanism_group", "mechanism", "statement", "gloss_zh", "sources",
            "instantiations", "applies_when", "checks", "parameter_relation",
            "violation_manifestation", "defect_class", "fault_types", "mitigation_family"]
ADV_REQUIRED = ["advisory_id", "mechanism_group", "mechanism", "statement", "gloss_zh",
                "sources", "why_not_checkable"]
FAULT_TYPES = {"dependency-delay", "dependency-unavailable", "packet-loss", "instance-kill",
               "cpu-pressure", "memory-pressure", "network-partition", "disk-pressure",
               "traffic-surge", "dependency-error"}
CHECKABILITY = {"static-code", "static-config", "static-manifest", "needs-requirement",
                "needs-runtime", "static-cross-object"}

errors, warns = [], []


def err(m):
    errors.append(m)


def warn(m):
    warns.append(m)


def norm(s):
    s = s.replace(" ", " ").replace("​", "")
    s = re.sub(r"[“”]", '"', s)
    s = re.sub(r"[‘’]", "'", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def parse_stack():
    s = open(os.path.join(ROOT, "stack.md"), encoding="utf-8").read()
    mechs, group, in_comp = {}, None, False
    comps = set()
    for line in s.split("\n"):
        if "<!-- BEGIN:components -->" in line:
            in_comp = True
            continue
        if "<!-- END:components -->" in line:
            in_comp = False
            continue
        if in_comp and line.startswith("| ") and not line.startswith("|---"):
            cells = [c.strip() for c in line.split("|")]
            if len(cells) > 4 and cells[1].isdigit():
                comps.add(cells[3])
            continue
        m = re.match(r"^### 组 \d+ (\S+)（", line)
        if m:
            group = m.group(1)
            continue
        if line.startswith("## ") and group:
            group = None
        if group and line.startswith("| ") and not line.startswith("|---"):
            name = line.split("|")[1].strip()
            name = re.sub(r"^\*\*\[新增\]\s*(.*?)\*\*$", r"\1", name).strip()
            if name and name != "机制":
                mechs[name] = group
    return mechs, comps


def load(name, key):
    p = os.path.join(ROOT, name)
    if not os.path.exists(p):
        err("缺文件：%s" % name)
        return []
    d = yaml.safe_load(open(p, encoding="utf-8"))
    return (d or {}).get(key, []) or []


def check_quote(where, doc_id, quote, texts):
    if doc_id not in texts:
        err("%s：doc_id %s 没有本地副本（抓取失败或不存在），不得引用" % (where, doc_id))
        return "missing-doc"
    body = texts[doc_id]
    if quote in body:
        return "exact"
    if norm(quote) and norm(quote) in norm(body):
        warn("%s：quote 只在归一化空白后匹配（doc %s）：%s…" % (where, doc_id, quote[:60]))
        return "normalized"
    err("%s：quote 在 docs-cache/%s.txt 里找不到：%s…" % (where, doc_id, quote[:90]))
    return "missing"


def main():
    mechs, comps = parse_stack()
    docs = {}
    p = os.path.join(ROOT, "documents.yaml")
    if os.path.exists(p):
        for d in (yaml.safe_load(open(p, encoding="utf-8")) or {}).get("documents", []):
            docs[d["doc_id"]] = d
    else:
        err("缺 documents.yaml")
    texts = {}
    for doc_id, d in docs.items():
        if d.get("fetch_status") == "ok" and d.get("local_copy"):
            fp = os.path.join(ROOT, d["local_copy"])
            if os.path.exists(fp):
                texts[doc_id] = open(fp, encoding="utf-8").read()
            else:
                err("documents.yaml 说 %s 有本地副本，但文件不在：%s" % (doc_id, d["local_copy"]))

    rules = load("rules.yaml", "rules")
    advs = load("advisories.yaml", "advisories")

    seen_ids, seen_stmt, qstat = set(), {}, {"exact": 0, "normalized": 0, "missing": 0, "missing-doc": 0}
    for r in rules:
        rid = r.get("rule_id", "<无 rule_id>")
        for f in REQUIRED:
            if f not in r or r[f] in (None, "", [], {}):
                err("%s：缺字段 %s" % (rid, f))
        if rid in seen_ids:
            err("%s：rule_id 重复" % rid)
        seen_ids.add(rid)
        st = norm(r.get("statement", ""))
        if st in seen_stmt:
            warn("%s：statement 与 %s 重复" % (rid, seen_stmt[st]))
        seen_stmt[st] = rid

        mech = r.get("mechanism")
        if mech not in mechs:
            err("%s：mechanism「%s」不在 stack.md 的机制清单里" % (rid, mech))
        elif r.get("mechanism_group") != mechs[mech]:
            err("%s：mechanism_group「%s」与 stack.md 里该机制所属组「%s」不一致" % (
                rid, r.get("mechanism_group"), mechs[mech]))
        if r.get("defect_class") not in (1, 2, 3, 4):
            err("%s：defect_class 非法：%r" % (rid, r.get("defect_class")))
        for ft in r.get("fault_types", []) or []:
            if ft not in FAULT_TYPES:
                err("%s：fault_types 出现未登记的取值「%s」" % (rid, ft))

        srcs = r.get("sources") or []
        if not srcs:
            err("%s：sources 为空" % rid)
        for s in srcs:
            for f in ("doc_id", "quote", "location"):
                if not s.get(f):
                    err("%s：sources 条目缺 %s" % (rid, f))
            if s.get("doc_id") and s.get("quote"):
                qstat[check_quote(rid, s["doc_id"], s["quote"], texts)] += 1

        for inst in r.get("instantiations") or []:
            c = inst.get("component")
            if c not in comps:
                err("%s：instantiations 的组件「%s」不在 stack.md 的组件表里" % (rid, c))
            if inst.get("config_or_code") not in ("config", "code", "both", "manifest"):
                err("%s：instantiations[%s].config_or_code 非法：%r" % (rid, c, inst.get("config_or_code")))
            if not inst.get("how"):
                err("%s：instantiations[%s] 缺 how" % (rid, c))

        checks = r.get("checks") or {}
        if not (checks.get("static") or checks.get("runtime")):
            err("%s：checks 里 static 和 runtime 都空——写不出可核对条件的应放 advisories.yaml" % rid)
        for c in checks.get("static") or []:
            if c.get("checkability") not in CHECKABILITY:
                err("%s：static check %s 的 checkability 非法：%r" % (rid, c.get("id"), c.get("checkability")))
            if not c.get("what"):
                err("%s：static check %s 缺 what" % (rid, c.get("id")))
        for c in checks.get("runtime") or []:
            if not c.get("what") or not c.get("signals"):
                err("%s：runtime check %s 缺 what 或 signals" % (rid, c.get("id")))

        pr = r.get("parameter_relation") or {}
        if not pr.get("activation"):
            err("%s：parameter_relation 缺 activation" % rid)
        for k, v in (pr.get("defaults") or {}).items():
            if not isinstance(v, dict) or "value" not in v or "doc_id" not in v:
                err("%s：defaults[%s] 必须写成 {value: ..., doc_id: ...}，默认值要注明出处" % (rid, k))
            elif v["doc_id"] not in docs:
                err("%s：defaults[%s] 的 doc_id %s 不存在" % (rid, k, v["doc_id"]))

    for a in advs:
        aid = a.get("advisory_id", "<无 advisory_id>")
        for f in ADV_REQUIRED:
            if f not in a or a[f] in (None, "", [], {}):
                err("%s：缺字段 %s" % (aid, f))
        if a.get("mechanism") and a["mechanism"] not in mechs:
            err("%s：mechanism「%s」不在 stack.md 里" % (aid, a["mechanism"]))
        for s in a.get("sources") or []:
            if s.get("doc_id") and s.get("quote"):
                qstat[check_quote(aid, s["doc_id"], s["quote"], texts)] += 1

    print("=" * 72)
    print("rules-v1/validate.py")
    print("=" * 72)
    print("stack.md：机制 %d 条，组件 %d 个" % (len(mechs), len(comps)))
    print("documents.yaml：%d 份，其中有本地副本可校验的 %d 份" % (len(docs), len(texts)))
    print("rules.yaml：%d 条规则；advisories.yaml：%d 条" % (len(rules), len(advs)))
    print("quote 校验：逐字命中 %d，仅空白归一化后命中 %d，找不到 %d，文档缺失 %d" % (
        qstat["exact"], qstat["normalized"], qstat["missing"], qstat["missing-doc"]))
    print("-" * 72)
    if warns:
        print("警告 %d 条：" % len(warns))
        for w in warns:
            print("  ! " + w)
    if errors:
        print("错误 %d 条：" % len(errors))
        for e in errors:
            print("  x " + e)
        print("-" * 72)
        print("结果：FAIL")
        return 1
    print("结果：PASS（0 错误，%d 警告）" % len(warns))
    return 0


if __name__ == "__main__":
    sys.exit(main())
