# -*- coding: utf-8 -*-
"""决策器（Decider）：Agent 循环里「模型决策」这一步的可替换实现。

为什么要抽象成接口？
    执行循环（Harness）只关心「下一步该调什么工具 / 还是该收尾出最终答案」，
    不关心这个判断是大模型给的，还是规则脚本给的。
    于是同一个循环可以：
      · 生产环境   → LLMDecider（OpenAI 兼容 Function Calling，供应商可换）
      · 没有 Key   → HeuristicDecider（离线启发式，用于本地验证编排链路 / 单测）
      · 将来       → 换成别的推理服务，只加一个类，Harness 一行不改

内部消息格式（与 OpenAI 略有差异，Harness 统一维护，发请求时再转换）：
    {"role": "system",  "content": "..."}
    {"role": "user",    "content": "..."}
    {"role": "assistant", "content": "", "tool_calls": [{"id","name","arguments"}]}
    {"role": "tool",    "name": "...", "tool_call_id": "...", "content": "<JSON 字符串>"}
"""
import json
import re
import uuid
from dataclasses import dataclass, field

import httpx

from ..config import settings
from ..errors import ApiError
from ..tools import CATEGORY_REQ


# ===========================================================================
# 一、数据结构
# ===========================================================================
@dataclass
class ToolCall:
    """一次工具调用意图（模型说「我要调 X，参数是 Y」）。"""
    name: str
    arguments: dict = field(default_factory=dict)
    call_id: str = ""

    def ensure_id(self) -> "ToolCall":
        """补齐 call_id：OpenAI 协议要求 assistant.tool_calls 与 tool 消息一一对应。"""
        if not self.call_id:
            self.call_id = "call_" + uuid.uuid4().hex[:8]
        return self


@dataclass
class Decision:
    """一次决策的结果：要么继续调工具（tool），要么给出最终回答（final）。"""
    kind: str                              # "tool" | "final"
    tool_calls: list = field(default_factory=list)
    content: str = ""


# ===========================================================================
# 二、真实大模型决策器（OpenAI 兼容 Function Calling）
# ===========================================================================
class LLMDecider:
    """走 /chat/completions，开启 tools + tool_choice=auto。

    决策用非流式（要拿结构化的 tool_calls），最终答案由 Harness 按句切分推送，
    避免「决策一次 + 生成一次」双倍调用成本。
    """

    def _to_openai_messages(self, messages):
        """内部消息格式 → OpenAI messages 格式。"""
        out = []
        for m in messages:
            role = m.get("role")
            if role == "assistant" and m.get("tool_calls"):
                out.append({
                    "role": "assistant",
                    "content": m.get("content") or "",
                    "tool_calls": [{
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"],
                                     "arguments": json.dumps(tc["arguments"], ensure_ascii=False)},
                    } for tc in m["tool_calls"]],
                })
            elif role == "tool":
                out.append({"role": "tool", "tool_call_id": m.get("tool_call_id"),
                            "content": m.get("content") or ""})
            else:
                out.append({"role": role, "content": m.get("content") or ""})
        return out

    def _to_openai_tools(self, tools):
        """工具规范 → OpenAI tools 格式（我们内部存的是精简版 schema）。"""
        return [{"type": "function", "function": t} for t in tools]

    async def decide(self, messages, tools, ctx) -> Decision:
        url = settings.LLM_BASE_URL.rstrip("/") + "/chat/completions"
        payload = {
            "model": settings.LLM_MODEL,
            "messages": self._to_openai_messages(messages),
            "tools": self._to_openai_tools(tools),
            "tool_choice": "auto",
            "temperature": 0.1,        # 决策要稳定，不要发散
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {settings.LLM_API_KEY}"}
        try:
            async with httpx.AsyncClient(timeout=settings.AGENT_DECIDE_TIMEOUT) as client:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code >= 400:
                    raise ApiError("50003", f"智能体决策失败：模型服务返回 {resp.status_code}", 502)
                data = resp.json()
        except ApiError:
            raise
        except Exception as e:
            raise ApiError("50003", f"智能体决策失败：{e}", 502)

        msg = (data.get("choices") or [{}])[0].get("message") or {}
        calls = msg.get("tool_calls") or []
        if calls:
            parsed = []
            for c in calls:
                fn = c.get("function") or {}
                raw = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw) if isinstance(raw, str) else dict(raw)
                except Exception:
                    args = {}      # 模型给了非法 JSON：交给工具层报参数错误，别让循环崩掉
                parsed.append(ToolCall(fn.get("name") or "", args, c.get("id") or "").ensure_id())
            return Decision("tool", tool_calls=parsed)

        return Decision("final", content=(msg.get("content") or "").strip())


