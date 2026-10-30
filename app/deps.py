# -*- coding: utf-8 -*-
"""FastAPI 依赖：数据库会话、当前用户、接口级角色校验。"""
from fastapi import Depends, Request
from sqlalchemy.orm import Session

from .db import SessionLocal, User
from .errors import ApiError
from .security import decode_token


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _extract_token(request: Request) -> str | None:
    """优先取 Authorization Bearer，其次取 HttpOnly Cookie。"""
    auth = request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:]
    return request.cookies.get("access_token")


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = _extract_token(request)
    if not token:
        raise ApiError("40101", "登录状态已失效，请重新登录", 401)
    payload = decode_token(token)
    if not payload:
        raise ApiError("40101", "登录状态已失效，请重新登录", 401)
    user = db.get(User, payload.get("uid"))
    if not user or user.status != "active":
        raise ApiError("40101", "登录状态已失效，请重新登录", 401)
    return user


def require_role(*roles: str):
    """接口级权限：返回一个校验当前用户角色的依赖。"""
    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise ApiError("40301", "无权访问该功能", 403)
        return user
    return checker
