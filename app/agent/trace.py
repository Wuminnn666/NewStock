# -*- coding: utf-8 -*-
"""执行轨迹持久化（供阶段 5 分析）+ 会话消息落库。

设计原则：
  · 每一次工具调用都落一条 agent_steps，无论成功、失败、被门禁拦截还是被跳过；
  · agent_runs.messages 存「决策上下文快照」，中断（人工确认）后靠它原样续跑；
  · 所有写库都用独立的短会话，避免和 HTTP 请求的 Session 生命周期互相拖累。
"""
import json
import uuid
from datetime import datetime

from ..db import (
    AgentConfirmation, AgentRun, AgentStep, ChatMessage, ChatSession, SessionLocal,
)


# ---------------------------------------------------------------------------
# 会话消息（让审计页能照样看到 Agent 对话）
# ---------------------------------------------------------------------------
def ensure_chat_session(session_id: str, user_id: str, title: str) -> None:
    db = SessionLocal()
    try:
        if not db.get(ChatSession, session_id):
            db.add(ChatSession(session_id=session_id, user_id=user_id, title=title[:20]))
            db.commit()
    finally:
        db.close()


def append_chat_message(session_id: str, role: str, content: str, citations=None) -> None:
    db = SessionLocal()
    try:
        last = (db.query(ChatMessage).filter(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.seq.desc()).first())
        seq = (last.seq + 1) if last else 1
        db.add(ChatMessage(session_id=session_id, role=role, content=content,
                           citations=json.dumps(citations, ensure_ascii=False) if citations else None,
                           seq=seq))
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 执行记录
# ---------------------------------------------------------------------------
def new_run(session_id: str, user_id: str, question: str) -> str:
    """创建一次执行记录，返回 run_id。"""
    run_id = "r_" + uuid.uuid4().hex[:12]
    db = SessionLocal()
    try:
        db.add(AgentRun(run_id=run_id, session_id=session_id, user_id=user_id,
                        question=question, status="running", rounds=0))
        db.commit()
    finally:
        db.close()
    return run_id


def save_context(run_id: str, messages, rounds: int, status: str = None,
                 pending_call=None, final_answer: str = None) -> None:
    """保存决策上下文快照 + 当前状态（中断续跑的关键）。"""
    db = SessionLocal()
    try:
        run = db.query(AgentRun).filter(AgentRun.run_id == run_id).first()
        if not run:
            return
        run.messages = json.dumps(messages, ensure_ascii=False)
        run.rounds = rounds
        if status:
            run.status = status
        if pending_call is not None:
            run.pending_call = json.dumps(pending_call, ensure_ascii=False)
        if final_answer is not None:
            run.final_answer = final_answer
        db.commit()
    finally:
        db.close()


def log_step(run_id: str, round_no: int, step_no: int, tool_name: str, arguments: dict,
             status: str, elapsed_ms: int = 0, result_summary: str = "",
             error_code: str = None) -> None:
    """记录一次工具调用明细。

    status 取值：
      ok             执行成功
      error          工具抛错
      gate_blocked   被服务端门禁拦截（越序/令牌无效）
      skipped        同轮内因前置失败而未执行
      confirm_pending 已中断，等待人工确认（此时工具尚未真正执行）
    """
    db = SessionLocal()
    try:
        db.add(AgentStep(
            run_id=run_id, round_no=round_no, step_no=step_no, tool_name=tool_name,
            arguments=json.dumps(arguments, ensure_ascii=False)[:2000],
            status=status, elapsed_ms=elapsed_ms,
            result_summary=(result_summary or "")[:500], error_code=error_code,
        ))
        db.commit()
    finally:
        db.close()


def finish(run_id: str, status: str, final_answer: str = None, error: str = None) -> None:
    db = SessionLocal()
    try:
        run = db.query(AgentRun).filter(AgentRun.run_id == run_id).first()
        if not run:
            return
        run.status = status
        if final_answer is not None:
            run.final_answer = final_answer
        if error is not None:
            run.error = error[:1000]
        db.commit()
    finally:
        db.close()


def get_run(run_id: str):
    db = SessionLocal()
    try:
        return db.query(AgentRun).filter(AgentRun.run_id == run_id).first()
    finally:
        db.close()


def list_runs(limit: int = 100):
    db = SessionLocal()
    try:
        return (db.query(AgentRun).order_by(AgentRun.id.desc()).limit(limit).all())
    finally:
        db.close()


def list_steps(run_id: str):
    db = SessionLocal()
    try:
        return (db.query(AgentStep).filter(AgentStep.run_id == run_id)
                .order_by(AgentStep.round_no.asc(), AgentStep.step_no.asc()).all())
    finally:
        db.close()


def next_step_no(run_id: str, round_no: int) -> int:
    """同一轮内的下一个序号（中断续跑时，接着原来那轮的序号往下排）。"""
    from sqlalchemy import func
    db = SessionLocal()
    try:
        cur = (db.query(func.max(AgentStep.step_no))
               .filter(AgentStep.run_id == run_id, AgentStep.round_no == round_no).scalar())
        return (cur or 0) + 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 人工确认单
# ---------------------------------------------------------------------------
def create_confirmation(run_id: str, session_id: str, draft: dict,
                        payload: dict, token: str) -> str:
    """create_goods_draft 执行前生成确认单（状态 pending）。"""
    confirm_id = "c_" + uuid.uuid4().hex[:12]
    db = SessionLocal()
    try:
        db.add(AgentConfirmation(
            confirm_id=confirm_id, run_id=run_id, session_id=session_id,
            draft=json.dumps(draft, ensure_ascii=False),
            payload=json.dumps(payload, ensure_ascii=False),
            precheck_token=token, status="pending",
        ))
        db.commit()
    finally:
        db.close()
    return confirm_id


def get_confirmation(confirm_id: str):
    db = SessionLocal()
    try:
        return (db.query(AgentConfirmation)
                .filter(AgentConfirmation.confirm_id == confirm_id).first())
    finally:
        db.close()


def decide_confirmation(confirm_id: str, approved: bool, decided_by: str) -> None:
    db = SessionLocal()
    try:
        row = (db.query(AgentConfirmation)
               .filter(AgentConfirmation.confirm_id == confirm_id).first())
        if not row:
            return
        row.status = "confirmed" if approved else "rejected"
        row.decided_by = decided_by
        row.decided_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()


def expire_confirmation(confirm_id: str) -> None:
    db = SessionLocal()
    try:
        row = (db.query(AgentConfirmation)
               .filter(AgentConfirmation.confirm_id == confirm_id).first())
        if row:
            row.status = "expired"
            db.commit()
    finally:
        db.close()
