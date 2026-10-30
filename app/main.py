# -*- coding: utf-8 -*-
"""FastAPI 应用入口：装配路由、静态资源、异常处理与启动初始化。"""
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import router as api_router
from .config import settings
from .db import SessionLocal, init_db, seed_basic
from .errors import ApiError
from .pages import router as pages_router

app = FastAPI(title=settings.APP_NAME)

BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    """统一错误信封：{code, message, request_id}。"""
    return JSONResponse(
        status_code=exc.status,
        content={"code": exc.code, "message": exc.message,
                 "request_id": uuid.uuid4().hex[:12]},
    )


def _seed_knowledge():
    """首次启动时导入示例制度并构建索引。"""
    from .db import RuleDoc
    from .knowledge import index_state, ingest_document
    db = SessionLocal()
    try:
        if db.query(RuleDoc).count() == 0:
            p = Path("审核制度.md")
            if p.exists():
                try:
                    ingest_document(db, "审核制度.md", p.read_bytes(), "审核规范",
                                    title="商品上架审核制度（示例）")
                except Exception as e:  # 不阻断启动
                    print("示例知识库导入失败：", e)
        index_state.build(db)
    finally:
        db.close()


def _check_production_safety():
    """上线前自检：不安全的默认配置直接拒绝启动，而不是打条日志了事。

    为什么必须 fail-fast：SECRET_KEY 决定 JWT 签名。默认值写在代码里、
    且已经进过 Git 仓库，等于任何人都能自己签一张 role=admin 的令牌。
    这种问题不会在功能测试里暴露，只会在被别人登进来那天暴露。
    日志警告没人看，启动失败才会被人处理。
    """
    if settings.ENV.lower() not in ("prod", "production"):
        print(f"[安全检查] 当前 ENV={settings.ENV}，跳过生产校验（仅本地开发可接受）")
        return

    problems = []
    if settings.SECRET_KEY == "dev-secret-please-change-in-prod":
        problems.append("SECRET_KEY 仍是代码里的默认值")
    if len(settings.SECRET_KEY) < 32:
        problems.append(f"SECRET_KEY 长度仅 {len(settings.SECRET_KEY)}，要求 ≥32")
    if problems:
        raise RuntimeError(
            "生产环境配置不安全，已拒绝启动：\n  - " + "\n  - ".join(problems)
            + "\n请设置环境变量 SECRET_KEY（可用 `python -c \"import secrets;print(secrets.token_urlsafe(48))\"` 生成）"
        )
    print("[安全检查] 生产配置校验通过")


@app.on_event("startup")
def _startup():
    _check_production_safety()
    init_db()
    seed_basic()
    _seed_knowledge()


app.include_router(api_router)
app.include_router(pages_router)
