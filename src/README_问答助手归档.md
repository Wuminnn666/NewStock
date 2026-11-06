# 问答助手归档说明（src）

> 项目：万象商家中心 · 商品上架与资料审核 AI 问答助手（MVP）
> 归档日期：2026-11-06 · 源码仓库：https://github.com/Wuminnn666/NewStock

## 一、归档范围

本目录是「问答助手」交付的**归档索引**。可运行源码统一放在仓库 `app/` 包内（含问答助手、Agent 框架、知识库、管理后台），本目录说明每一部分的位置、职责与人工确认点，评审时可按下表直达源码。

## 二、源码结构对照表

| 归档项 | 源码位置 | 职责 |
|---|---|---|
| 应用入口 | `run.py` → `app/main.py` | 装配路由、静态资源、启动初始化（建表/种子账号/知识索引）、生产安全自检 |
| 问答助手 | `app/api.py`（`/api/chat`）、`app/agent/decider.py` | 意图路由：闲聊/问答/材料核对/Agent 任务；RAG 依据判定（覆盖率 + 主语校验），无依据不硬答 |
| Agent 框架 | `app/agent/decider.py`、`app/tools.py` | 5 轮工具预算、precheck_token 防重放、高危动作强制中断确认、冲突预检 `attribute_conflict_check`、转人工 `escalate_manual` |
| 知识库 | `app/knowledge.py`、`app/db.py`（RuleDoc/Chunk） | 制度上传 → 分句分片 → FAISS 向量 + BM25 关键词混合检索 → 快照时间戳 staleness 兜底 |
| 数据模型 | `app/db.py` | SQLAlchemy 2.0，16 张表（用户/会话/消息/规则/分片/确认单/Agent 步骤/人工工单/审计/安全事件等） |
| 安全护栏 | `app/guard.py` | 限流、配额、并发闸门、SSE 连接计数（幂等票据）、提示词注入识别 |
| 管理后台 | `app/pages.py`、`app/templates/` | 审计留痕、Agent 运行记录、人工处置队列、指标口径页 |
| 测试与自检 | `eval_runner.py`（30 条评测）、`guard_selftest.py`、`scripts/demo_rehearsal.py`（9 问）、`scripts/sse_selftest.py`、`scripts/load_test.py` | 见 Test 交付物 |

## 三、MVP 工程架构（四层）

1. **接入层**：FastAPI + SSE 流式输出，JWT 鉴权 + RBAC（submitter/reviewer/admin），限流与并发闸门在前。
2. **编排层（Agent 框架）**：意图决策器 `decider.py` 把用户输入路由到「直接回答 / RAG 问答 / 工具调用 / 高危动作」，每轮消耗工具预算，预算耗尽或遇冲突强制中断转人工。
3. **能力层（工具与知识库）**：制度检索（混合检索）、属性冲突预检（电压/功率/容量跨字段比对，只读提示）、材料抽取、工单创建。
4. **数据层**：SQLite（WAL 模式）单机存储，备份脚本每日快照；试点规模 ≤50 并发，规模化路径见《07_详细项目设计方案》。

## 四、人工确认 / 审核点（≥3 处，逐点可演示）

| # | 确认点 | 触发场景 | 演示入口 |
|---|---|---|---|
| 1 | **Agent 高危动作二次确认** | Agent 要创建商品草稿、提交上架等写操作时，生成确认单，须授权人签核后才执行 | 对话触发提交意图 → `/admin/agent` 看确认单与签核状态 |
| 2 | **冲突转人工工单闭环** | 属性冲突预检发现 220V/110V 矛盾时，不自动放行/驳回，生成人工工单（含 task_no/reason/handler），管理员填写处置结论后才闭环 | 对话发冲突问句 → `/admin/tasks` 点「确认已处置」 |
| 3 | **退单与放行人工终审** | 系统只做只读提示与依据挂靠，退回商家、放行上架一律由 reviewer 在工单页人工决策 | `/tasks` 处理退回工单 |
| 4 | **知识库变更管理员审核** | 制度上传、提示词切换仅 admin 可操作，上传即留审计 | `/admin/rules`（RBAC 越权访问返回 403） |

## 五、运行方式

```bash
pip install -r requirements.txt
python run.py          # http://127.0.0.1:8000
python eval_runner.py  # 30 条回归评测
python scripts/demo_rehearsal.py  # 9 问演示彩排
```

线上部署地址与运行手册见 `docs/deliverables/09_项目上线交付运行手册.md`。
