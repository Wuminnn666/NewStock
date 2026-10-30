# -*- coding: utf-8 -*-
"""Harness —— 智能体执行循环（本项目的 Agent 运行时）。

为什么叫 Harness（执行护具）而不是「框架」？
    一个好的 Agent 运行时，价值不在「能不能调大模型」，而在「调错了也拦得住」。
    Harness 就是这层护具：它包住模型，把模型的一次次「提议」变成受控的执行，
    并在关键动作上强制插入人工确认。

循环（每一步都写日志）：
        ┌─────────────────────────────────────────────┐
        │  ① 模型决策（decide）                        │
        │      ├─ 要给最终答案 → 推流输出 → 结束        │
        │      └─ 要调工具 → 进入 ②                    │
        │  ② 门禁检查（gate）                          │
        │      ├─ create_goods_draft 且前置未通过 → 拒绝 │
        │      └─ create_goods_draft 且前置通过 → ③中断  │
        │  ③ 执行工具（dispatch）                       │
        │  ④ 结果回填（messages.append）→ 回到 ①         │
        └─────────────────────────────────────────────┘
    轮次预算：settings.AGENT_MAX_ROUNDS（默认 5）。
    一轮 = 一次模型决策；一轮内可以发起多个工具调用（按返回顺序串行执行）。

中断-续跑机制：
    create_goods_draft 不直接执行，而是生成确认单并挂起（run.status=waiting_confirm）。
    决定上下文快照存在 agent_runs.messages 里，前端确认后再从断点续跑，
    所以「确认」不是重新问一遍，而是接着上一次的推理继续。
"""
import asyncio
import json
import time
from datetime import datetime

from ..config import settings
from ..errors import ApiError
from ..tools import CATEGORY_REQ, DISPATCH, TOOL_SCHEMAS, create_goods_draft, get_precheck_state
from . import trace
from .decider import get_decider
from .prompts import build_system_prompt

# 状态 → 中文可读描述（给日志摘要用）
STATUS_CN = {"ok": "成功", "error": "报错", "gate_blocked": "门禁拦截",
             "skipped": "跳过", "confirm_pending": "待确认"}


