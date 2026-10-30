# -*- coding: utf-8 -*-
"""全部 JSON 接口：认证、聊天(SSE)、规则库、提示词、审计、Agent 工具。"""
import asyncio
import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .config import settings
from .db import (
    ChatMessage, ChatSession, KnowledgeChunk, PromptTemplate, RuleDoc, SessionLocal,
)
from .deps import get_db, get_current_user, require_role
from .errors import ApiError
from .guard import (
    REFUSE_EXTRACT, REFUSE_INJECT_NOTE, acquire_sse, charge_usage, check_quota,
    conn_snapshot, detect_injection,
    enforce_rate, estimate_tokens, is_prompt_extraction, llm_slot,
    rate_snapshot, record_security_event, strip_injection, usage_today,
)
from .knowledge import analyze_support, delete_document, index_state, ingest_document
from .llm import build_user_content, get_active_prompt, stream_llm
from .security import create_access_token, verify_password
from .tools import DISPATCH, TOOL_SCHEMAS

router = APIRouter()


# ===================== 通用 =====================
def sse(event: str, data) -> str:
    """构造 SSE 帧。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def sse_response(agen):
    """所有流式接口的统一出口。

    两件事必须在这里做，散到各个路由就一定会漏：
    1) 进流之前先占一个连接名额（对应上线清单 B4），占不到就直接抛 429，
       让用户看到「稍后再试」，而不是把连接挂着等 FD 耗尽；
    2) 无论正常结束、异常、还是客户端断开（GeneratorExit），都保证归还名额，
       否则跑几天后名额泄漏干净，服务表现为「谁都进不来」。
    """
    ticket = acquire_sse()
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
               "Connection": "keep-alive"}

    async def guarded():
        try:
            async for chunk in agen:
                yield chunk
        finally:
            ticket.release()

    return StreamingResponse(guarded(), media_type="text/event-stream", headers=headers)


@router.get("/api/health")
def health():
    """运维探针：判断「服务是否还能接客」，而不是只判断「进程还在」。

    上线后扩容/限流的依据在这里：SSE 活跃连接数接近上限就该加实例，
    而不是等用户开始报 429。此接口不鉴权、不查敏感数据，可被负载均衡器高频调用。
    """
    return {
        "status": "degraded" if settings.DEGRADED_MODE else "ok",
        "degraded": settings.DEGRADED_MODE,
        "agent_enabled": settings.AGENT_ENABLED,
        "sse": conn_snapshot(),
        "rate_keys": rate_snapshot()["keys"],
        "llm_today": usage_today(),
    }


class LoginIn(BaseModel):
    username: str
    password: str

class ChatIn(BaseModel):
    session_id: str | None = None
    question: str


class PromptUpdateIn(BaseModel):
    content: str


class FlagIn(BaseModel):
    flagged: bool
    reason: str | None = None


# ===================== 认证 =====================
@router.post("/api/auth/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    from .db import User
    # 撞库防护：按「IP + 用户名」计数。只按 IP 会被代理池绕过，
    # 只按用户名会让全公司共用一个出口 IP 时互相影响，两者组合最合适。
    ip = (request.client.host if request.client else "unknown")
    enforce_rate("login", f"{ip}:{body.username}", settings.RATE_LOGIN_PER_MIN)
    user = db.query(User).filter(User.username == body.username).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise ApiError("40102", "用户名或密码错误", 401)
    token = create_access_token(user)
    response.set_cookie("access_token", token, httponly=True, samesite="lax",
                        max_age=settings.JWT_EXPIRE_MINUTES * 60)
    return {"ok": True, "user": {"id": user.id, "username": user.username,
                                 "role": user.role, "name": user.display_name}}


@router.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie("access_token")
    return {"ok": True}


@router.get("/api/auth/logout")
def logout_get():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("access_token")
    return resp


@router.get("/api/auth/me")
def me(user=Depends(get_current_user)):
    return {"id": user.id, "username": user.username, "role": user.role,
            "name": user.display_name}


# ===================== 聊天（SSE） =====================
@router.post("/api/chat")
async def chat(body: ChatIn, request: Request, db: Session = Depends(get_db),
               user=Depends(require_role("submitter", "admin"))):
    question = (body.question or "").strip()
    if not question:
        raise ApiError("40001", "请求参数不合法：question 不能为空", 400)
    if len(question) > settings.MAX_QUESTION_LEN:
        raise ApiError("40002", f"问题内容超过 {settings.MAX_QUESTION_LEN} 字上限", 400)

    # 限流必须在这里（读完参数、还没检索、没调模型）：
    # 放在后面等于「只限制了响应速度，没限制成本」
    enforce_rate("chat", str(user.id), settings.RATE_CHAT_PER_MIN)

    # 会话：不存在则新建
    session_id = body.session_id
    if session_id:
        sess = db.get(ChatSession, session_id)
        if not sess:
            raise ApiError("40003", "会话不存在或已失效", 404)
    else:
        session_id = "s_" + uuid.uuid4().hex[:12]
        sess = ChatSession(session_id=session_id, user_id=str(user.id), title=question[:20])
        db.add(sess)
        db.commit()

    # 最近 N 轮历史
    msgs = (db.query(ChatMessage).filter(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.seq.asc()).all())
    history = [{"role": m.role, "content": m.content} for m in msgs][-settings.HISTORY_ROUNDS * 2:]
    next_seq = (msgs[-1].seq + 1) if msgs else 1

    # 先落用户消息
    db.add(ChatMessage(session_id=session_id, role="user", content=question, seq=next_seq))
    db.commit()

    # ---- 安全前置判定（必须先于检索：注入前缀会把检索带偏）----
    # 1) 套取系统提示词：规则化拒答，不交给模型自由发挥
    extraction = is_prompt_extraction(question)
    # 2) 注入特征：留痕 + 剥离指令片段后再检索，结论照常给
    inject_hits = detect_injection(question)
    if extraction:
        record_security_event("prompt_extraction", user.id, session_id, question, "refused")
    elif inject_hits:
        record_security_event("injection_attempt", user.id, session_id,
                              "命中:" + ",".join(inject_hits) + " | " + question, "logged")

    # 检索用「剥掉指令片段后的真问题」；原文仍完整入库，审计不受影响
    retrieval_q = strip_injection(question) if inject_hits else question

    # 检索 + 提示词
    contexts = index_state.retrieve(retrieval_q, top_k=settings.TOP_K)
    # 依据判定：top-k 永远有返回，若条文根本答不了这个问题，就必须直说「未找到」，
    # 而不是把三段不相关文字塞给模型（模型会顺着硬编，这是最典型的幻觉来源）。
    support = analyze_support(retrieval_q, [c["content"] for c in contexts])
    system_prompt = get_active_prompt(db)
    user_content = build_user_content(retrieval_q, contexts)

    # 3) 配额：超限直接 429，不进模型、不花钱
    if not extraction and not settings.DEGRADED_MODE:
        check_quota(user.id)

    def _save_assistant(text: str, cites):
        """助手消息落库（独立 session，避免与请求生命周期耦合）。"""
        db2 = SessionLocal()
        try:
            db2.add(ChatMessage(session_id=session_id, role="assistant", content=text,
                                citations=json.dumps(cites, ensure_ascii=False),
                                seq=next_seq + 1))
            db2.commit()
        finally:
            db2.close()

    async def _stream_text(text: str):
        """把一段固定文本按小块吐出去，保持打字机效果一致。"""
        for i in range(0, len(text), 24):
            yield sse("delta", {"text": text[i:i + 24]})
            await asyncio.sleep(0.01)

    async def gen():
        yield sse("meta", {"session_id": session_id})
        yield sse("status", {"stage": "retrieving", "text": "正在匹配审核规则…"})
        full = ""

        # ---- 分支 1：套取系统提示词 → 拒答（不调模型）----
        if extraction:
            async for chunk in _stream_text(REFUSE_EXTRACT):
                yield chunk
            _save_assistant(REFUSE_EXTRACT, [])
            yield sse("done", {"finish_reason": "refused"})
            return

        # ---- 分支 2：降级模式 → 只回条文 + 引用，不调模型 ----
        # 这是模型故障/账单异常时唯一能做到「不停服」的路径
        if settings.DEGRADED_MODE:
            if contexts:
                lines = ["【系统维护中·当前为规则条文模式】以下是检索到的相关条文，"
                         "未经过模型解读：", ""]
                for i, c in enumerate(contexts[:3]):
                    snippet = (c.get("content") or "").replace("\n", " ")[:120]
                    lines.append(f"- 《{c['file']}》片段#{c['chunk_index'] + 1}：{snippet}")
                lines += ["", "如需人工解读，请稍后重试或联系审核专员。"]
            else:
                lines = ["【系统维护中·当前为规则条文模式】未检索到相关条文，请稍后重试。"]
            text = "\n".join(lines)
            async for chunk in _stream_text(text):
                yield chunk
            cites = [{"file": c["file"], "chunk": c["chunk_index"] + 1,
                      "snippet": c["content"][:80], "score": round(c["score"], 2)}
                     for c in contexts]
            if cites:
                yield sse("citations", cites)
            _save_assistant(text, cites)
            yield sse("done", {"finish_reason": "degraded"})
            return

        if not support.get("supported", True):
            # 命中不了就说命中不了，并说明判定依据（可解释，便于审计）
            text = ("规则中未找到相关条文。\n\n"
                    f"（判定依据：{support.get('reason') or '检索到的条文与问题不匹配'}）\n\n"
                    "为避免给出错误的上架标准，我不做推测。建议：\n"
                    "- 联系合规部门或资深审核确认；\n"
                    "- 由管理员在「规则知识库」补充对应制度文档后再咨询。")
            for i in range(0, len(text), 24):
                yield sse("delta", {"text": text[i:i + 24]})
                await asyncio.sleep(0.01)
            full = text
            db2 = SessionLocal()
            try:
                db2.add(ChatMessage(session_id=session_id, role="assistant", content=full,
                                    citations=json.dumps([], ensure_ascii=False),
                                    seq=next_seq + 1))
                db2.commit()
            finally:
                db2.close()
            yield sse("done", {"finish_reason": "no_grounding"})
            return
        try:
            # 并发闸门：限制同时在飞的模型请求，排队超时快速失败而不是无限等待
            async with llm_slot():
                async for delta in stream_llm(system_prompt, history, user_content):
                    full += delta
                    yield sse("delta", {"text": delta})
        except ApiError as e:
            # 排队超时等「预期内的失败」：给出明确中文，而不是笼统的生成失败
            yield sse("error", {"code": e.code, "message": e.message})
            return
        except Exception:
            yield sse("error", {"code": "50002", "message": "回答生成中断，请重试"})
            return
        # 记账（估算值）：无论成功与否只要模型被调用过就应该计成本
        try:
            charge_usage(user.id, estimate_tokens(system_prompt, user_content, full))
        except Exception as e:            # noqa: BLE001
            print("[用量记账失败]", e)
        # 命中注入特征：结论照给，但明确告诉用户「判断标准没被改变」
        if inject_hits and full:
            async for chunk in _stream_text(REFUSE_INJECT_NOTE):
                yield chunk
            full += REFUSE_INJECT_NOTE
        citations = [{
            "file": c["file"], "chunk": c["chunk_index"] + 1,
            "snippet": c["content"][:80], "score": round(c["score"], 2),
        } for c in contexts]
        if citations:
            yield sse("citations", citations)
        # 落助手消息（用独立会话，避免依赖生命周期）
        db2 = SessionLocal()
        try:
            db2.add(ChatMessage(session_id=session_id, role="assistant", content=full,
                                citations=json.dumps(citations, ensure_ascii=False),
                                seq=next_seq + 1))
            db2.commit()
        finally:
            db2.close()
        yield sse("done", {"finish_reason": "stop"})

    return sse_response(gen())


# ===================== 规则知识库 =====================
@router.get("/api/admin/rules")
def list_rules(db: Session = Depends(get_db), user=Depends(require_role("admin"))):
    docs = db.query(RuleDoc).order_by(RuleDoc.id.desc()).all()
    return [{"id": d.id, "title": d.title, "filename": d.filename, "category": d.category,
             "chunk_count": d.chunk_count, "status": d.status,
             "created_at": d.created_at.strftime("%Y-%m-%d %H:%M")} for d in docs]


@router.post("/api/admin/rules/upload")
async def upload_rule(file: UploadFile = File(...), category: str = Form("审核规范"),
                      db: Session = Depends(get_db), user=Depends(require_role("admin"))):
    filename = file.filename or "未命名.md"
    if not filename.lower().endswith((".md", ".pdf")):
        raise ApiError("41501", "不支持的文件类型（仅支持 .md / .pdf）", 415)
    raw = await file.read()
    if len(raw) > settings.UPLOAD_MAX_MB * 1024 * 1024:
        raise ApiError("41301", f"文件超过 {settings.UPLOAD_MAX_MB}MB 上限", 413)
    try:
        doc = ingest_document(db, filename, raw, category)
    except ValueError as e:
        raise ApiError("50001", f"切片/向量化失败：{e}", 500)
    index_state.build(db)  # 重建索引，立即参与检索
    return {"ok": True, "doc_id": doc.id, "chunk_count": doc.chunk_count}


@router.delete("/api/admin/rules/{doc_id}")
def remove_rule(doc_id: int, db: Session = Depends(get_db), user=Depends(require_role("admin"))):
    doc = db.get(RuleDoc, doc_id)
    if not doc:
        raise ApiError("40401", "规则文档不存在", 404)
    delete_document(db, doc_id)
    index_state.build(db)
    return {"ok": True}


@router.get("/api/admin/rules/{doc_id}/chunks")
def rule_chunks(doc_id: int, db: Session = Depends(get_db), user=Depends(require_role("admin"))):
    rows = (db.query(KnowledgeChunk).filter(KnowledgeChunk.rule_doc_id == doc_id)
            .order_by(KnowledgeChunk.chunk_index.asc()).all())
    return [{"chunk_index": r.chunk_index, "content": r.content} for r in rows]


# ===================== 提示词模板 =====================
@router.get("/api/admin/prompts")
def list_prompts(db: Session = Depends(get_db), user=Depends(require_role("admin"))):
    rows = db.query(PromptTemplate).order_by(PromptTemplate.id.asc()).all()
    return [{"id": p.id, "name": p.name, "content": p.content, "is_active": p.is_active,
             "version": p.version} for p in rows]


@router.put("/api/admin/prompts/{prompt_id}")
def update_prompt(prompt_id: int, body: PromptUpdateIn, db: Session = Depends(get_db),
                  user=Depends(require_role("admin"))):
    content = (body.content or "").strip()
    if not content:
        raise ApiError("40001", "提示词内容不能为空", 400)
    p = db.get(PromptTemplate, prompt_id)
    if not p:
        raise ApiError("40401", "提示词模板不存在", 404)
    p.content = content
    p.version += 1           # 版本+1 -> 缓存失效 -> 下一问立即生效
    p.updated_by = user.username
    db.commit()
    return {"ok": True, "version": p.version}


@router.post("/api/admin/prompts/{prompt_id}/activate")
def activate_prompt(prompt_id: int, db: Session = Depends(get_db),
                    user=Depends(require_role("admin"))):
    p = db.get(PromptTemplate, prompt_id)
    if not p:
        raise ApiError("40401", "提示词模板不存在", 404)
    db.query(PromptTemplate).update({PromptTemplate.is_active: False})
    p.is_active = True
    db.commit()
    return {"ok": True}


# ===================== 对话审计 =====================
def _audit_query(db, submitter_id, date_from, date_to, flagged):
    q = db.query(ChatSession)
    if submitter_id:
        q = q.filter(ChatSession.user_id == submitter_id)
    if date_from:
        q = q.filter(ChatSession.created_at >= datetime.fromisoformat(date_from + " 00:00:00"))
    if date_to:
        q = q.filter(ChatSession.created_at <= datetime.fromisoformat(date_to + " 23:59:59"))
    sessions = q.order_by(ChatSession.created_at.desc()).all()
    out = []
    for s in sessions:
        msgs = db.query(ChatMessage).filter(ChatMessage.session_id == s.session_id).all()
        is_flagged = any(m.flagged for m in msgs)
        if flagged == "1" and not is_flagged:
            continue
        if flagged == "0" and is_flagged:
            continue
        out.append({"session_id": s.session_id, "user_id": s.user_id, "title": s.title,
                    "message_count": len(msgs), "flagged": is_flagged,
                    "created_at": s.created_at.strftime("%Y-%m-%d %H:%M")})
    return out


@router.get("/api/admin/audit/conversations")
def audit_list(submitter_id: str = "", date_from: str = "", date_to: str = "",
               flagged: str = "", db: Session = Depends(get_db),
               user=Depends(require_role("reviewer", "admin"))):
    return _audit_query(db, submitter_id, date_from, date_to, flagged)


@router.get("/api/admin/audit/conversations/{session_id}/messages")
def audit_messages(session_id: str, db: Session = Depends(get_db),
                   user=Depends(require_role("reviewer", "admin"))):
    rows = (db.query(ChatMessage).filter(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.seq.asc()).all())
    return [{"id": m.id, "role": m.role, "content": m.content,
             "citations": m.citations, "flagged": m.flagged} for m in rows]


@router.post("/api/admin/audit/messages/{message_id}/flag")
def audit_flag(message_id: int, body: FlagIn, db: Session = Depends(get_db),
               user=Depends(require_role("reviewer", "admin"))):
    m = db.get(ChatMessage, message_id)
    if not m:
        raise ApiError("40401", "消息不存在", 404)
    m.flagged = body.flagged
    m.flag_reason = body.reason
    m.flagged_by = user.username
    m.flagged_at = datetime.utcnow() if body.flagged else None
    db.commit()
    return {"ok": True}


# ===================== Agent 工具 =====================
@router.get("/api/agent/tools")
def agent_tools(user=Depends(get_current_user)):
    return {"tools": TOOL_SCHEMAS}


# 只读工具白名单：这两个没有副作用，随便直调都不影响数据。
# 其余（material_validate / qualification_audit / compliance_check / create_goods_draft）
# 一旦允许直调，就等于给了一条绕过 Agent 编排的旁路：
# 实测过「先直调三个校验拿 token，再直调 create_goods_draft」，
# 草稿会在没有任何人工确认的情况下被创建出来，
# 而「建草稿前强制中断确认」是这一层的核心约束，绝不能有旁路。
TOOLS_DIRECT_ALLOWED = {"search_review_rules", "get_category_requirements"}


@router.post("/api/agent/tools/{name}")
def agent_invoke(name: str, body: dict, user=Depends(require_role("submitter", "admin"))):
    fn = DISPATCH.get(name)
    if not fn:
        raise ApiError("40401", "工具不存在", 404)
    if name not in TOOLS_DIRECT_ALLOWED:
        # 统一引导到正式入口，不暴露「你差一步就能绕过」的细节
        raise ApiError("40302",
                       "该操作需通过智能体流程执行（含校验与人工确认），不支持直接调用",
                       403)
    try:
        return {"ok": True, "result": fn(**body)}
    except TypeError as e:
        raise ApiError("40001", f"工具参数错误：{e}", 400)


# ===================== Agent 编排（执行循环 / 人工确认 / 轨迹查询） =====================
class AgentRunIn(BaseModel):
    session_id: str | None = None
    question: str


class AgentConfirmIn(BaseModel):
    confirm_id: str
    approved: bool


def _ensure_session(db: Session, user, session_id: str | None, question: str) -> str:
    """复用会话：传了就校验存在性，没传就新建（与 /api/chat 行为一致）。"""
    if session_id:
        if not db.get(ChatSession, session_id):
            raise ApiError("40003", "会话不存在或已失效", 404)
        return session_id
    sid = "s_" + uuid.uuid4().hex[:12]
    db.add(ChatSession(session_id=sid, user_id=str(user.id), title=question[:20]))
    db.commit()
    return sid


@router.post("/api/agent/run")
async def agent_run(body: AgentRunIn, db: Session = Depends(get_db),
                    user=Depends(require_role("submitter", "admin"))):
    """启动一次智能体执行，SSE 推送「决策 → 工具 → 结果」全过程。

    事件序列：
      meta → thinking → tool_call → tool_result …（循环）
      → delta（最终答案分块）→ citations → done
      若遇到 create_goods_draft：draft_start → draft_delta… → confirm_required（挂起）
    """
    from .agent import AgentHarness

    question = (body.question or "").strip()
    if not question:
        raise ApiError("40001", "请求参数不合法：question 不能为空", 400)
    if len(question) > settings.MAX_QUESTION_LEN:
        raise ApiError("40002", f"问题内容超过 {settings.MAX_QUESTION_LEN} 字上限", 400)

    # 降级开关：智能体编排涉及多轮决策，成本与风险都高于普通问答，
    # 出问题时第一个被摘掉的就是它（而不是整站停服）
    if not settings.AGENT_ENABLED:
        raise ApiError("50302", "智能审核功能维护中，可先使用规则条文问答或稍后重试", 503)
    enforce_rate("agent", str(user.id), settings.RATE_AGENT_PER_MIN)
    # 注入特征留痕（不阻断：越权由服务端工具门禁兜底，这里只负责可追溯）
    _hits = detect_injection(question)
    if _hits:
        record_security_event("injection_attempt", user.id, body.session_id or "",
                              "agent | 命中:" + ",".join(_hits) + " | " + question, "logged")

    session_id = _ensure_session(db, user, body.session_id, question)
    harness = AgentHarness(session_id=session_id, user_id=str(user.id))
    return sse_response(harness.astream(question))


@router.post("/api/agent/confirm")
async def agent_confirm(body: AgentConfirmIn, db: Session = Depends(get_db),
                        user=Depends(require_role("submitter", "admin"))):
    """人工确认草稿：同意则真正创建（续跑循环出结论），拒绝则取消。

    同样返回 SSE，前端沿用同一套解析逻辑即可。
    """
    from .agent import AgentHarness
    from .agent import trace as agent_trace

    row = agent_trace.get_confirmation(body.confirm_id)
    if not row:
        raise ApiError("40401", "确认单不存在或已失效", 404)

    harness = AgentHarness(session_id=row.session_id, user_id=str(user.id))
    harness.load_pending(body.confirm_id)   # 提前校验，错误才能以 JSON 返回而不是断流
    return sse_response(
        harness.astream_confirm(body.confirm_id, body.approved, user.username))


@router.get("/api/agent/runs")
def agent_runs(limit: int = 50, db: Session = Depends(get_db),
               user=Depends(require_role("reviewer", "admin"))):
    """执行记录列表（阶段 5 分析入口）。"""
    from .agent import trace as agent_trace
    rows = agent_trace.list_runs(limit=limit)
    return [{
        "run_id": r.run_id, "session_id": r.session_id, "user_id": r.user_id,
        "question": r.question, "status": r.status, "rounds": r.rounds,
        "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        "final_answer": (r.final_answer or "")[:200], "error": r.error,
    } for r in rows]


@router.get("/api/agent/runs/{run_id}/steps")
def agent_run_steps(run_id: str, db: Session = Depends(get_db),
                    user=Depends(require_role("reviewer", "admin"))):
    """单次执行的逐轮明细：工具名 / 参数 / 耗时 / 结果摘要（阶段 5 的核心数据）。"""
    from .agent import trace as agent_trace
    steps = agent_trace.list_steps(run_id)
    return [{
        "round": s.round_no, "step": s.step_no, "tool": s.tool_name,
        "arguments": s.arguments, "status": s.status, "elapsed_ms": s.elapsed_ms,
        "summary": s.result_summary, "error_code": s.error_code,
        "created_at": s.created_at.strftime("%H:%M:%S"),
    } for s in steps]


# ===================== 运营观测（用量 / 安全事件） =====================
@router.get("/api/admin/usage")
def admin_usage(user=Depends(require_role("admin"))):
    """当日模型用量、限流键规模与关键阈值（上线后每天的巡检入口）。"""
    return {
        "usage": usage_today(),
        "rate": rate_snapshot(),
        "limits": {
            "chat_per_min": settings.RATE_CHAT_PER_MIN,
            "agent_per_min": settings.RATE_AGENT_PER_MIN,
            "login_per_min": settings.RATE_LOGIN_PER_MIN,
            "tokens_per_user": settings.LLM_DAILY_TOKENS_PER_USER,
            "tokens_global": settings.LLM_DAILY_TOKENS_GLOBAL,
            "llm_concurrency": settings.LLM_MAX_CONCURRENCY,
        },
        "switches": {"degraded": settings.DEGRADED_MODE, "agent_enabled": settings.AGENT_ENABLED},
    }


@router.get("/api/admin/security-events")
def security_events(limit: int = 50, event_type: str = "",
                    db: Session = Depends(get_db), user=Depends(require_role("admin"))):
    """安全事件列表：注入尝试、套取提示词等。

    detail 里可能含用户原话，返回前统一脱敏——安全页也不该成为
    手机号/证件的二次泄露面。
    """
    from .db import SecurityEvent
    from .guard import mask_text
    q = db.query(SecurityEvent)
    if event_type:
        q = q.filter(SecurityEvent.event_type == event_type)
    rows = q.order_by(SecurityEvent.id.desc()).limit(min(limit, 200)).all()
    return [{
        "id": r.id, "type": r.event_type, "user_id": r.user_id,
        "session_id": r.session_id, "detail": mask_text(r.detail or ""),
        "action": r.action_taken,
        "at": r.created_at.strftime("%Y-%m-%d %H:%M:%S"),
    } for r in rows]
