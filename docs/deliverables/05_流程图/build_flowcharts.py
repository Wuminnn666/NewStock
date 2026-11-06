# -*- coding: utf-8 -*-
"""生成两份流程图（交付物⑤）：改造前现状流程、改造后目标流程。

改造后流程与需求发现报告 REQ-1~REQ-6 一一对应，节点上直接标注需求编号，
便于评审逐点对照。输出 SVG（浏览器可直接打开、可打印为 PDF）。
"""
import os

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)))
FONT = "'Microsoft YaHei','PingFang SC',sans-serif"

# 配色（浅色主题）
C_BOX = "#ffffff"
C_BORDER = "#c9d6e4"
C_TEXT = "#1f2d3d"
C_START = "#eef4fb"
C_DEC = "#fdf3e3"
C_DEC_B = "#e8b96a"
C_AI = "#e8f4ea"
C_AI_B = "#5aa469"
C_HUMAN = "#eaf0fd"
C_HUMAN_B = "#5b7fd4"
C_WARN = "#fdecec"
C_WARN_B = "#d9736f"
C_PAIN = "#fff8e6"
C_PAIN_B = "#d9a441"
C_ARROW = "#8fa3b8"


def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def box(x, y, w, h, text, fill=C_BOX, stroke=C_BORDER, size=15, bold=False, sub=None):
    """圆角矩形；sub 为第二行小字。"""
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{fill}" '
           f'stroke="{stroke}" stroke-width="1.6"/>']
    lines = text.split("\n")
    total = len(lines) + (1 if sub else 0)
    lh = size + 8
    start = y + h / 2 - (total - 1) * lh / 2 + size * 0.35
    for i, ln in enumerate(lines):
        out.append(f'<text x="{x + w / 2}" y="{start + i * lh}" text-anchor="middle" '
                   f'font-family="{FONT}" font-size="{size}" fill="{C_TEXT}" '
                   f'{"font-weight=\"600\"" if bold else ""}>{esc(ln)}</text>')
    if sub:
        out.append(f'<text x="{x + w / 2}" y="{start + len(lines) * lh}" text-anchor="middle" '
                   f'font-family="{FONT}" font-size="12" fill="#6b7c8f">{esc(sub)}</text>')
    return "".join(out)


def diamond(cx, cy, w, h, text, fill=C_DEC, stroke=C_DEC_B, size=14):
    pts = f"{cx},{cy - h / 2} {cx + w / 2},{cy} {cx},{cy + h / 2} {cx - w / 2},{cy}"
    out = [f'<polygon points="{pts}" fill="{fill}" stroke="{stroke}" stroke-width="1.6"/>']
    lines = text.split("\n")
    start = cy - (len(lines) - 1) * (size + 4) / 2 + size * 0.35
    for i, ln in enumerate(lines):
        out.append(f'<text x="{cx}" y="{start + i * (size + 4)}" text-anchor="middle" '
                   f'font-family="{FONT}" font-size="{size}" fill="{C_TEXT}">{esc(ln)}</text>')
    return "".join(out)


def arrow(x1, y1, x2, y2, label="", dash=False, color=C_ARROW):
    """直线箭头，带路由拐点由调用方自行给点。"""
    d = f"M{x1},{y1} L{x2},{y2}"
    out = [f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.7" '
           f'marker-end="url(#ah)" {"stroke-dasharray=\"6 5\"" if dash else ""}/>']
    if label:
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        out.append(f'<text x="{mx + 6}" y="{my - 6}" font-family="{FONT}" font-size="12.5" '
                   f'fill="#5d6f82">{esc(label)}</text>')
    return "".join(out)