# ===========================================================================
# 三、离线启发式决策器（无 API Key 时使用）
# ===========================================================================
# 说明：它不是「智能」，而是一套把三条典型业务路径脚本化的规则脑。
# 它的价值在于：没有模型凭证时，也能把「决策 → 工具 → 回填 → 再决策 → 中断确认」
# 整条编排链路跑通并留下完整日志（阶段 5 分析需要真实数据）。
# 换成 LLMDecider 时，这段规则完全不参与。

CATEGORY_ALIASES = [
    ("3C数码", ("3c", "数码", "电子")),
    ("食品", ("食品", "零食", "生鲜")),
    ("化妆品", ("化妆品", "美妆", "护肤")),
    ("儿童玩具", ("玩具", "儿童")),
    ("服饰", ("服饰", "服装", "鞋服", "衣服")),
]

MATERIAL_ALIASES = [
    ("营业执照", "营业执照"),
    ("食品经营许可证", "食品经营许可证"),
    ("化妆品生产许可证", "化妆品生产许可证"),
    ("ccc", "CCC证书"),
    ("3c认证", "CCC证书"),
    ("商标授权书", "商标授权书"),
    ("品牌授权书", "品牌授权书"),
    ("授权书", "品牌授权书"),
    ("质检报告", "质检报告"),
    ("检验报告", "质检报告"),
]

# 明确表示「这次不要真提交」：出现后必须压回只读咨询。
# 没有这条，「…我只是先了解，不用帮我提交」会因为含"帮我提交"被误判成 submit，
# 进而替用户跑校验、甚至走到建草稿——这是过度执行，属于真实缺陷。
NO_COMMIT_HINTS = ("只是先了解", "先了解", "不用帮我提交", "不用提交", "先别提交",
                   "只是问问", "先问问", "不要提交", "不用上架")

# 明确要求跳过校验：必须拒绝并说明原因（不是照做）。
SKIP_HINTS = ("别校验", "不用校验", "不要校验", "跳过校验", "省掉校验",
              "别重复校验", "不用复核", "直接给我建", "别审了", "不用走流程")

# 「资料齐全」这类模糊表述：按该类目的标准清单视为已提供
ALL_MATERIAL_HINTS = ("资料齐全", "资料都齐", "资质齐全", "资质都齐", "材料齐全",
                      "全部提供", "都已提供", "都提供了")

# 提交类动作词：只有出现这些才算「让我真去提交/建草稿」
SUBMIT_HINTS = ("帮我提交", "提交申请", "上架申请", "帮我上架", "申请上架", "发布商品",
                "把草稿", "建草稿", "创建草稿", "草稿建", "建出来", "create_goods_draft")

# 文案合规咨询：问的是"这句话能不能用"，不是"我这个商品能不能过"。
# 这类问题要的是条文依据，不该反过来向用户索要营业执照。
COPY_HINTS = ("详情页", "文案", "话术", "主图", "写进", "写成", "标题写", "描述写",
              "详情", "宣传语", "广告语")

# 问「要交什么」的措辞：只有这类问题才需要额外查一次类目清单
ASK_MATERIAL_HINTS = ("资质", "材料", "资料", "证件", "证书", "授权", "要交", "准备")