def _sse(event: str, data) -> str:
    """构造 SSE 帧（与 api.py 的 sse 保持同一协议，避免循环引用所以本地实现）。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _chunks(text: str, size: int = 24):
    """把最终答案按 ~size 字切块，制造打字机效果（不额外调用模型）。"""
    for i in range(0, len(text), size):
        yield text[i:i + size]


def _brief(result: dict, limit: int = 200) -> str:
    """工具结果摘要（截断），写进 agent_steps.result_summary 供阶段 5 看板展示。"""
    try:
        s = json.dumps(result, ensure_ascii=False)
    except Exception:
        s = str(result)
    return s[:limit]


class AgentHarness:
    """一次执行 = 一个 Harness 实例（对应一条 agent_runs 记录）。"""

    def __init__(self, session_id: str, user_id: str, decider=None, max_rounds: int = None):
        self.session_id = session_id
        self.user_id = str(user_id)
        self.decider = decider or get_decider()
        self.max_rounds = max_rounds or settings.AGENT_MAX_ROUNDS
        self.run_id = ""
        self.question = ""

    # =====================================================================
    # 对外入口（FastAPI 直接用 astream 输出 SSE；CLI/测试用 aevents 拿结构化事件）
    # =====================================================================
    async def astream(self, question: str):
        async for event, data in self.aevents(question):
            yield _sse(event, data)

    async def astream_confirm(self, confirm_id: str, approved: bool, decided_by: str):
        async for event, data in self.aevents_confirm(confirm_id, approved, decided_by):
            yield _sse(event, data)

    # ---------------------------------------------------------------------
    # 新会话执行
    # ---------------------------------------------------------------------
    async def aevents(self, question: str):
        self.question = question
        trace.ensure_chat_session(self.session_id, self.user_id, question)
        trace.append_chat_message(self.session_id, "user", question)

        self.run_id = trace.new_run(self.session_id, self.user_id, question)
        messages = [
            {"role": "system", "content": build_system_prompt(self.session_id, self.max_rounds)},
            {"role": "user", "content": question},
        ]
        trace.save_context(self.run_id, messages, 0)

        yield ("meta", {"session_id": self.session_id, "run_id": self.run_id})
        async for ev in self._loop(messages, start_round=1):
            yield ev

    # ---------------------------------------------------------------------
    # 人工确认后续跑（同一 run 的断点恢复）
    # ---------------------------------------------------------------------
    def load_pending(self, confirm_id: str):
        """校验确认单可用性（存在 / 归属 / 未处理 / 未过期）。

        必须在「开始流式响应之前」调用，否则错误只能以断流的形式出现，
        前端拿不到 code/message（HTTP 已经是 200 了）。
        """
        row = trace.get_confirmation(confirm_id)
        if not row:
            raise ApiError("40401", "确认单不存在或已失效", 404)
        if row.session_id != self.session_id:
            raise ApiError("40301", "无权操作该确认单", 403)
        if row.status != "pending":
            raise ApiError("40903", "该确认单已处理或已过期", 409)

        # 过期校验：防止几分钟前的旧草稿被误创建
        # 注意：created_at 存的是 UTC naive 时间，必须用 utcnow() 相减，
        # 用 .timestamp() 会被当成本地时区解释（东八区会凭空多出 8 小时 → 误判过期）。
        age_min = (datetime.utcnow() - row.created_at).total_seconds() / 60
        if age_min > settings.AGENT_CONFIRM_TTL_MIN:
            trace.expire_confirmation(confirm_id)
            raise ApiError("40904", f"确认单已超过 {settings.AGENT_CONFIRM_TTL_MIN} 分钟有效期，请重新发起", 409)
        return row

    async def aevents_confirm(self, confirm_id: str, approved: bool, decided_by: str):
        row = self.load_pending(confirm_id)

        run = trace.get_run(row.run_id)
        if not run:
            raise ApiError("40401", "执行记录不存在", 404)

        self.run_id = run.run_id
        self.question = run.question or ""
        messages = json.loads(run.messages or "[]")
        pending = json.loads(run.pending_call or "{}")
        call_id = pending.get("call_id") or "call_pending"

        trace.decide_confirmation(confirm_id, approved, decided_by)

        if not approved:
            # 拒绝：把「用户取消」作为工具结果回填，让模型自然地收尾
            messages.append({"role": "tool", "name": "create_goods_draft",
                             "tool_call_id": call_id,
                             "content": json.dumps({"tool": "create_goods_draft",
                                                    "confirm": "rejected"}, ensure_ascii=False)})
            trace.log_step(self.run_id, run.rounds,
                           trace.next_step_no(self.run_id, run.rounds), "create_goods_draft",
                           pending.get("arguments") or {}, "skipped", 0,
                           "用户取消确认，未创建草稿")
            yield ("meta", {"session_id": self.session_id, "run_id": self.run_id,
                            "resumed": True})
            async for ev in self._loop(messages, start_round=run.rounds + 1):
                yield ev
            return

        # 同意：真正执行 create_goods_draft（此时才写业务数据）
        payload = json.loads(row.payload or "{}")
        t0 = time.perf_counter()
        try:
            result = create_goods_draft(session_id=self.session_id, payload=payload,
                                        precheck_token=row.precheck_token)
            status, err_code = "ok", None
        except ApiError as e:
            result = {"tool": "create_goods_draft", "error": e.message, "code": e.code}
            status, err_code = "error", e.code
        elapsed = int((time.perf_counter() - t0) * 1000)

        trace.log_step(self.run_id, run.rounds,
                       trace.next_step_no(self.run_id, run.rounds), "create_goods_draft",
                       {"payload": payload, "precheck_token": row.precheck_token},
                       status, elapsed, _brief(result), err_code)

        messages.append({"role": "tool", "name": "create_goods_draft", "tool_call_id": call_id,
                         "content": json.dumps(result, ensure_ascii=False)})
        trace.save_context(self.run_id, messages, run.rounds, status="running")

        yield ("meta", {"session_id": self.session_id, "run_id": self.run_id, "resumed": True})
        yield ("tool_result", {"round": run.rounds, "tool": "create_goods_draft",
                               "status": status, "elapsed_ms": elapsed,
                               "summary": _brief(result, 120)})
        async for ev in self._loop(messages, start_round=run.rounds + 1):
            yield ev

    # =====================================================================
    # 主循环
    # =====================================================================
    async def _loop(self, messages, start_round: int):
        round_no = start_round

        while round_no <= self.max_rounds:
            yield ("thinking", {"round": round_no, "max": self.max_rounds})

            # ---------- ① 模型决策 ----------
            try:
                decision = await self.decider.decide(
                    messages, TOOL_SCHEMAS,
                    {"session_id": self.session_id, "question": self.question})
            except ApiError as e:
                trace.finish(self.run_id, "failed", error=e.message)
                yield ("error", {"code": e.code, "message": e.message})
                return
            except Exception as e:  # 兜底：决策层任何异常都不能让流断得莫名其妙
                trace.finish(self.run_id, "failed", error=str(e))
                yield ("error", {"code": "50003", "message": f"智能体决策异常：{e}"})
                return

            # ---------- ② 只出答案，不再调工具 → 收尾 ----------
            if decision.kind == "final" or not decision.tool_calls:
                trace.log_step(self.run_id, round_no, 0, "(finish)", {}, "ok", 0,
                               (decision.content or "")[:200])
                for ev in self._emit_final(messages, decision.content):
                    yield ev
                return

            # ---------- ③ 工具轮 ----------
            calls = [tc.ensure_id() for tc in decision.tool_calls]
            messages.append({
                "role": "assistant", "content": "",
                "tool_calls": [{"id": tc.call_id, "name": tc.name,
                                "arguments": tc.arguments} for tc in calls],
            })

            interrupted = False
            for idx, tc in enumerate(calls, start=1):
                yield ("tool_call", {"round": round_no, "step": idx,
                                     "tool": tc.name, "args": tc.arguments})

                # 未知工具：回填错误让模型自我纠正，而不是直接崩
                if tc.name not in DISPATCH:
                    err = {"code": "E-TOOL-404", "message": f"工具 {tc.name} 不存在"}
                    trace.log_step(self.run_id, round_no, idx, tc.name, tc.arguments,
                                   "error", 0, err["message"], err["code"])
                    self._append_tool(messages, tc, err)
                    yield ("tool_result", {"round": round_no, "tool": tc.name,
                                           "status": "error", "summary": err["message"]})
                    continue

                # ===== 强制中断点：创建草稿前必须人工确认 =====
                if tc.name == "create_goods_draft":
                    gate_err = self._gate_create_draft(tc)
                    if gate_err:
                        # 前置校验没全过 / 令牌不对 → 拒绝执行，把错误回灌给模型
                        trace.log_step(self.run_id, round_no, idx, tc.name, tc.arguments,
                                       "gate_blocked", 0, gate_err["message"], gate_err["code"])
                        self._append_tool(messages, tc, gate_err)
                        yield ("tool_result", {"round": round_no, "tool": tc.name,
                                               "status": "gate_blocked",
                                               "summary": gate_err["message"]})
                        self._skip_rest(round_no, calls, idx, messages, "前置校验未通过")
                        break

                    # 通过门禁 → 生成确认单、挂起循环
                    payload = (tc.arguments or {}).get("payload") or {}
                    draft = self._build_draft(payload)
                    confirm_id = trace.create_confirmation(
                        self.run_id, self.session_id, draft, payload,
                        (tc.arguments or {}).get("precheck_token") or "")

                    trace.log_step(self.run_id, round_no, idx, tc.name, tc.arguments,
                                   "confirm_pending", 0, "已中断，等待提交方确认")
                    self._skip_rest(round_no, calls, idx, messages, "等待人工确认")
                    trace.save_context(
                        self.run_id, messages, round_no, status="waiting_confirm",
                        pending_call={"call_id": tc.call_id, "name": tc.name,
                                      "arguments": tc.arguments})

                    yield ("draft_start", {"confirm_id": confirm_id})
                    for line in self._draft_lines(draft):
                        yield ("draft_delta", {"text": line})
                        await asyncio.sleep(0.02)      # 制造「逐行送达」的观感
                    yield ("confirm_required", {"confirm_id": confirm_id, "draft": draft})
                    interrupted = True
                    return     # 挂起：等 /api/agent/confirm 续跑

                # ===== 普通工具执行 =====
                t0 = time.perf_counter()
                try:
                    result = DISPATCH[tc.name](**(tc.arguments or {}))
                    status, err_code = "ok", None
                except ApiError as e:
                    result = {"tool": tc.name, "error": e.message, "code": e.code}
                    status, err_code = "error", e.code
                except TypeError as e:
                    result = {"tool": tc.name, "error": f"参数错误：{e}"}
                    status, err_code = "error", "E-TOOL-400"
                except Exception as e:
                    result = {"tool": tc.name, "error": f"工具执行异常：{e}"}
                    status, err_code = "error", "E-TOOL-500"
                elapsed = int((time.perf_counter() - t0) * 1000)

                trace.log_step(self.run_id, round_no, idx, tc.name, tc.arguments,
                               status, elapsed, _brief(result), err_code)
                self._append_tool(messages, tc, result)
                yield ("tool_result", {"round": round_no, "tool": tc.name, "status": status,
                                       "elapsed_ms": elapsed, "summary": _brief(result, 160)})

                # 校验失败 → 本轮剩余调用不再执行（避免连环报错污染上下文）
                if isinstance(result, dict) and result.get("passed") is False:
                    self._skip_rest(round_no, calls, idx, messages, "前置校验未通过")
                    break

            if interrupted:      # 理论到不了这（上面已 return），留着防御
                return

            round_no += 1
            trace.save_context(self.run_id, messages, round_no - 1)

        # ---------- 轮次用尽 ----------
        msg = (f"已达到最大决策轮次（{self.max_rounds} 轮），流程尚未完成。"
               "已执行的步骤见上方轨迹；请补充必要资料后我再继续。")
        for ev in self._emit_final(messages, msg):
            yield ev

    # =====================================================================
    # 门禁 / 草稿 / 日志辅助
    # =====================================================================
    def _gate_create_draft(self, tc) -> dict:
        """创建草稿前的服务端硬门禁（不依赖提示词，模型绕不过去）。

        校验三件事：
          1. 资料完整性、资质、合规 三项预检是否全部通过；
          2. 请求携带的 precheck_token 是否等于服务端签发的那一枚；
          3. payload 是否和校验时一致（由 create_goods_draft 内部再算哈希复核）。
        """
        st = get_precheck_state(self.session_id)
        missing = []
        if not st.get("material"):
            missing.append("资料完整性校验")
        if not st.get("qualification"):
            missing.append("资质审核")
        if not st.get("compliance"):
            missing.append("合规校验")
        if missing:
            return {"code": "E-STATE-001",
                    "message": "前置校验未通过，禁止创建商品草稿，缺少：" + "、".join(missing)}
        token = (tc.arguments or {}).get("precheck_token") or ""
        if not st.get("token") or token != st.get("token"):
            return {"code": "E-STATE-003",
                    "message": "预检令牌无效或已过期，请重新执行三项校验后再创建草稿"}
        return {}

    @staticmethod
    def _build_draft(payload: dict) -> dict:
        """把 payload 渲染成给人看的草稿结构（确认页直接用）。"""
        cat = payload.get("category") or "未指定"
        return {
            "title": payload.get("title") or "（未填写）",
            "category": cat,
            "description": payload.get("description") or "（未填写）",
            "materials": payload.get("materials") or [],
            "required": CATEGORY_REQ.get(cat, []),
            "checks": {"资料完整性": "通过", "资质审核": "通过", "合规校验": "通过"},
        }

    @staticmethod
    def _draft_lines(draft: dict):
        """逐行产出草稿文本 —— 前端据此做流式展示。"""
        yield "【商品上架草稿（待您确认）】\n"
        yield f"· 商品标题：{draft['title']}\n"
        yield f"· 商品类目：{draft['category']}\n"
        yield f"· 商品描述：{draft['description'][:80]}\n"
        yield f"· 资质材料：{'、'.join(draft['materials']) or '（无）'}\n"
        if draft["required"]:
            yield f"· 该类目要求：{'、'.join(draft['required'])}\n"
        yield "· 预检结论：资料完整性 ✅ ／ 资质 ✅ ／ 合规 ✅\n"
        yield "确认后才会正式创建草稿；取消则不会写入任何数据。\n"

    @staticmethod
    def _append_tool(messages, tc, payload: dict):
        """结果回填：工具输出以 tool 角色写回上下文，模型下一轮就能看到。"""
        messages.append({"role": "tool", "name": tc.name, "tool_call_id": tc.call_id,
                         "content": json.dumps(payload, ensure_ascii=False)})

    def _skip_rest(self, round_no: int, calls, idx: int, messages, reason: str):
        """本轮剩余工具：记为 skipped 并补齐 tool 消息。

        补齐是必须的 —— OpenAI 协议要求 assistant 里每个 tool_call 都要有对应响应，
        否则下一轮请求会因「缺少 tool 消息」直接失败。
        """
        for j, tc in enumerate(calls[idx:], start=idx + 1):
            trace.log_step(self.run_id, round_no, j, tc.name, tc.arguments,
                           "skipped", 0, f"未执行：{reason}", "E-SKIP")
            self._append_tool(messages, tc, {"tool": tc.name, "skipped": True, "reason": reason})

    def _emit_final(self, messages, content: str):
        """输出最终答案：分块推流 → 引用来源 → 落库 → done。"""
        text = (content or "").strip() or "抱歉，我这次没能给出结论，请补充更具体的商品信息。"
        for chunk in _chunks(text):
            yield ("delta", {"text": chunk})

        cites = self._citations(messages)
        if cites:
            yield ("citations", cites)

        trace.finish(self.run_id, "done", text)
        trace.append_chat_message(self.session_id, "assistant", text, cites)
        yield ("done", {"finish_reason": "stop"})

    @staticmethod
    def _citations(messages):
        """从历史里的 search_review_rules 结果提取引用（文件名 + 片段 + 摘录）。"""
        cites, seen = [], set()
        for m in messages:
            if m.get("role") != "tool" or m.get("name") != "search_review_rules":
                continue
            try:
                data = json.loads(m.get("content") or "{}")
            except Exception:
                continue
            for hit in data.get("results") or []:
                key = (hit.get("file"), hit.get("chunk_index"))
                if key in seen:
                    continue
                seen.add(key)
                cites.append({
                    "file": hit.get("file"),
                    "chunk": (hit.get("chunk_index") or 0) + 1,
                    "snippet": (hit.get("content") or "")[:80],
                    "score": round(float(hit.get("score") or 0), 2),
                })
        return cites[:5]
