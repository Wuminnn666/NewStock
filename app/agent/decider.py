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


def _intent(question: str) -> str:
    """判定用户意图：qa（只问规则）/ validate（判断某份资料能否过）/ submit（要走完整上架流程）。

    顺序很重要：先判 validate 再判 submit，
    否则「这份授权书能提交吗」会被误判成 submit。
    """
    q = question
    if any(k in q for k in ("能通过", "能过吗", "能否通过", "会不会被拒", "审核通过吗", "能上架吗")):
        return "validate"
    if any(k in q for k in ("提交", "上架申请", "帮我上架", "申请上架", "发布商品", "帮我建草稿")):
        return "submit"
    return "qa"


def _category(question: str) -> str:
    """从自然语言里猜类目（离线模式专用；真实模型会自己决定要不要调类目接口）。"""
    low = question.lower()
    for name, keys in CATEGORY_ALIASES:
        if any(k in low for k in keys):
            return name
    # 提到授权书但没说类目：按「服饰」处理（该品类正是「营业执照 + 品牌授权书」的典型组合）
    if "授权书" in question:
        return "服饰"
    return "通用"


def _materials(question: str) -> list:
    """从描述里抽取「已提供的资质材料」名称。"""
    low = question.lower()
    out = []
    for kw, name in MATERIAL_ALIASES:
        if kw in low and name not in out:
            out.append(name)
    return out


def _payload(question: str, category: str) -> dict:
    """把用户的一句话粗略结构化成商品 payload（离线演示用）。"""
    return {
        "title": question[:40],
        "category": category,
        "description": question,
        "materials": _materials(question),
    }


def _plan(question: str, session_id: str):
    """把三条业务路径翻译成「按轮组织的工具调用计划」。

    注意：每一轮（内层 list）里的多个调用是「同一轮内串行执行」的，
    这也解释了为什么「最多 5 轮」不会限制住完整上架流程：
      r1 只读查询（2 个可并行）
      r2 三项串行校验（有依赖，但同轮内顺序执行即可满足门禁）
      r3 创建草稿（触发强制中断）
    """
    intent = _intent(question)
    cat = _category(question)
    payload = _payload(question, cat)

    if intent == "qa":
        # 只查规则：绝不碰校验与草稿工具
        return [[ToolCall("search_review_rules", {"query": question, "top_k": 3})]]

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


def _qa_answer(results) -> str:
    """基于检索片段拼一个「离线可读」的回答。"""
    snippets = []
    reqs = None
    for r in results:
        if r.get("tool") == "get_category_requirements":
            reqs = r.get("required") or []
        for hit in (r.get("results") or []):
            snippets.append(hit)
    lines = ["依据检索到的审核规则条文：", ""]
    seen = 0
    for s in snippets[:3]:
        text = (s.get("content") or "").replace("\n", " ").strip()
        lines.append(f"- 《{s.get('file')}》片段#{s.get('chunk_index', 0) + 1}：{text[:90]}…")
        seen += 1
    if reqs:
        lines += ["", "该类目需提交的资质：" + "、".join(reqs) + "。"]
    if not seen and not reqs:
        lines = ["规则中未找到相关条文。"]
    elif not reqs:
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
            if need and "request_missing_material" not in done:
                return Decision("tool", [ToolCall("request_missing_material",
                                                  {"session_id": session_id, "items": need})])
            return Decision("final", content=_conclusion(last, results))

        # 5) 刚生成完待补清单 → 收尾等用户补充
        if last.get("tool") == "request_missing_material":
            return Decision("final", content=_missing_answer(last))

        # 5.5) 被服务端门禁拦下（模型越序调 create_goods_draft）
        #      这时不能假装成功，要如实告诉用户「被拦了、还差什么」。
        if last.get("code") in ("E-STATE-001", "E-STATE-003"):
            return Decision("final", content=(
                "已拦截本次创建草稿的请求：" + (last.get("message") or "前置校验未通过") + "\n\n"
                "请先把「资料完整性 → 资质 → 合规」三项校验走完整，"
                "三项全部通过后我再把草稿发给你确认，确认后才会真正创建。"))

        # 6) 按计划推进
        plan = _plan(question, session_id)
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

        # 7) 计划走完仍没有结论 → 兜底总结
        return Decision("final", content=_qa_answer(results) if done else _summary(results))


# ===========================================================================
# 四、工厂
# ===========================================================================
def get_decider():
    """有 API Key 用真实模型，没有则退化为离线启发式（便于本地演示与测试）。"""
    if settings.LLM_API_KEY:
        return LLMDecider()
    return HeuristicDecider()