REFUSE_SKIP = ("先说明一点：创建商品草稿前必须完成「资料完整性 → 资质 → 合规」三项校验，"
               "这个顺序不能跳过，我也不能凭「看起来没问题」就替你建草稿。"
               "我先按规范把校验跑一遍，通过的环节不会为难你。\n\n")


def _has(text: str, words) -> bool:
    """是否包含任意关键词（统一小写比较，避免 3C / 3c 漏判）。"""
    low = (text or "").lower()
    return any(w.lower() in low for w in words)


def _quote(text: str, key: str) -> str:
    """从「标题“xxx”」这类写法里取出引号内的值（支持中英文引号）。"""
    m = re.search(rf"{key}\s*[是为：:]?\s*[\u201c\"\u300c']([^\u201d\"\u300d']{{1,60}})[\u201d\"\u300d']", text)
    return m.group(1).strip() if m else ""


def _history(messages) -> str:
    """把历史里用户说过的话拼起来，用于补全被省略的类目/材料（多轮上下文）。"""
    return "\n".join(m.get("content") or "" for m in messages if m.get("role") == "user")


def _intent(question: str, history: str = "") -> str:
    """判定意图：qa（只问规则）/ material_only（只看资料齐不齐）/
    validate（判断能不能过）/ submit（走完整上架流程）。

    判定顺序有讲究：
      1. 先看「不用提交」这类否定，否则「…不用帮我提交」会因含"帮我提交"被误判；
      2. material_only 早于 validate，「只看看资料全不全，先别做资质判断」不能被当成完整校验；
      3. validate 早于 submit，「这份授权书能通过吗」不是让你去提交；
      4. 最后才认提交意图，且必须带动作词（帮我提交/建草稿…），
         单纯出现"提交"二字（如"提交后多久审核"）不算。
    """
    q = question
    if _has(q, NO_COMMIT_HINTS):
        return "qa"
    if _has(q, ("填得全不全", "资料全不全", "齐不齐", "只看资料", "先别做资质",
                "别做资质", "只校验资料", "资料完整性", "只帮我看")):
        return "material_only"
    # 文案合规咨询：问"这句宣传语能不能用"，要的是条文，不是替他去校验商品
    if _has(q, COPY_HINTS) and not _has(q, SUBMIT_HINTS):
        return "qa"
    if _has(q, ("能通过", "能过吗", "能否通过", "会不会被拒", "审核通过吗", "能上架吗",
                "能过审", "重新校验", "重新核对", "重新看", "再校验")):
        return "validate"
    if _has(q, SUBMIT_HINTS):
        return "submit"
    return "qa"


def _category(question: str, history: str = "") -> str:
    """从自然语言里猜类目；本轮没提就从历史里找（多轮里"它"指代上一个类目）。"""
    for text in (question, history):
        low = (text or "").lower()
        for name, keys in CATEGORY_ALIASES:
            if any(k in low for k in keys):
                return name
    if "授权书" in (question + history):
        return "服饰"      # 提授权书但没说类目：服饰是「营业执照 + 品牌授权书」的典型组合
    return "通用"


# 证件名的后续状态词：出现这些说明「还没拿到」，不能算作已提供
_NOT_AVAILABLE = ("还在办理", "办理中", "正在办理", "还没下来", "还没拿到", "未取得",
                  "待办", "申请中", "没办下来", "丢了", "过期", "已失效", "失效")


def _materials(question: str) -> list:
    """从描述里抽取「已提供的资质材料」名称。

    难点不是找到证件名，而是判断它到底有没有："化妆品生产许可证还在办理中"
    提到了证件名，但意思是没拿到。只做关键词命中会把缺证误判成齐备，
    进而让整个校验链放行——这是最危险的一类误判。
    """
    low = question.lower()
    out = []
    for kw, name in MATERIAL_ALIASES:
        pos = low.find(kw)
        if pos < 0 or name in out:
            continue
        tail = low[pos + len(kw): pos + len(kw) + 14]     # 证件名往后看一小段
        if any(w in tail for w in _NOT_AVAILABLE):
            continue                                       # 还在办理 / 已过期 → 不计入
        out.append(name)
    return out


