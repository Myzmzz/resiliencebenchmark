#!/usr/bin/env python3
# 用途：抓取 sources.tsv 里的官方文档/准则页面，存 raw HTML 与归一化文本到 rules-v1/docs-cache/，
#       并把抓取结果（状态、日期、页面自报版本）写进 tools/fetch-log.json。由第 2 步使用。
import html as htmlmod
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "docs-cache")
RAW = os.path.join(CACHE, "raw")
LOG = os.path.join(ROOT, "tools", "fetch-log.json")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

BLOCK = ("p|div|li|ul|ol|h1|h2|h3|h4|h5|h6|pre|blockquote|section|article|header|footer|"
         "main|aside|nav|table|thead|tbody|dl|dt|dd|figure|figcaption|details|summary|hr|form")
DROP_TAGS = ["script", "style", "svg", "noscript", "iframe", "canvas", "template"]
MAIN_SELECTORS = [
    ("tag", "main"), ("attr", ("role", "main")), ("tag", "article"),
    ("class", "md-content"), ("class", "content"), ("id", "main-content"),
    ("class", "td-content"), ("id", "content"),
]


def to_text(raw_html, url=""):
    # 纯文本/Markdown/AsciiDoc 原始文件（raw.githubusercontent.com 一类）不走 HTML 处理，保留原有换行。
    # 判据用 URL 后缀，而不是"有没有 <html> 标签"——有些官方页面是不带 <html> 的 HTML 片段。
    u = url.lower().split("?")[0]
    if u.endswith((".md", ".adoc", ".txt", ".rst")) or "raw.githubusercontent.com" in u:
        out = []
        for line in raw_html.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = re.sub(r"[ \t\f\v\u00a0\u200b]+", " ", line).rstrip()
            out.append(line)
        txt = "\n".join(out)
        txt = re.sub(r"\n{3,}", "\n\n", txt)
        return txt.strip() + "\n"
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(raw_html, "html.parser")
        for t in DROP_TAGS:
            for el in soup.find_all(t):
                el.decompose()
        root = None
        for kind, sel in MAIN_SELECTORS:
            if kind == "tag":
                root = soup.find(sel)
            elif kind == "attr":
                root = soup.find(attrs={sel[0]: sel[1]})
            elif kind == "class":
                root = soup.find(attrs={"class": sel})
            elif kind == "id":
                root = soup.find(attrs={"id": sel})
            if root is not None and len(root.get_text(strip=True)) > 400:
                break
            root = None
        if root is None:
            root = soup.body or soup
        frag = str(root)
    except Exception:
        frag = raw_html
        frag = re.sub(r"(?is)<(script|style|svg|noscript).*?</\1>", "", frag)
    # HTML 源码里的换行是无意义空白：先把 <pre>/<code> 里的换行保护起来，再把其余换行并成空格，
    # 这样一个段落就落在一行里，引用条款时不会被硬换行切碎。
    def _protect(m):
        return m.group(0).replace("\n", "\x00")
    frag = re.sub(r"(?is)<pre[^>]*>.*?</pre>", _protect, frag)
    frag = frag.replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ")
    frag = re.sub(r"(?is)<h([1-6])[^>]*>(.*?)</h\1>",
                  lambda m: "\n[[H%s]] %s\n" % (m.group(1), re.sub(r"(?is)<[^>]+>", "", m.group(2))), frag)
    frag = re.sub(r"(?is)</t[dh]\s*>", " | ", frag)
    frag = re.sub(r"(?is)<br\s*/?>", "\n", frag)
    frag = re.sub(r"(?is)</?(%s)(\s[^>]*)?>" % BLOCK, "\n", frag)
    frag = re.sub(r"(?is)</?tr(\s[^>]*)?>", "\n", frag)
    frag = re.sub(r"(?is)<[^>]+>", "", frag)
    frag = htmlmod.unescape(frag)
    frag = frag.replace("\x00", "\n")
    out = []
    for line in frag.split("\n"):
        line = re.sub(r"[ \t\r\f\v ​]+", " ", line).strip()
        line = re.sub(r"(\s*\|\s*)+$", "", line).strip()
        if line:
            out.append(line)
    return "\n".join(out) + "\n"


def sniff_version(raw_html, url, text):
    cands = []
    m = re.search(r"/(v?\d+\.\d+(?:\.\d+)?)/", url)
    if m:
        cands.append("url:" + m.group(1))
    if "/latest/" in url:
        cands.append("url:latest")
    for pat in [r'"version"\s*:\s*"([^"]{1,24})"',
                r'name="docsearch:version"\s+content="([^"]{1,24})"',
                r'<meta[^>]+content="([^"]{1,24})"[^>]+name="docsearch:version"']:
        m = re.search(pat, raw_html)
        if m:
            cands.append("meta:" + m.group(1))
            break
    m = re.search(r"(?im)^(?:Version|版本)[: ]\s*([0-9][\w.\-]{0,20})\s*$", text)
    if m:
        cands.append("page:" + m.group(1))
    return ";".join(dict.fromkeys(cands))[:120]


