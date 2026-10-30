# -*- coding: utf-8 -*-
"""把 docs/go_live_checklist.md 渲染成可打印的评审版 HTML。

为什么要有 HTML 版：上线评审是「一群人对着同一份东西逐条打勾」的会，
Markdown 在投影和打印时表格会错行；HTML 版能固定列宽、加状态色块、留签字行，
会后还能直接当附件归档。为避免「md 改了 html 忘了改」，这里一律从 md 生成，
不手改 HTML。

用法：python scripts/build_checklist_html.py
"""
import html
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "docs", "go_live_checklist.md")
OUT = os.path.join(ROOT, "docs", "go_live_checklist.html")

CHIP = {
    "✅": '<span class="chip ok">已达标</span>',
    "🟡": '<span class="chip warn">部分达标</span>',
    "❌": '<span class="chip bad">未达标</span>',
}


def inline(text: str) -> str:
    """先转义再处理 `code` 与 **粗体**，避免把用户内容当 HTML。"""
    t = html.escape(text)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    for k, v in CHIP.items():
        t = t.replace(k, v)
    return t


def cell(text: str, tag="td") -> str:
    t = inline(text.strip())
    cls = ""
    if re.fullmatch(r"\s*[✅🟡❌]+\s*[^|]{0,14}", text.strip()):
        cls = ' class="c-status"'
    return f"<{tag}{cls}>{t}</{tag}>"


def render(md: str) -> str:
    out, i = [], 0
    lines = md.splitlines()
    in_table = False
    while i < len(lines):
        ln = lines[i]
        s = ln.strip()

        # 表格：连续的 | 开头行
        if s.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            body = [r for r in rows if not re.fullmatch(r"\|[\s:\-|]+\|", r)]
            head = [c for c in body[0].strip("|").split("|")]
            out.append("<div class='tw'><table><thead><tr>"
                       + "".join(cell(c, "th") for c in head) + "</tr></thead><tbody>")
            for r in body[1:]:
                cols = [c for c in r.strip("|").split("|")]
                out.append("<tr>" + "".join(cell(c) for c in cols) + "</tr>")
            out.append("</tbody></table></div>")
            in_table = True
            continue

        if s.startswith("# "):
            out.append(f"<h1>{inline(s[2:])}</h1>")
        elif s.startswith("## "):
            out.append(f"<h2>{inline(s[3:])}</h2>")
        elif s.startswith("### "):
            out.append(f"<h3>{inline(s[4:])}</h3>")
        elif s.startswith("> "):
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append("<div class='meta'>" + "<br>".join(inline(b) for b in buf) + "</div>")
            continue
        elif s == "---":
            out.append("<hr>")
        elif re.match(r"^\d+\.\s", s):
            items = []
            while i < len(lines) and re.match(r"^\d+\.\s", lines[i].strip()):
                items.append(re.sub(r"^\d+\.\s", "", lines[i].strip()))
                i += 1
            out.append("<ol>" + "".join(f"<li>{inline(x)}</li>" for x in items) + "</ol>")
            continue
        elif s.startswith("|"):
            pass
        elif s:
            out.append(f"<p>{inline(s)}</p>")
        i += 1
    return "\n".join(out)


CSS = """
:root{--ink:#1d2129;--sub:#5b6472;--line:#e3e6eb;--bg:#f6f7f9;--card:#fff;
--ok:#12805c;--okbg:#e6f6ef;--warn:#b26a00;--warnbg:#fdf1dc;--bad:#c02626;--badbg:#fdeaea;
--accent:#1f5fbf;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font-family:-apple-system,"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;
font-size:14px;line-height:1.75;}
.page{max-width:1180px;margin:0 auto;padding:36px 40px 64px;background:var(--card);
box-shadow:0 1px 3px rgba(0,0,0,.06);}
h1{font-size:25px;margin:0 0 6px;letter-spacing:.3px}
h2{font-size:18px;margin:38px 0 12px;padding-left:11px;border-left:4px solid var(--accent)}
h3{font-size:15px;margin:22px 0 8px;color:var(--sub)}
p{margin:8px 0}
.meta{color:var(--sub);font-size:12.5px;background:#fafbfc;border:1px solid var(--line);
border-radius:6px;padding:10px 14px;margin:10px 0 0}
hr{border:0;border-top:1px solid var(--line);margin:26px 0}
.tw{overflow-x:auto;margin:10px 0 4px;border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:13px;background:#fff}
th{background:#f2f4f7;text-align:left;font-weight:600;color:#33404f;
padding:9px 11px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:9px 11px;border-bottom:1px solid #eef0f3;vertical-align:top}
tr:last-child td{border-bottom:0}
tbody tr:nth-child(even){background:#fcfcfd}
td:nth-child(1),th:nth-child(1){white-space:nowrap;color:var(--accent);font-weight:600}
td.c-status{white-space:nowrap}
code{background:#f2f4f7;border-radius:4px;padding:1px 5px;font-size:12.2px;
font-family:"JetBrains Mono",Consolas,monospace;color:#0f4c81}
.chip{display:inline-block;padding:1px 8px;border-radius:20px;font-size:12px;
font-weight:600;white-space:nowrap}
.chip.ok{color:var(--ok);background:var(--okbg)}
.chip.warn{color:var(--warn);background:var(--warnbg)}
.chip.bad{color:var(--bad);background:var(--badbg)}
ol{margin:6px 0 6px 22px;padding:0}
li{margin:3px 0}
@media print{
 body{background:#fff;font-size:11.5px}
 .page{box-shadow:none;max-width:none;padding:0 6mm}
 h2{margin-top:20px}
 .tw{border:1px solid #ccc}
 tr{page-break-inside:avoid}
}
"""

TPL = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>上线前检查清单 · 商品上架与资料审核系统</title>
<style>{css}</style></head>
<body><div class="page">{body}
<div class="meta" style="margin-top:34px">
本页由 docs/go_live_checklist.md 自动生成（python scripts/build_checklist_html.py），
请勿直接编辑 HTML；结论以 Markdown 源文件为准。</div>
</div></body></html>"""


def main():
    md = open(SRC, encoding="utf-8").read()
    open(OUT, "w", encoding="utf-8").write(TPL.format(css=CSS, body=render(md)))
    print("已生成:", OUT, os.path.getsize(OUT), "bytes")


if __name__ == "__main__":
    main()
