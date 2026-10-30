# 商品上架与资料审核系统

基于 **FastAPI + SQLite + FAISS** 的商品上架资料审核系统，含：提交方咨询（SSE 流式问答 + RAG 引用）、规则知识库管理、提示词模板管理、对话审计，以及带服务端门禁的 Agent 工具层。

## 快速开始

```bash
cd E:/Wuminnn/NewStock
pip install -r requirements.txt
python run.py
```

打开 http://127.0.0.1:8000 ，使用演示账号登录：

| 用户名 | 密码 | 角色 |
|---|---|---|
| submitter | 123456 | 提交方 |
| reviewer | 123456 | 审核员 |
| admin | 123456 | 管理员 |

首次启动会自动建表、初始化账号与默认提示词，并导入根目录 `审核制度.md` 构建向量索引。

## 接入真实大模型（可选）

默认无 Key 时聊天走「离线演示」模式（不调用模型，仅验证检索与流式链路）。配置环境变量即可接入任意 OpenAI 兼容供应商：

```bash
# Linux / macOS
LLM_API_KEY=sk-xxx LLM_BASE_URL=https://api.deepseek.com/v1 LLM_MODEL=deepseek-chat python run.py

# Windows PowerShell
$env:LLM_API_KEY="sk-xxx"; $env:LLM_BASE_URL="https://api.deepseek.com/v1"; $env:LLM_MODEL="deepseek-chat"; python run.py
```

Embedding 默认复用 LLM 的 Key/BaseURL；也可单独配置 `EMB_API_KEY / EMB_BASE_URL / EMB_MODEL`。未配置时使用本地哈希词袋向量（仅演示，非真实语义）。

## 目录结构

```
NewStock/
├── run.py                    # 启动入口
├── requirements.txt
├── 审核制度.md               # 示例知识库（首次启动自动导入）
├── product_review_stage1.py  # 阶段1 离线原型（保留）
└── app/
    ├── main.py               # FastAPI 装配、异常处理、启动初始化
    ├── config.py             # 全部配置（环境变量可覆盖）
    ├── db.py                 # SQLite 模型 + 初始化 + 种子数据
    ├── security.py           # PBKDF2 密码哈希 + JWT
    ├── deps.py               # 数据库会话 / 当前用户 / 角色校验
    ├── knowledge.py          # 解析、切片、Embedding、BM25+FAISS 检索
    ├── llm.py                # LLM 网关（可配置、流式、提示词动态生效）
    ├── tools.py              # Agent 工具规范 + 顺序硬门禁
    ├── api.py                # 全部 JSON 接口
    ├── pages.py              # SSR 页面路由
    ├── templates/            # Jinja2 页面
    └── static/               # app.css / chat.js
```

## 主要接口

- `POST /api/auth/login` — 登录（下发 HttpOnly JWT Cookie）
- `POST /api/chat` — 聊天，返回 SSE（`meta/status/delta/citations/done/error`）
- `GET|POST|DELETE /api/admin/rules*` — 规则库上传/列表/删除/切片
- `GET|PUT /api/admin/prompts*` — 提示词查询/修改（版本+1 即时生效）
- `GET|POST /api/admin/audit*` — 对话审计与消息级标记
- `GET /api/agent/tools`、`POST /api/agent/tools/{name}` — Agent 工具规范与调用

## 权限

| 模块 | submitter | reviewer | admin |
|---|---|---|---|
| 聊天 | 是 | 否 | 是 |
| 规则库 | 否 | 否 | 是 |
| 提示词 | 否 | 否 | 是 |
| 审计 | 否 | 是 | 是 |

## Agent 顺序约束

`material_validate → qualification_audit → compliance_check → create_goods_draft`，任一前置未通过时后续工具直接抛 `E-STATE-001/002/003`，模型无法跳过校验直接建草稿。

## 说明

- 向量检索为进程内 FAISS，服务重启后从 SQLite 重建。
- PDF 使用 `pypdf` 提取文本，扫描件/图片 PDF 不做 OCR。
- 删除规则文档会删除其切片并全量重建索引。
