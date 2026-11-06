# -*- coding: utf-8 -*-
"""为 5 份场景测试生成 GitHub 可直接预览的 HTML（含内嵌图 base64）。

复用 build_test_docs.py 的 SCENES 数据源，保证 docx 与 HTML 内容一致。
用法：python scripts/build_test_previews.py
输出：tests/02_场景测试过程/preview/*.html
"""
import base64
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_test_docs import SCENES, FIG, OUT, INTRO  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREVIEW = os.path.join(OUT, "preview")
os.makedirs(PREVIEW, exist_ok=True)

STYLE = """
body { font-family: "Microsoft YaHei","PingFang SC",sans-serif; max-width: 900px;
       margin: 0 auto; padding: 24px 20px 60px; color: #1f2d3d; line-height: 1.7; }
h1 { color: #14243a; border-bottom: 2.5px solid #5b7fd4; padding-bottom: 8px; font-size: 22px; }
h2 { color: #1d3f6e; font-size: 16px; margin: 22px 0 8px; }
.note { background: #f4f7fb; border-left: 4px solid #5b7fd4; padding: 8px 12px; color: #44576b; }
table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 13px; }
th { background: #eaf0fd; color: #1d3f6e; }
th, td { border: 1px solid #c9d6e4; padding: 6px 9px; text-align: left; vertical-align: top; }
tr:nth-child(even) td { background: #fafcff; }
img { max-width: 100%; border: 1px solid #dde5ee; margin: 8px 0; }
pre { background: #f6f8fa; border: 1px solid #dde5ee; padding: 10px; font-size: 12.5px;
      white-space: pre-wrap; font-family: Consolas, monospace; }
.pass { color: #3f9d6e; font-weight: 600; }
"""


def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def scene_png_b64(scene):
    """重画该场景图并转 base64（与 docx 内嵌图同源）。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
    plt.rcParams["axes.unicode_minus"] = False
    buf = io.BytesIO()
    tmp = os.path.join(PREVIEW, "_tmp.png")
    scene["fig"](tmp)
    buf.write(open(tmp, "rb").read())
    os.remove(tmp)
    return base64.b64encode(buf.getvalue()).decode()


def build_html(scene):
    img64 = scene_png_b64(scene)
    rows = "\n".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td class='pass'>{}</td></tr>".format(
            *(esc(str(c)) for c in r)) for r in scene["rows"])
    intro = "\n".join(f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in INTRO)
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{esc(scene['name'])}</title><style>{STYLE}</style></head><body>
<h1>{esc(scene['name'])}（可视化测试过程）</h1>
<div class="note">Test-2 交付项：每个场景一份可视化的测试过程文档，有图有过程。
本页与同名 .docx 内容一致；docx 请下载后用 Word/WPS 打开。</div>
<h2>一、文档说明</h2><table>{intro}</table>
<h2>二、场景目标</h2><p>{esc(scene['goal'])}</p>
<h2>三、用例执行记录（{len(scene['rows'])} 条）</h2>
<table><tr><th>编号</th><th>用例</th><th>输入/操作</th><th>期望结果</th><th>实测</th></tr>{rows}</table>
<h2>四、结果可视化</h2>
<img src="data:image/png;base64,{img64}" alt="{esc(scene['name'])}结果图"/>
<h2>五、复现方式</h2><pre>{esc(scene['cmd'])}</pre>
<h2>六、测试结论</h2>
<p>本场景共 {scene['total']} 条用例，通过 {scene['passed']} 条，通过率 {scene['passed']/scene['total']:.0%}。{esc(scene['concl'])}</p>
</body></html>"""
    out = os.path.join(PREVIEW, f"{scene['name']}.html")
    open(out, "w", encoding="utf-8").write(html)
    print("OK", out, len(html), "bytes")


if __name__ == "__main__":
    for s in SCENES:
        build_html(s)
    print("done:", PREVIEW)
