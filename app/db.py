# -*- coding: utf-8 -*-
"""SQLite 数据模型与初始化（沿用阶段 1 的规则/切片结构，并新增会话/消息/提示词表）。"""
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, LargeBinary, String, Text,
    create_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings
from .security import hash_password


def now():
    return datetime.utcnow()


engine = create_engine(
    settings.DB_URL,
    # check_same_thread=False：SQLite 默认禁止跨线程复用连接，而 FastAPI 的
    # 同步依赖会在线程池里执行，必须放开。
    # timeout=30：拿不到写锁时最多等 30 秒再报错，而不是立刻抛
    # "database is locked"——SSE 长连接 + 后台任务并发写的时候这个参数很关键。
    connect_args={"check_same_thread": False, "timeout": 30},
)


# 开启 WAL：读不阻塞写、写不阻塞读，是 SQLite 支撑「边聊边审计」的前提。
# 只在 SQLite 下执行，换 PostgreSQL 时自动跳过。
if settings.DB_URL.startswith("sqlite"):
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _sqlite_pragma(dbapi_conn, _rec):        # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")     # 并发读写下显著减少锁冲突
        cur.execute("PRAGMA synchronous=NORMAL")   # WAL 下够安全，且快很多
        cur.execute("PRAGMA busy_timeout=30000")   # 30s，与上面的 timeout 对齐
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, index=True, nullable=False)
    password_hash = Column(String(256), nullable=False)
    role = Column(String(16), nullable=False)  # submitter/reviewer/admin
    display_name = Column(String(64))
    status = Column(String(16), default="active")
    created_at = Column(DateTime, default=now)


class RuleDoc(Base):
    __tablename__ = "rule_docs"
    id = Column(Integer, primary_key=True)
    title = Column(String(200))
    filename = Column(String(200), nullable=False)
    category = Column(String(32), default="审核规范")
    chunk_count = Column(Integer, default=0)
    status = Column(String(16), default="已生效")
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    id = Column(Integer, primary_key=True)
    rule_doc_id = Column(Integer, ForeignKey("rule_docs.id"), index=True)
    chunk_index = Column(Integer, default=0)
    content = Column(Text, nullable=False)
    embedding = Column(LargeBinary)          # float32 字节
    meta = Column(Text)                       # {"file":..., "chunk_no":...}
    created_at = Column(DateTime, default=now)


class PromptTemplate(Base):
    __tablename__ = "prompt_templates"
    id = Column(Integer, primary_key=True)
    name = Column(String(64), default="默认模板")
    content = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True)
    version = Column(Integer, default=1)
    updated_by = Column(String(64))
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    session_id = Column(String(64), primary_key=True)
    user_id = Column(String(64), index=True)
    title = Column(String(100))
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True)
    session_id = Column(String(64), ForeignKey("chat_sessions.session_id"), index=True)
    role = Column(String(16))                 # user/assistant/system
    content = Column(Text, default="")
    citations = Column(Text)                  # JSON 数组字符串
    seq = Column(Integer, default=0)          # 会话内顺序
    flagged = Column(Boolean, default=False)  # 审计：疑似违规
    flag_reason = Column(Text)
    flagged_by = Column(String(64))
    flagged_at = Column(DateTime)
    created_at = Column(DateTime, default=now)


# ---------------------------------------------------------------------------
# Agent 编排层数据表
# 三张表分工：
#   agent_runs         一次「智能体执行」的总体记录（含决策上下文快照，支持中断后续跑）
#   agent_steps        每一轮里每一次工具调用的明细（阶段 5 分析的核心数据源）
#   agent_confirmations 人工确认单（create_goods_draft 执行前的强制中断凭证）
# ---------------------------------------------------------------------------
class AgentRun(Base):
    __tablename__ = "agent_runs"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), unique=True, index=True)   # 对外暴露的执行 ID
    session_id = Column(String(64), index=True)            # 关联聊天会话
    user_id = Column(String(64), index=True)               # 触发人
    question = Column(Text)                                # 原始提问
    status = Column(String(24), default="running")         # running/waiting_confirm/done/failed/aborted
    rounds = Column(Integer, default=0)                    # 已消耗的决策轮次
    messages = Column(Text)                                # 决策上下文快照(JSON)，中断后靠它续跑
    pending_call = Column(Text)                            # 挂起中的工具调用(JSON)
    final_answer = Column(Text)                            # 最终回答
    error = Column(Text)                                   # 失败原因
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)


