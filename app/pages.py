# -*- coding: utf-8 -*-
"""服务端渲染页面路由（Jinja2）。"""
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .api import _audit_query
from .config import ROLE_NAME
from .db import PromptTemplate, RuleDoc, User
from .deps import get_db
from .security import decode_token

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter()


def _page_user(request: Request, db: Session):
    """页面鉴权：读取 Cookie 中的 JWT，失败返回 None。"""
    token = request.cookies.get("access_token")
    if not token:
        return None
    payload = decode_token(token)
    if not payload:
        return None
    return db.get(User, payload.get("uid"))


def _ctx(request, user, active, page_title, **extra):
    ctx = {"request": request, "user": user, "active": active,
           "page_title": page_title, "role_name": ROLE_NAME.get(user.role, user.role)}
    ctx.update(extra)
    return ctx


@router.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)):
    user = _page_user(request, db)
    if user:
        return RedirectResponse("/")
    return templates.TemplateResponse(request, "login.html", {"request": request})


@router.get("/")
def home(request: Request, db: Session = Depends(get_db)):
    user = _page_user(request, db)
    if not user:
        return RedirectResponse("/login")
    if user.role == "submitter":
        return RedirectResponse("/chat")
    if user.role == "reviewer":
        return RedirectResponse("/admin/audit")
    return RedirectResponse("/admin/rules")


@router.get("/chat")
def chat_page(request: Request, db: Session = Depends(get_db)):
    user = _page_user(request, db)
    if not user:
        return RedirectResponse("/login")
    if user.role not in ("submitter", "admin"):
        return RedirectResponse("/403")
    return templates.TemplateResponse(request, "chat.html", _ctx(request, user, "chat", "提交方咨询"))


@router.get("/admin/rules")
def rules_page(request: Request, db: Session = Depends(get_db)):
    user = _page_user(request, db)
    if not user:
        return RedirectResponse("/login")
    if user.role != "admin":
        return RedirectResponse("/403")
    docs = db.query(RuleDoc).order_by(RuleDoc.id.desc()).all()
    return templates.TemplateResponse(request, "rules.html", _ctx(request, user, "rules", "规则知识库", docs=docs))


@router.get("/admin/prompts")
def prompts_page(request: Request, db: Session = Depends(get_db)):
    user = _page_user(request, db)
    if not user:
        return RedirectResponse("/login")
    if user.role != "admin":
        return RedirectResponse("/403")
    prompt = db.query(PromptTemplate).filter(PromptTemplate.is_active == True).first()  # noqa: E712
    return templates.TemplateResponse(request, "prompts.html", _ctx(request, user, "prompts", "提示词模板", prompt=prompt))


@router.get("/admin/audit")
def audit_page(request: Request, db: Session = Depends(get_db)):
    user = _page_user(request, db)
    if not user:
        return RedirectResponse("/login")
    if user.role not in ("reviewer", "admin"):
        return RedirectResponse("/403")
    qp = request.query_params
    rows = _audit_query(db, qp.get("submitter_id", ""), qp.get("date_from", ""),
                        qp.get("date_to", ""), qp.get("flagged", ""))
    return templates.TemplateResponse(request, "audit.html", _ctx(
        request, user, "audit", "对话审计", rows=rows,
        users=db.query(User).all(),
        f_submitter=qp.get("submitter_id", ""), f_from=qp.get("date_from", ""),
        f_to=qp.get("date_to", ""), f_flagged=qp.get("flagged", "")))


@router.get("/admin/agent")
def agent_page(request: Request, db: Session = Depends(get_db)):
    """执行轨迹页：智能体每次执行的轮次与工具明细（阶段 5 分析入口）。"""
    from .agent import trace as agent_trace

    user = _page_user(request, db)
    if not user:
        return RedirectResponse("/login")
    if user.role not in ("reviewer", "admin"):
        return RedirectResponse("/403")
    runs = agent_trace.list_runs(limit=100)
    return templates.TemplateResponse(request, "agent.html",
                                      _ctx(request, user, "agent", "执行轨迹", runs=runs))


@router.get("/403")
def forbidden(request: Request, db: Session = Depends(get_db)):
    user = _page_user(request, db)
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "403.html", _ctx(request, user, "", "无权限"))
