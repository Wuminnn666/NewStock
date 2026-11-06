# -*- coding: utf-8 -*-
"""把交付物的 Markdown 转成排版规范、可直接灌入 Word 的 HTML（S2 产物）。

只覆盖本项目文档用到的 Markdown 子集：标题、段落、有序/无序列表、
表格、引用块、代码块、分隔线、加粗与行内代码。
"""
import os
import re
import sys
import html as H

STYLE = """
@page { size: A4; margin: 2.2cm 2.0cm; }
body { font-family: "Microsoft YaHei","PingFang SC",serif; font-size: 10.5pt;
       line-height: 1.75; color: #1f2d3d; }
.cover { text-align: center; padding-top: 180px; page-break-after: always; }
.cover .kicker { font-size: 11pt; color: #5b7fd4; letter-spacing: 2px; }
.cover h1 { font-size: 26pt; margin: 18px 0 10px; color: #14243a; }
.cover .sub { font-size: 11.5pt; color: #5d6f82; margin-top: 6px; }
.cover .meta { margin-top: 60px; font-size: 10pt; color: #7b8a99; }
h1 { font-size: 19pt; color: #14243a; border-bottom: 2.5px solid #5b7fd4;
     padding-bottom: 6px; margin: 26px 0 12px; page-break-after: avoid; }
h2 { font-size: 15pt; color: #1d3f6e; margin: 22px 0 10px; page-break-after: avoid; }
h3 { font-size: 12.5pt; color: #23496f; margin: 16px 0 8px; page-break-after: avoid; }
p { margin: 6px 0; text-align: justify; }
ul, ol { margin: 6px 0 6px 22px; }
li { margin: 3px 0; }
table { border-collapse: collapse; width: 100%; margin: 10px 0 14px;
        font-size: 9.5pt; page-break-inside: avoid; }
th { background: #eaf0fd; color: #1d3f6e; font-weight: 600; text-align: left; }
th, td { border: 1px solid #c9d6e4; padding: 6px 8px; vertical-align: top; }
tr:nth-child(even) td { background: #fafcff; }
blockquote { margin: 10px 0; padding: 8px 12px; background: #f4f7fb;
             border-left: 4px solid #5b7fd4; color: #44576b; font-size: 10pt; }
pre { background: #f6f8fa; border: 1px solid #dde5ee; padding: 10px; font-size: 9pt;
      font-family: Consolas,monospace; white-space: pre-wrap; }
code { font-family: Consolas,monospace; background: #eef2f7; padding: 1px 3px; font-size: 9.5pt; }
strong { color: #14243a; }
hr { border: none; border-top: 1px solid #dde5ee; margin: 16px 0; }
.note { background: #fff8e6; border: 1px solid #e8d199; padding: 10px 12px; margin: 10px 0; }
"""