class AgentStep(Base):
    __tablename__ = "agent_steps"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), index=True)
    round_no = Column(Integer, default=0)      # 第几轮决策
    step_no = Column(Integer, default=0)       # 该轮内第几个工具调用
    tool_name = Column(String(64))
    arguments = Column(Text)                   # 入参(JSON)
    status = Column(String(16))                # ok/error/gate_blocked/skipped/confirm_pending
    elapsed_ms = Column(Integer, default=0)    # 耗时（阶段 5 做性能分析）
    result_summary = Column(String(500))       # 结果摘要（截断，便于看板展示）
    error_code = Column(String(32))
    created_at = Column(DateTime, default=now)


class AgentConfirmation(Base):
    __tablename__ = "agent_confirmations"
    id = Column(Integer, primary_key=True)
    confirm_id = Column(String(64), unique=True, index=True)
    run_id = Column(String(64), index=True)
    session_id = Column(String(64), index=True)
    draft = Column(Text)                       # 展示给提交方的草稿内容(JSON)
    payload = Column(Text)                     # 待创建的商品 payload(JSON，确认后原样使用)
    precheck_token = Column(String(128))       # 预检令牌
    status = Column(String(16), default="pending")  # pending/confirmed/rejected/expired
    decided_by = Column(String(64))
    decided_at = Column(DateTime)
    created_at = Column(DateTime, default=now)


class LlmUsage(Base):
    """模型调用用量：按「账号 + 日期」累计，用于日配额控制。

    为什么落库而不是放内存：配额的意义就是「跨进程、跨重启都算数」。
    放内存的话，重启一次配额就清零，等于没有。
    """
    __tablename__ = "llm_usage"
    id = Column(Integer, primary_key=True)
    user_id = Column(String(64), index=True)
    day = Column(String(10), index=True)       # YYYY-MM-DD（本地日）
    calls = Column(Integer, default=0)
    tokens = Column(Integer, default=0)        # 估算值，用于防失控
    updated_at = Column(DateTime, default=now, onupdate=now)


class SecurityEvent(Base):
    """安全事件留痕：提示词注入、套取系统提示、越权尝试等。

    单独一张表而不是塞进业务日志，是因为它的留存期、访问权限、
    告警规则都跟业务日志不一样（安全事件要留更久、只有安全角色能看）。
    """
    __tablename__ = "security_events"
    id = Column(Integer, primary_key=True)
    event_type = Column(String(32), index=True)   # injection_attempt/prompt_extraction/...
    user_id = Column(String(64), index=True)
    session_id = Column(String(64))
    detail = Column(String(500))                  # 命中片段（截断，避免把全文塞进安全表）
    action_taken = Column(String(64))             # refused/logged/degraded
    created_at = Column(DateTime, default=now, index=True)


def init_db():
    Base.metadata.create_all(engine)


def seed_basic():
    """初始化三个角色账号与默认提示词（仅当为空时）。"""
    db = SessionLocal()
    try:
        if db.query(User).count() == 0:
            db.add_all([
                User(username="submitter", password_hash=hash_password("123456"),
                     role="submitter", display_name="张三（提交方）"),
                User(username="reviewer", password_hash=hash_password("123456"),
                     role="reviewer", display_name="李四（审核员）"),
                User(username="admin", password_hash=hash_password("123456"),
                     role="admin", display_name="王五（管理员）"),
            ])
        if db.query(PromptTemplate).count() == 0:
            db.add(PromptTemplate(name="默认模板", content=settings.DEFAULT_PROMPT,
                                  is_active=True, version=1))
        db.commit()
    finally:
        db.close()
