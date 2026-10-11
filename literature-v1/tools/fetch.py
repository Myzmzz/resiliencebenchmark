#!/usr/bin/env python3
# 用途：抓取 sources.tsv 里的页面/论文/issue，存 raw 与归一化文本到 literature-v1/docs-cache/，
#       抓取结果写进 tools/fetch-log.json。支持三种来源：
#         html   普通网页          -> BeautifulSoup 取正文后归一化
#         pdf    论文 PDF          -> pdfminer 提取文本，合并断词连字符
#         ghissue GitHub issue/PR  -> gh api 拉 JSON，标题+正文+全部评论拼成文本
# 归一化只做空白折叠，不改写任何词句，保证 quote 能逐字回溯。
import html as htmlmod
import json
import os
import re
import subprocess
import sys
import time
import ssl
import urllib.request
import urllib.error
from datetime import datetime, timezone

try:
    import certifi
    SSLCTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    SSLCTX = ssl.create_default_context()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "docs-cache")
RAW = os.path.join(CACHE, "raw")
LOG = os.path.join(ROOT, "tools", "fetch-log.json")
TSV = os.path.join(ROOT, "tools", "sources.tsv")
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


def html_to_text(raw_html, url=""):
    u = url.lower().split("?")[0]
    if u.endswith((".md", ".adoc", ".txt", ".rst")) or "raw.githubusercontent.com" in u:
        out = []
        for line in raw_html.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            out.append(re.sub(r"[ \t\f\v ​]+", " ", line).rstrip())
        return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"
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
        frag = re.sub(r"(?is)<(script|style|svg|noscript).*?</\1>", "", raw_html)

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
    frag = htmlmod.unescape(frag).replace("\x00", "\n")
    out = []
    for line in frag.split("\n"):
        line = re.sub(r"[ \t\r\f\v ​]+", " ", line).strip()
        line = re.sub(r"(\s*\|\s*)+$", "", line).strip()
        if line:
            out.append(line)
    return "\n".join(out) + "\n"


def pdf_to_text(path):
    from pdfminer.high_level import extract_text
    txt = extract_text(path)
    # PDF 排版的断词连字符会把一个词劈成两半，先接回来，再把段内硬换行并成空格，
    # 这样引用一句话不会被版面换行切碎；段落之间保留空行。
    txt = txt.replace("\r\n", "\n").replace("\r", "\n")
    txt = re.sub(r"(\w)-\n(\w)", r"\1\2", txt)
    txt = re.sub(r"[ \t ​]+", " ", txt)
    paras = re.split(r"\n\s*\n", txt)
    out = []
    for p in paras:
        p = " ".join(x.strip() for x in p.split("\n") if x.strip())
        if p.strip():
            out.append(p.strip())
    return "\n\n".join(out) + "\n"


def gh_issue_to_text(url):
    m = re.search(r"github\.com/([^/]+)/([^/]+)/(issues|pull)/(\d+)", url)
    if not m:
        raise ValueError("not a github issue url: %s" % url)
    owner, repo, kind, num = m.group(1), m.group(2), m.group(3), m.group(4)

    def api(path):
        r = subprocess.run(["gh", "api", path, "--paginate"], capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            raise RuntimeError("gh api %s failed: %s" % (path, r.stderr.strip()[:200]))
        body = r.stdout.strip()
        # --paginate 会把多页数组直接拼接，这里补成合法 JSON 数组
        if body.startswith("[") and "][" in body:
            body = body.replace("][", ",")
        return json.loads(body) if body else []

    iss = api("repos/%s/%s/issues/%s" % (owner, repo, num))
    comments = []
    try:
        comments = api("repos/%s/%s/issues/%s/comments" % (owner, repo, num))
    except Exception:
        pass
    lines = []
    lines.append("[[H1]] %s" % (iss.get("title") or ""))
    lines.append("state: %s   created: %s   author: %s" % (
        iss.get("state"), (iss.get("created_at") or "")[:10], (iss.get("user") or {}).get("login")))
    labels = ",".join(l.get("name", "") for l in (iss.get("labels") or []))
    if labels:
        lines.append("labels: %s" % labels)
    lines.append("")
    lines.append("[[H2]] ISSUE BODY")
    lines.append((iss.get("body") or "").replace("\r\n", "\n").strip())
    for c in comments:
        lines.append("")
        lines.append("[[H2]] COMMENT by %s at %s" % (
            (c.get("user") or {}).get("login"), (c.get("created_at") or "")[:10]))
        lines.append((c.get("body") or "").replace("\r\n", "\n").strip())
    txt = "\n".join(lines)
    txt = re.sub(r"[ \t ​]+", " ", txt)
    return re.sub(r"\n{3,}", "\n\n", txt) + "\n"


def http_get(url, retries=2, binary=False):
    last = ""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=40, context=SSLCTX) as r:
                data = r.read()
                return dict(status="ok", http=r.getcode(), data=data,
                            lastmod=r.headers.get("Last-Modified", ""), final_url=r.geturl(),
                            ctype=r.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, str(e)[:140])
        time.sleep(2 + attempt * 3)
    # 少数学术站点证书链不全，urllib 拿不到；系统 curl 的根证书库更全，用它兜底。
    try:
        r = subprocess.run(["curl", "-sSL", "--max-time", "60", "-A", UA, url],
                           capture_output=True, timeout=90)
        if r.returncode == 0 and len(r.stdout) > 400:
            return dict(status="ok", http=200, data=r.stdout, lastmod="",
                        final_url=url, ctype="", via="curl")
    except Exception as e:
        last += " | curl: %s" % str(e)[:80]
    return dict(status="failed", http=0, data=b"", error=last, lastmod="", final_url=url, ctype="")