def inline(t: str) -> str:
    t = H.escape(t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    return t


def md_to_html(md: str) -> str:
    lines = md.split("\n")
    out, i = [], 0
    in_code = in_table = in_ul = in_ol = False

    def close_lists():
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>"); in_ul = False
        if in_ol:
            out.append("</ol>"); in_ol = False

    while i < len(lines):
        ln = lines[i]
        s = ln.strip()

        # 代码块
        if s.startswith("```"):
            if in_code:
                out.append("</pre>"); in_code = False
            else:
                close_lists(); out.append("<pre>"); in_code = True
            i += 1
            continue
        if in_code:
            out.append(H.escape(ln))
            i += 1
            continue

        # 表格
        if s.startswith("|") and s.endswith("|"):
            close_lists()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            # 第二行是分隔线
            body = [r for idx, r in enumerate(rows) if idx != 1]
            out.append("<table>")
            for ridx, r in enumerate(body):
                tag = "th" if ridx == 0 else "td"
                out.append("<tr>" + "".join(f"<{tag}>{inline(c)}</{tag}>" for c in r) + "</tr>")
            out.append("</table>")
            in_table = True
            continue

        if not s:
            close_lists(); i += 1; continue

        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        if m:
            close_lists()
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{inline(m.group(2))}</h{lvl}>")
            i += 1
            continue

        if s.startswith("---") and set(s) <= {"-", " "}:
            close_lists(); out.append("<hr/>"); i += 1; continue

        if s.startswith(">"):
            close_lists()
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append("<blockquote>" + "<br/>".join(inline(x) for x in buf if x) + "</blockquote>")
            continue

        m = re.match(r"^[-*]\s+(.*)$", s)
        if m:
            if not in_ul:
                close_lists(); out.append("<ul>"); in_ul = True
            out.append(f"<li>{inline(m.group(1))}</li>")
            i += 1
            continue

        m = re.match(r"^\d+\.\s+(.*)$", s)
        if m:
            if not in_ol:
                close_lists(); out.append("<ol>"); in_ol = True
            out.append(f"<li>{inline(m.group(1))}</li>")
            i += 1
            continue

        close_lists()
        out.append(f"<p>{inline(s)}</p>")
        i += 1

    if in_code:
        out.append("</pre>")
    close_lists()
    return "\n".join(out)


def build(src, dst, kicker, title, sub, meta):
    md = open(src, encoding="utf-8").read()
    # 首个 H1 用作正文标题，封面单独设计
    body_md = re.sub(r"^#\s+.*\n", "", md, count=1)
    body = md_to_html(body_md)
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{H.escape(title)}</title>
<style>{STYLE}</style></head><body>
<div class="cover">
  <div class="kicker">{H.escape(kicker)}</div>
  <h1>{H.escape(title)}</h1>
  <div class="sub">{H.escape(sub)}</div>
  <div class="meta">{H.escape(meta)}</div>
</div>
{body}
</body></html>"""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    open(dst, "w", encoding="utf-8").write(doc)
    print("HTML:", dst, len(doc), "bytes")


if __name__ == "__main__":
    BASE = r"E:\Wuminnn\NewStock\docs\deliverables"
    OUT = os.path.join(BASE, ".build")
    jobs = [
        ("03_典型事件访谈/典型事件访谈整理_唐映秋.md", "03_典型事件访谈整理.html",
         "交付物 03 · 典型事件访谈", "典型事件访谈整理：规格表 220V / 详情页 110V 的资料冲突",
         "万象商家中心 · 商品上架与资料审核｜受访人：唐映秋（内容审核员）",
         "AI 辅助整理 · 依据原始访谈记录逐句还原 · 2026-11-06"),
        ("04_行业分析.md", "04_行业分析.html",
         "交付物 04 · 行业分析", "电商商品上架与资料审核 · 行业分析",
         "监管框架 / 痛点结构 / 技术方案对比 / 结论建议",
         "万象商家中心 · 商品详情里的自相矛盾 · 2026-11-06"),
        ("06_需求定义文档.md", "06_需求定义文档.html",
         "交付物 06 · 需求定义", "需求定义文档 · 商品上架审核 AI 改造",
         "痛点 → AI 改造点 → 优先级 → 需求规格与追溯矩阵 → 验收标准清单",
         "版本 v1.0 · 2026-11-06"),
        ("07_详细项目设计方案.md", "07_详细项目设计方案.html",
         "交付物 07 · 详细设计", "详细项目设计方案 · 商品上架与资料审核系统",
         "四层架构 / 数据模型 / Agent 编排 / 安全护栏 / 部署运维",
         "版本 v1.0 · 2026-11-06"),
        ("08_报价与投入产出测算.md", "08_报价与投入产出测算.html",
         "交付物 08 · 商务测算", "报价与投入产出测算 · 商品上架审核 AI 改造",
         "低 / 中 / 高三档报价 · 试点口径 ROI · 敏感性分析与付款节奏",
         "版本 v1.0 · 2026-11-06"),
    ]
    for src, name, k, t, s, m in jobs:
        build(os.path.join(BASE, src), os.path.join(OUT, name), k, t, s, m)