def fetch_one(doc_id, url, retries=1):
    last = ""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
            })
            with urllib.request.urlopen(req, timeout=25) as r:
                data = r.read()
                code = r.getcode()
                lastmod = r.headers.get("Last-Modified", "")
                final = r.geturl()
            enc = "utf-8"
            m = re.search(rb'charset=["\']?([\w\-]+)', data[:4000], re.I)
            if m:
                enc = m.group(1).decode("ascii", "replace")
            raw = data.decode(enc, errors="replace")
            return dict(status="ok", http=code, raw=raw, lastmod=lastmod, final_url=final)
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, str(e)[:120])
        time.sleep(2 + attempt * 3)
    return dict(status="failed", http=0, raw="", error=last, lastmod="", final_url=url)


def reparse():
    log = json.load(open(LOG, encoding="utf-8"))
    n = 0
    for doc_id in sorted(log):
        v = log[doc_id]
        rp = os.path.join(RAW, doc_id + ".html")
        if not os.path.exists(rp):
            if v.get("status") == "ok":
                v["status"] = "failed"
                v["error"] = "no local raw copy"
            continue
        raw = open(rp, encoding="utf-8", errors="replace").read()
        text = to_text(raw, v["url"])
        if len(text) < 500:
            v["status"] = "failed"
            v["error"] = "extracted text too short (%d chars), likely JS-rendered" % len(text)
            v.pop("cache", None)
            continue
        hdr = "# doc_id: %s\n# url: %s\n# retrieved: %s\n# ---- normalized text below ----\n" % (
            doc_id, v["url"], v["retrieved"])
        open(os.path.join(CACHE, doc_id + ".txt"), "w", encoding="utf-8").write(hdr + text)
        v["status"] = "ok"
        v.pop("error", None)
        v["chars"] = len(text)
        v["lines"] = text.count("\n")
        v["version_hint"] = sniff_version(raw, v["url"], text)
        v["cache"] = "docs-cache/%s.txt" % doc_id
        n += 1
    json.dump(log, open(LOG, "w", encoding="utf-8"), indent=1, ensure_ascii=False, sort_keys=True)
    print("reparsed %d docs from local raw copies; ok %d / %d" % (
        n, sum(1 for x in log.values() if x["status"] == "ok"), len(log)))


def main():
    if sys.argv[1:2] == ["--reparse"]:
        reparse()
        return
    only = set(sys.argv[1:])
    os.makedirs(RAW, exist_ok=True)
    log = {}
    if os.path.exists(LOG):
        log = json.load(open(LOG, encoding="utf-8"))
    rows = []
    with open(os.path.join(ROOT, "tools", "sources.tsv"), encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 6:
                print("BADROW", line[:80])
                continue
            rows.append(parts[:6])
    todo = []
    for doc_id, layer, component, stype, title, url in rows:
        if only and doc_id not in only:
            continue
        txt_path = os.path.join(CACHE, doc_id + ".txt")
        if doc_id in log and log[doc_id].get("status") == "ok" and os.path.exists(txt_path) and not only:
            continue
        todo.append((doc_id, layer, component, stype, title, url))

    def work(item):
        doc_id, layer, component, stype, title, url = item
        res = fetch_one(doc_id, url)
        txt_path = os.path.join(CACHE, doc_id + ".txt")
        entry = dict(doc_id=doc_id, layer=int(layer), component=component, source_type=stype,
                     title=title, url=url, retrieved=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                     status=res["status"], http=res["http"], last_modified=res.get("lastmod", ""),
                     final_url=res.get("final_url", url))
        if res["status"] == "ok":
            text = to_text(res["raw"], url)
            if len(text) < 500:
                entry["status"] = "failed"
                entry["error"] = "extracted text too short (%d chars), likely JS-rendered" % len(text)
            else:
                open(os.path.join(RAW, doc_id + ".html"), "w", encoding="utf-8").write(res["raw"])
                hdr = "# doc_id: %s\n# url: %s\n# retrieved: %s\n# ---- normalized text below ----\n" % (
                    doc_id, url, entry["retrieved"])
                open(txt_path, "w", encoding="utf-8").write(hdr + text)
                entry["chars"] = len(text)
                entry["lines"] = text.count("\n")
                entry["version_hint"] = sniff_version(res["raw"], url, text)
                entry["cache"] = "docs-cache/%s.txt" % doc_id
        else:
            entry["error"] = res.get("error", "")
        return entry

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as ex:
        for entry in ex.map(work, todo):
            log[entry["doc_id"]] = entry
            print("%-7s %-28s %s" % (entry["status"], entry["doc_id"],
                  entry.get("error", "%d chars" % entry.get("chars", 0))))

    json.dump(log, open(LOG, "w", encoding="utf-8"), indent=1, ensure_ascii=False, sort_keys=True)
    ok = sum(1 for v in log.values() if v["status"] == "ok")
    print("---- total %d, ok %d, failed %d" % (len(log), ok, len(log) - ok))


if __name__ == "__main__":
    main()
