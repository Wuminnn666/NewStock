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


@app.on_event("startup")
def _startup():
    init_db()
    seed_basic()
    _seed_knowledge()


app.include_router(api_router)
app.include_router(pages_router)
