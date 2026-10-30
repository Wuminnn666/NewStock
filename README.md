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
├── agent_demo.py             # Agent 编排命令行演练（4 条典型场景）
├── docs/                     # 编排设计说明 + 三条典型轨迹
└── app/
    ├── main.py               # FastAPI 装配、异常处理、启动初始化
    ├── config.py             # 全部配置（环境变量可覆盖）
    ├── db.py                 # SQLite 模型 + 初始化 + 种子数据
    ├── security.py           # PBKDF2 密码哈希 + JWT
    ├── deps.py               # 数据库会话 / 当前用户 / 角色校验
    ├── knowledge.py          # 解析、切片、Embedding、BM25+FAISS 检索
    ├── llm.py                # LLM 网关（可配置、流式、提示词动态生效）
    ├── tools.py              # Agent 工具规范 + 顺序硬门禁
    ├── agent/                # Agent 编排层
    │   ├── harness.py        #   执行循环（决策→工具→回填→中断确认）
    │   ├── decider.py        #   决策器：LLM / 离线启发式
    │   ├── prompts.py        #   编排提示词（流程契约 + few-shot）
    │   └── trace.py          #   轨迹落库（runs / steps / confirmations）
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

## Agent 编排层（执行循环）

`app/agent/` 实现了「模型决策 → 执行工具 → 结果回填 → 再决策」的受控循环，最多 **5 轮**（`AGENT_MAX_ROUNDS`）。

```bash
python agent_demo.py          # 命令行跑 4 条典型场景（不需要 API Key）
python agent_demo.py "帮我提交一款食品类商品的上架申请"
```

| 能力 | 实现 |
|---|---|
| 框架 | 自建 Harness（薄执行壳），决策器可替换：`LLMDecider`（OpenAI 兼容 Function Calling）／`HeuristicDecider`（离线规则脑） |
| 轮次控制 | 一轮 = 一次模型决策，一轮内可串行多个工具调用；超预算时如实汇报「已完成/还差什么」 |
| 逐轮日志 | `agent_steps`：轮次、工具名、参数、耗时、结果摘要、状态（ok/error/gate_blocked/skipped/confirm_pending） |
| 强制中断 | `create_goods_draft` 前必停：草稿内容逐行流式推送 → 前端确认卡片 → 确认后才真正执行 |
| 硬门禁 | 三项校验未全过时工具不执行，回填 `E-STATE-001`；令牌校验 `E-STATE-003` |

接口：

- `POST /api/agent/run` — 启动执行，SSE 推 `meta/thinking/tool_call/tool_result/draft_*/confirm_required/delta/citations/done`
- `POST /api/agent/confirm` — 人工确认（`{confirm_id, approved}`），同意后从断点续跑
- `GET /api/agent/runs`、`GET /api/agent/runs/{run_id}/steps` — 执行轨迹（阶段 5 分析数据源）
- 页面 `/admin/agent` — 执行轨迹看板（审核员 / 管理员）

详细设计见 [`docs/agent_design.md`](docs/agent_design.md)，三条典型轨迹见 [`docs/agent_trajectories.md`](docs/agent_trajectories.md)。

## 权限

| 模块 | submitter | reviewer | admin |
|---|---|---|---|
| 聊天 / Agent 执行 | 是 | 否 | 是 |
| 规则库 | 否 | 否 | 是 |
| 提示词 | 否 | 否 | 是 |
| 审计 / 执行轨迹 | 否 | 是 | 是 |

## Agent 顺序约束

`material_validate → qualification_audit → compliance_check → create_goods_draft`，任一前置未通过时后续工具直接抛 `E-STATE-001/002/003`，模型无法跳过校验直接建草稿。

## 评测与回归（QA）

30 条评测用例，覆盖规则问答 / 工具调用顺序 / 违规意图 / 多轮上下文：

```bash
python eval_runner.py --static   # 静态校验：字段、分类配比、工具名合法性（可进 CI）
python eval_runner.py            # 真跑 Agent，逐条比对工具序列与关键回答
```

- 用例集：[`docs/eval_cases.json`](docs/eval_cases.json)
- 评测报告（含修复前后对比与留出检验）：[`docs/eval_report.md`](docs/eval_report.md)
- 其中 3 条是**规则中不存在答案的陷阱题**（图书 ISBN / 二类医疗器械备案 / 直播口播话术），期望回答"规则中未找到"

判定口径：期望工具序列必须是实际序列的子序列（只读工具之间不比顺序，有依赖的写操作严格比顺序）；期望不含建草稿则实际也不许出现；陷阱题答案必须包含"未找到"。

## 说明

- 向量检索为进程内 FAISS，服务重启后从 SQLite 重建。
- 中文分词使用 `jieba`（未安装时退化为二元组）；单字切分会让 BM25 失去区分度，不建议回退。
- 检索结果附带 `support` 依据判定：条文答不了的问题一律回"规则中未找到"，不硬编。
- PDF 使用 `pypdf` 提取文本，扫描件/图片 PDF 不做 OCR。
- 删除规则文档会删除其切片并全量重建索引。