def legend(x, y):
    items = [(C_START, C_BORDER, "开始/结束"), (C_BOX, C_BORDER, "处理步骤"),
             (C_DEC, C_DEC_B, "判断"), (C_AI, C_AI_B, "AI 环节（只读提示）"),
             (C_HUMAN, C_HUMAN_B, "人工签核"), (C_WARN, C_WARN_B, "异常/转人工")]
    out = []
    for i, (f, s, t) in enumerate(items):
        yy = y + i * 22
        out.append(f'<rect x="{x}" y="{yy}" width="14" height="14" rx="3" fill="{f}" stroke="{s}"/>')
        out.append(f'<text x="{x + 20}" y="{yy + 11}" font-family="{FONT}" font-size="12" '
                   f'fill="#5d6f82">{t}</text>')
    return "".join(out)


HEAD = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="{{H}}" viewBox="0 0 1280 {{H}}">
<defs><marker id="ah" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto">
<path d="M0,0 L8,3 L0,6 z" fill="{C_ARROW}"/></marker></defs>
<rect width="1280" height="{{H}}" fill="#f7f9fc"/>
<text x="60" y="52" font-family="{FONT}" font-size="24" font-weight="700" fill="{C_TEXT}">{{TITLE}}</text>
<text x="60" y="80" font-family="{FONT}" font-size="13.5" fill="#6b7c8f">{{SUBTITLE}}</text>
'''


# ---------------------------------------------------------------- 改造前
def before():
    W = 1280
    s = [HEAD.replace("{H}", "1140").replace("{TITLE}", "改造前：商品上架与资料审核现状流程")
             .replace("{SUBTITLE}", "痛点集中在人工跨资料核验（占审核时间约 60%）与反复退回；冲突时以哪份资料为准无明文，责任链靠人盯")]

    # 主链
    s.append(box(470, 110, 340, 60, "商家提交上架申请", C_START, bold=True))
    s.append(arrow(640, 170, 640, 205))
    s.append(box(470, 205, 340, 64, "系统基础校验（必填/格式）", sub="不做跨资料语义比对"))
    s.append(arrow(640, 269, 640, 304))
    s.append(box(430, 304, 420, 76, "人工审核：逐项比对四类资料", C_HUMAN, C_HUMAN_B,
                 sub="规格表 / 图片 / 说明书 / 证书覆盖型号（约 60% 时间）"))
    s.append(arrow(640, 380, 640, 425))
    s.append(diamond(640, 480, 300, 110, "资料是否一致？"))
    # 一致 → 通过
    s.append(arrow(790, 480, 930, 480, "一致"))
    s.append(box(930, 448, 250, 64, "审核通过 → 商品上架", C_START))
    # 不一致 → 退回
    s.append(arrow(640, 535, 640, 585, "不一致"))
    s.append(box(455, 585, 370, 70, "按流程退回原步骤（必须写退回原因）", C_WARN, C_WARN_B))
    s.append(arrow(640, 655, 640, 700))
    s.append(box(470, 700, 340, 62, "商家修改资料后重新提交"))
    # 回流
    s.append(f'<path d="M470,731 L150,731 L150,237 L470,237" fill="none" stroke="{C_ARROW}" '
             f'stroke-width="1.7" stroke-dasharray="6 5" marker-end="url(#ah)"/>')
    s.append(f'<text x="160" y="700" font-family="{FONT}" font-size="12.5" fill="#5d6f82">反复退回 2–3 轮</text>')
    s.append(f'<text x="160" y="684" font-family="{FONT}" font-size="12.5" fill="#5d6f82">同一商品多次退回，原因散落</text>')
    # 未闭环
    s.append(arrow(640, 762, 640, 800))
    s.append(box(455, 800, 370, 66, "谁接手复核？改完哪份算闭环？", C_WARN, C_WARN_B, sub="制度未写死 → 靠人盯"))

    # 痛点侧栏
    pains = [
        ("判定权不明", "冲突时以哪份资料为准没有写死，\nAI 与人都不敢定案"),
        ("责任链断裂", "退回后接手人不明、闭环标准缺失，\n交接责任无从追溯"),
        ("记录分散", "同一商品多次退回，原因散在\n不同记录里，交接易漏"),
        ("数据口径不一", "220V / AC220V / 输出电压混填，\n证书过期未标注"),
        ("无可观测指标", "按商品还是按工单计未定，\n只考核退回数会诱发压单"),
    ]
    bx, by = 760, 585
    s.append(f'<text x="{bx}" y="{by - 14}" font-family="{FONT}" font-size="14.5" '
             f'font-weight="600" fill="#8a6d1f">现状五个痛点（访谈核对）</text>')
    for i, (t, d) in enumerate(pains):
        yy = by + i * 92
        s.append(f'<rect x="{bx}" y="{yy}" width="440" height="80" rx="8" fill="{C_PAIN}" '
                 f'stroke="{C_PAIN_B}" stroke-width="1.3"/>')
        s.append(f'<text x="{bx + 14}" y="{yy + 24}" font-family="{FONT}" font-size="14" '
                 f'font-weight="600" fill="{C_TEXT}">{i + 1}. {esc(t)}</text>')
        for j, ln in enumerate(d.split("\n")):
            s.append(f'<text x="{bx + 14}" y="{yy + 44 + j * 18}" font-family="{FONT}" '
                     f'font-size="12.5" fill="#6b5a2e">{esc(ln)}</text>')

    s.append(legend(60, 900))
    s.append(f'<text x="60" y="1080" font-family="{FONT}" font-size="13" fill="#6b7c8f">'
             f'注：本图依据六人访谈记录绘制；退回率 31%、初审 18 分钟为需求发现报告口径，基线仍待样本验证。</text>')
    s.append("</svg>")
    return "\n".join(s)


# ---------------------------------------------------------------- 改造后
def after():
    W = 1280
    s = [HEAD.replace("{H}", "1700").replace("{TITLE}", "改造后：冲突预检 + 人工签核的目标流程（对照 REQ-1~REQ-6）")
             .replace("{SUBTITLE}", "AI 只做「发现与举证」，人做「判定与签核」；冲突必须进人工队列，工单含触发原因/接手人/确认时间三硬字段")]

    # 主链
    s.append(box(450, 108, 380, 58, "商家提交上架申请", C_START, bold=True))
    s.append(arrow(640, 166, 640, 200))
    s.append(box(430, 200, 420, 72, "数据质量门（REQ-2）", C_AI, C_AI_B,
                 sub="主编号完整性 / 单位归一化 / 证书效期标注"))
    s.append(arrow(640, 272, 640, 306))
    s.append(box(430, 306, 420, 78, "AI 属性冲突预检（REQ-1）", C_AI, C_AI_B,
                 sub="跨字段比对：电压/功率/容量/型号覆盖｜只读、只提示"))
    s.append(arrow(640, 384, 640, 424))
    s.append(diamond(640, 480, 320, 112, "是否检出属性冲突？"))
    # 无冲突 → 人工复核
    s.append(arrow(800, 480, 930, 480, "否"))
    s.append(box(930, 440, 270, 80, "人工复核 + 岗位签核（REQ-4）", C_HUMAN, C_HUMAN_B,
                 sub="按品类授权清单，不得借用账号"))
    s.append(arrow(1065, 520, 1065, 566))
    s.append(box(930, 566, 270, 66, "生成确认单 → 签核放行", C_HUMAN, C_HUMAN_B, sub="确认单绑定签核人与时间"))
    s.append(arrow(1065, 632, 1065, 676))
    s.append(box(930, 676, 270, 60, "商品上架 + 审计留痕", C_START, bold=True))
    s.append(f'<text x="930" y="758" font-family="{FONT}" font-size="12.5" fill="#6b7c8f">'
             f'审计留痕：谁 / 何时 / AI 给了什么建议 / 人是否覆盖</text>')

    # 有冲突 → 转人工
    s.append(arrow(640, 536, 640, 578, "是"))
    s.append(box(430, 578, 420, 84, "转人工处置队列（REQ-3）", C_WARN, C_WARN_B,
                 sub="工单三硬字段：触发原因 / 接手人 / 确认时间"))
    s.append(arrow(640, 662, 640, 702))
    s.append(box(430, 702, 420, 78, "内容审核员判定：以哪份资料为准", C_HUMAN, C_HUMAN_B,
                 sub="AI 不代为判断哪边数值正确"))
    s.append(arrow(640, 780, 640, 820))
    s.append(diamond(640, 878, 320, 106, "能否直接判定？"))
    # 不能 → 退回整改
    s.append(arrow(480, 878, 300, 878, "否"))
    s.append(box(60, 840, 240, 80, "退回商家整改", C_WARN, C_WARN_B, sub="原因写清字段与两份资料"))
    s.append(f'<path d="M180,920 L180,348 L430,348" fill="none" stroke="{C_ARROW}" stroke-width="1.7" '
             f'stroke-dasharray="6 5" marker-end="url(#ah)"/>')
    s.append(f'<text x="192" y="1000" font-family="{FONT}" font-size="12.5" fill="#5d6f82">整改后复检</text>')
    # 能 → 签核
    s.append(arrow(640, 931, 640, 972, "是"))
    s.append(box(430, 972, 420, 74, "审核员签核 + 填写工单确认时间", C_HUMAN, C_HUMAN_B, sub="确认单闭环，重复确认拒绝"))
    s.append(f'<path d="M850,1009 L1235,1009 L1235,480 L1200,480" fill="none" stroke="{C_ARROW}" stroke-width="1.7" '
             f'marker-end="url(#ah)"/>')
    s.append(f'<text x="880" y="1002" font-family="{FONT}" font-size="12.5" fill="#5d6f82">工单闭环后转入人工复核</text>')

    # 护栏区
    gy = 1075
    s.append(f'<text x="60" y="{gy}" font-family="{FONT}" font-size="15" font-weight="600" '
             f'fill="{C_TEXT}">贯穿护栏（每一环节都生效）</text>')
    guards = [
        ("REQ-1 冲突预检", "只提示不处置；输出字段位置与两个数值，供人工核对"),
        ("REQ-3 转人工兜底", "冲突/资质临期/低置信度一律入队；工单缺三硬字段不算闭环"),
        ("REQ-4 授权与签核", "按品类列出签核人；AI 不得放行、不得驳回、不得代替签核"),
        ("REQ-5 快照兜底", "知识/数据带时间戳，超周期标「暂未更新」，不冒充实时"),
        ("REQ-6 指标口径", "固定分母（失败计入不剔除）；效率与责任双轨，禁止压单"),
    ]
    for i, (t, d) in enumerate(guards):
        yy = gy + 22 + i * 62
        s.append(f'<rect x="60" y="{yy}" width="1160" height="52" rx="8" fill="{C_BOX}" '
                 f'stroke="{C_BORDER}" stroke-width="1.3"/>')
        s.append(f'<text x="80" y="{yy + 22}" font-family="{FONT}" font-size="13.5" '
                 f'font-weight="600" fill="{C_TEXT}">{esc(t)}</text>')
        s.append(f'<text x="80" y="{yy + 41}" font-family="{FONT}" font-size="12.5" '
                 f'fill="#5d6f82">{esc(d)}</text>')

    s.append(f'<text x="60" y="{gy + 350}" font-family="{FONT}" font-size="13" fill="#6b7c8f">'
             f'注：需求编号对应《业务需求发现报告》REQ-1~REQ-6；本流程图与系统实现（app/tools.py、app/agent/、/admin/tasks）逐节点一致。</text>')
    s.append(legend(60, gy + 380))
    s.append("</svg>")
    return "\n".join(s)


open(os.path.join(OUT, "改造前_现状流程图.svg"), "w", encoding="utf-8").write(before())
open(os.path.join(OUT, "改造后_目标流程图.svg"), "w", encoding="utf-8").write(after())
print("已生成两份 SVG 流程图")