def _payload(question: str, category: str, history: str = "") -> dict:
    """把用户的一句话粗略结构化成商品 payload（离线演示用）。

    真实模型会自己产出结构化参数；这里用正则 + 别名词表做等价的粗略抽取，
    目的只是让离线链路能跑出与在线一致的轨迹。
    """
    title = _quote(question, "标题") or _quote(question, "商品名") or question[:40]
    desc = _quote(question, "描述") or _quote(question, "详情") or question
    # 材料跨轮累计：第二轮说"质检报告补上了"，要能和第一轮的营业执照合起来看
    mats = _materials(history + "\n" + question)
    if _has(question, ALL_MATERIAL_HINTS):
        # 「资料齐全」没逐个点名 → 按该类目标准清单视为已提供
        merged = list(CATEGORY_REQ.get(category) or [])
        merged += [m for m in mats if m not in merged]
        mats = merged
    return {"title": title, "category": category, "description": desc, "materials": mats}


def _plan(question: str, session_id: str, history: str = ""):
    """把四条业务路径翻译成「按轮组织的工具调用计划」。

    注意：每一轮（内层 list）里的多个调用是「同一轮内串行执行」的，
    这也解释了为什么「最多 5 轮」不会限制住完整上架流程：
      r1 只读查询（查类目 + 查规则，可并行）
      r2 三项串行校验（有依赖，同轮内顺序执行即可满足门禁）
      r3 创建草稿（触发强制中断）
    """
    intent = _intent(question, history)
    cat = _category(question, history)
    payload = _payload(question, cat, history)

    if intent == "material_only":
        # 用户只要"齐不齐"，那就不该顺手做资质与合规判断
        return [[ToolCall("material_validate", {"session_id": session_id, "payload": payload})]]

    if intent == "qa":
        calls = []
        if _has(question, ASK_MATERIAL_HINTS):
            calls.append(ToolCall("get_category_requirements", {"category": cat}))
        calls.append(ToolCall("search_review_rules", {"query": question, "top_k": 3}))
        return [calls]

    checks = [
        ToolCall("material_validate", {"session_id": session_id, "payload": payload}),
        ToolCall("qualification_audit", {"session_id": session_id, "payload": payload}),
        ToolCall("compliance_check", {"session_id": session_id, "payload": payload}),
    ]

    if intent == "validate":
        return [[ToolCall("get_category_requirements", {"category": cat})], checks]

    return [
        [ToolCall("search_review_rules", {"query": f"{cat}类商品上架资料标准", "top_k": 3}),
         ToolCall("get_category_requirements", {"category": cat})],
        checks,
        # 令牌占位，真正调用前用 material_validate 返回的真实 token 替换
        [ToolCall("create_goods_draft",
                  {"session_id": session_id, "payload": payload, "precheck_token": "__TOKEN__"})],
    ]


def _token(results) -> str:
    """从历史工具结果里取最近一次 material_validate 签发的预检令牌。"""
    token = ""
    for r in results:
        if r.get("tool") == "material_validate" and r.get("token"):
            token = r["token"]
    return token


def _guard(text: str, question: str) -> str:
    """用户要求「跳过校验直接建草稿」时，先明确拒绝，再给正常结论。"""
    return (REFUSE_SKIP + text) if _has(question, SKIP_HINTS) else text


