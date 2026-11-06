# -*- coding: utf-8 -*-
"""启动入口：python run.py

本地开发默认 127.0.0.1:8000；云端/容器部署时通过环境变量注入：
    PORT   监听端口（部署平台约定注入，缺省 8000）
    HOST   绑定地址（缺省 127.0.0.1；部署平台要求 0.0.0.0 时会自动生效）
"""
import os

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    # 显式设置 HOST 优先；否则当检测到部署平台注入的 PORT 时绑定 0.0.0.0
    host = os.environ.get("HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    uvicorn.run("app.main:app", host=host, port=port, reload=False)