def write_cache(doc_id, url, text, retrieved):
    hdr = "# doc_id: %s\n# url: %s\n# retrieved: %s\n# ---- normalized text below ----\n" % (
        doc_id, url, retrieved)
    open(os.path.join(CACHE, doc_id + ".txt"), "w", encoding="utf-8").write(hdr + text)


def work(item):
    doc_id, group, component, stype, title, url = item
    # 第 4 列写成 "抓取方式:来源类型"，例如 html:doc / pdf:study / ghissue:issue / html:postmortem
    kind = stype.split(":")[0]
    src_type = stype.split(":")[-1]
    retrieved = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entry = dict(doc_id=doc_id, mech_group=group, component=component, source_type=src_type,
                 title=title, url=url, retrieved=retrieved, status="failed", http=0,
                 last_modified="", final_url=url)
    try:
        if kind == "ghissue":
            text = gh_issue_to_text(url)
            entry["http"] = 200
        elif kind == "pdf" or url.lower().split("?")[0].endswith(".pdf"):
            res = http_get(url)
            if res["status"] != "ok":
                entry["error"] = res.get("error", "")
                return entry
            pdfp = os.path.join(RAW, doc_id + ".pdf")
            open(pdfp, "wb").write(res["data"])
            text = pdf_to_text(pdfp)
            entry["http"] = res["http"]
            entry["last_modified"] = res["lastmod"]
            entry["final_url"] = res["final_url"]
        else:
            res = http_get(url)
            if res["status"] != "ok":
                entry["error"] = res.get("error", "")
                return entry
            enc = "utf-8"
            m = re.search(rb'charset=["\']?([\w\-]+)', res["data"][:4000], re.I)
            if m:
                enc = m.group(1).decode("ascii", "replace")
            raw = res["data"].decode(enc, errors="replace")
            open(os.path.join(RAW, doc_id + ".html"), "w", encoding="utf-8").write(raw)
            text = html_to_text(raw, url)
            entry["http"] = res["http"]
            entry["last_modified"] = res["lastmod"]
            entry["final_url"] = res["final_url"]
        if len(text) < 400:
            entry["error"] = "extracted text too short (%d chars)" % len(text)
            return entry
        write_cache(doc_id, url, text, retrieved)
        entry["status"] = "ok"
        entry["chars"] = len(text)
        entry["cache"] = "docs-cache/%s.txt" % doc_id
    except Exception as e:
        entry["error"] = "%s: %s" % (type(e).__name__, str(e)[:200])
    return entry


def main():
    os.makedirs(RAW, exist_ok=True)
    only = set(a for a in sys.argv[1:] if not a.startswith("-"))
    force = "--force" in sys.argv
    log = json.load(open(LOG, encoding="utf-8")) if os.path.exists(LOG) else {}
    rows = []
    for line in open(TSV, encoding="utf-8"):
        line = line.rstrip("\n")
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 6:
            print("BADROW", line[:80])
            continue
        rows.append(parts[:6])
    todo = []
    for r in rows:
        doc_id = r[0]
        if only and doc_id not in only:
            continue
        done = (log.get(doc_id, {}).get("status") == "ok"
                and os.path.exists(os.path.join(CACHE, doc_id + ".txt")))
        if done and not force and not only:
            continue
        todo.append(r)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as ex:
        for entry in ex.map(work, todo):
            log[entry["doc_id"]] = entry
            print("%-7s %-30s %s" % (entry["status"], entry["doc_id"],
                  entry.get("error", "%d chars" % entry.get("chars", 0))))
    json.dump(log, open(LOG, "w", encoding="utf-8"), indent=1, ensure_ascii=False, sort_keys=True)
    ok = sum(1 for v in log.values() if v["status"] == "ok")
    print("---- total %d, ok %d, failed %d" % (len(log), ok, len(log) - ok))


if __name__ == "__main__":
    main()