def _conclusion(last: dict, results) -> str:
    """校验失败时给一张「人话版」结论卡。"""
    issues = last.get("issues") or []
    passed_before = [r for r in results if r.get("tool") in
                     ("material_validate", "qualification_audit") and r.get("passed")]
    head = {
        "material_validate": "资料完整性校验",
        "qualification_audit": "资质审核",
        "compliance_check": "合规校验",
    }.get(last.get("tool"), "校验")
    lines = [f"结论：**无法通过**（{head}未通过）", "", "问题清单："]
    lines += [f"- {i}" for i in issues] or ["- 未返回具体原因"]
    if passed_before:
        lines += ["", "已通过的环节：" + "、".join(
            {"material_validate": "资料完整性", "qualification_audit": "资质"}.get(r["tool"], r["tool"])
            for r in passed_before)]
    lines += ["", "建议：按上述清单整改后重新提交，整改会重新走一遍三项校验。"]
    return "\n".join(lines)


def _missing_answer(last: dict) -> str:
    items = last.get("missing") or []
    lines = ["结论：**当前资料无法通过审核**，需要先补齐材料。", "", "待补清单："]
    lines += [f"- {i}" for i in items] or ["- （未返回明细）"]
    lines += ["", "补齐后把材料名称发给我，我会重新走「资料 → 资质 → 合规」三项校验。"]
    return "\n".join(lines)


def _qa_answer(question: str, results) -> str:
    """基于检索片段拼一个「离线可读」的回答；没有依据时明确说未找到。"""
    snippets, reqs, support = [], None, None
    for r in results:
        if r.get("tool") == "get_category_requirements":
            reqs = r.get("required")
        if r.get("tool") == "search_review_rules":
            support = r.get("support")
            snippets.extend(r.get("results") or [])

    # 依据不足 → 直说未找到，绝不硬编。这是本系统最容易翻车的地方：
    # top-k 永远有返回，看起来"有材料"，实则答非所问。
    if support is not None and not support.get("supported", True):
        why = support.get("reason") or "检索到的条文与问题不匹配"
        return ("规则中未找到相关条文。\n\n"
                f"（判定依据：{why}）\n\n"
                "为避免给出错误的上架标准，我不做推测。建议：\n"
                "- 联系合规部门或资深审核确认该类目要求；\n"
                "- 由管理员在「规则知识库」补充对应制度文档后我再依据条文回答。")

    lines = ["依据检索到的审核规则条文：", ""]
    seen = 0
    for s in snippets[:3]:
        text = (s.get("content") or "").replace("\n", " ").strip()
        lines.append(f"- 《{s.get('file')}》片段#{s.get('chunk_index', 0) + 1}：{text[:90]}…")
        seen += 1
    if reqs:
        lines += ["", "该类目需提交的资质：" + "、".join(reqs) + "。"]
    if not seen and not reqs:
        return "规则中未找到相关条文。"
    if not reqs:
        lines += ["", "如需我核对具体资质清单，请告诉我商品类目。"]
    return "\n".join(lines)


def _summary(results) -> str:
    """轮次用尽或计划走完时的兜底总结（绝不假装已完成）。"""
    done = [r.get("tool") for r in results if r.get("tool")]
    if not done:
        return "抱歉，本次没有成功执行任何步骤，请补充更具体的商品信息后重试。"
    return ("已完成的步骤：" + "、".join(done) +
            "。由于达到轮次上限或缺少必要信息，流程尚未走完，请补充资料后继续。")


