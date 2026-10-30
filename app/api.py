# -*- coding: utf-8 -*-
"""全部 JSON 接口：认证、聊天(SSE)、规则库、提示词、审计、Agent 工具。"""
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
from .knowledge import delete_document, index_state, ingest_document
from .llm import build_user_content, get_active_prompt, stream_llm
from .security import create_access_token, verify_password
from .tools import DISPATCH, TOOL_SCHEMAS

router = APIRouter()


# ===================== 通用 =====================
def sse(event: str, data) -> str:
    """构造 SSE 帧。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


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
def login(body: LoginIn, response: Response, db: Session = Depends(get_db)):
    from .db import User
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

    # 检索 + 提示词
    contexts = index_state.retrieve(question, top_k=settings.TOP_K)
    system_prompt = get_active_prompt(db)
    user_content = build_user_content(question, contexts)

    async def gen():
        yield sse("meta", {"session_id": session_id})
        yield sse("status", {"stage": "retrieving", "text": "正在匹配审核规则…"})
        full = ""
        try:
            async for delta in stream_llm(system_prompt, history, user_content):
                full += delta
                yield sse("delta", {"text": delta})
        except Exception:
            yield sse("error", {"code": "50002", "message": "回答生成中断，请重试"})
            return
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

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
               "Connection": "keep-alive"}
    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


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


@router.post("/api/agent/tools/{name}")
def agent_invoke(name: str, body: dict, user=Depends(require_role("submitter", "admin"))):
    fn = DISPATCH.get(name)
    if not fn:
        raise ApiError("40401", "工具不存在", 404)
    try:
        return {"ok": True, "result": fn(**body)}
    except TypeError as e:
        raise ApiError("40001", f"工具参数错误：{e}", 400)