class HeuristicDecider:
    """离线启发式决策器：按「计划 + 已执行工具集合」推导下一步。"""

    async def decide(self, messages, tools, ctx) -> Decision:
        session_id = ctx.get("session_id", "")
        question = ctx.get("question", "")
        # 多轮上下文：类目、材料可能只在上一轮说过，必须把历史带上
        history = _history(messages)
        intent = _intent(question, history)

        # 1) 把历史里的工具结果整理出来（这就是「结果回填」之后模型能看到的东西）
        #    注意：同轮内因前置失败而被标记 skipped 的占位结果要排除掉，
        #    否则「上一条真实结果」会被占位符盖住，模型就看不到真正的失败原因。
        results, done = [], []
        for m in messages:
            if m.get("role") == "tool":
                done.append(m.get("name"))
                try:
                    r = json.loads(m.get("content") or "{}")
                except Exception:
                    r = {}
                if r.get("skipped"):
                    continue
                results.append(r)
        last = results[-1] if results else {}

        # 2) 用户拒绝了草稿确认 → 收尾（不再创建）
        if last.get("confirm") == "rejected":
            return Decision("final", content=(
                "好的，已取消创建商品草稿，未写入任何数据。\n\n"
                "本次三项校验的结果仍然有效，如果你只是想改文案或换资质，"
                "把新的商品资料发我，我会重新校验后再给你确认。"))

        # 3) 草稿已创建 → 收尾
        if last.get("status") == "draft_created":
            return Decision("final", content=(
                f"草稿已创建，编号 **{last.get('draft_id')}**，当前状态：已进入待提交队列。\n\n"
                "下一步你可以：补充主图/详情页后提交人工审核；"
                "或告诉我需要修改的字段，我改完会重新走校验。"))

        # 4) 上一轮失败 → 索要资料 / 给结论
        if last.get("passed") is False:
            issues = last.get("issues") or []
            need = [i for i in issues
                    if i.startswith("缺少资质") or i.startswith("缺少字段") or "未上传" in i]
            # 用户只要"看看齐不齐"时，不要越界生成待补清单并推进流程
            if need and "request_missing_material" not in done and intent != "material_only":
                return Decision("tool", [ToolCall("request_missing_material",
                                                  {"session_id": session_id, "items": need})])
            return Decision("final", content=_guard(_conclusion(last, results), question))

        # 5) 刚生成完待补清单 → 收尾等用户补充
        if last.get("tool") == "request_missing_material":
            return Decision("final", content=_guard(_missing_answer(last), question))

        # 5.5) 被服务端门禁拦下（模型越序调 create_goods_draft）
        #      这时不能假装成功，要如实告诉用户「被拦了、还差什么」。
        if last.get("code") in ("E-STATE-001", "E-STATE-003"):
            return Decision("final", content=(
                "已拦截本次创建草稿的请求：" + (last.get("message") or "前置校验未通过") + "\n\n"
                "请先把「资料完整性 → 资质 → 合规」三项校验走完整，"
                "三项全部通过后我再把草稿发给你确认，确认后才会真正创建。"))

        # 6) 按计划推进
        plan = _plan(question, session_id, history)
        token = _token(results)
        for rnd in plan:
            calls = []
            for tc in rnd:
                if tc.name in done:
                    continue
                args = dict(tc.arguments)
                if args.get("precheck_token") == "__TOKEN__":
                    args["precheck_token"] = token
                calls.append(ToolCall(tc.name, args))
            if calls:
                return Decision("tool", calls)

        # 7) 计划走完 → 按意图收尾
        #    这里必须分意图：校验类问题如果走通用检索答案，会莫名其妙回一句
        #    「规则中未找到」——那是把"校验结果"误当成"规则问答"了。
        if intent == "material_only" and last.get("tool") == "material_validate" and last.get("passed"):
            n = len((last.get("issues") or []))
            return Decision("final", content=_guard(
                "资料完整性校验通过：必填字段齐全，未见缺项。"
                "本次没有做资质与合规判断（按你的要求）；需要我继续往下核验时告诉我。",
                question))

        if intent in ("validate", "submit") and last.get("tool") == "compliance_check" and last.get("passed"):
            return Decision("final", content=_guard(
                "结论：**三项校验全部通过**——资料完整、资质齐备、未发现违规用语，"
                "可以进入上架流程。", question))

        return Decision("final", content=_guard(
            _qa_answer(question, results) if done else _summary(results), question))


# ===========================================================================
# 四、工厂
# ===========================================================================
def get_decider():
    """有 API Key 用真实模型，没有则退化为离线启发式（便于本地演示与测试）。"""
    if settings.LLM_API_KEY:
        return LLMDecider()
    return HeuristicDecider()
