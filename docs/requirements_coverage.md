# 需求发现报告 → 系统能力覆盖对照表

> 对照文档：discovery-report.md（万象商家中心 · 业务需求发现报告，2026-11-05）
> 本表逐条说明报告提出的 REQ-1~REQ-6 在本项目中的落地位置与验证方法。
> 评审时按「验证方法」一列逐条复现即可。

## 覆盖总览

| 报告需求 | 优先级 | 系统能力 | 状态 |
|---|---|---|---|
| REQ-1 属性冲突预检（只读提示、不自动处置） | P0 | `attribute_conflict_check` 工具 + Agent 流程强制前置 | ✅ 已实现并实测 |
| REQ-2 数据质量校验与人工修复队列 | P0 | 冲突明细入库 + `/admin/tasks` 人工处置队列 | ✅ 已实现并实测 |
| REQ-3 转人工兜底（触发原因/接手人/确认时间） | P0 | `escalate_manual` 工具 + manual_tasks 表三硬字段 | ✅ 已实现并实测 |
| REQ-4 权限与授权岗位清单 | P0 | 审核制度第七章签核责任 + RBAC 三角色边界 | ✅ 已实现 |
| REQ-5 接口中断快照兜底（带时间戳、超周期标待刷新） | P1 | 知识快照时间戳 + `/api/health` stale 标记 | ✅ 已实现 |
| REQ-6 验收指标固定分母、排除规则、分岗耗时 | P1 | `/api/admin/metrics`（每指标带口径说明） | ✅ 已实现 |

## 逐条说明

### REQ-1 属性冲突预检（报告核心场景：规格表 220V / 图片 110V）

**落地**：`app/tools.py :: attribute_conflict_check`
- 跨字段比对同维度数值（电压/功率/容量/电池容量/重量/尺寸），发现不一致输出
  字段位置 + 两边数值 + 「AI 不代为判断哪边正确」的处置建议；
- **只读提示，不自动处置**：检测到冲突后 Agent 禁止调用 `create_goods_draft`，
  必须转人工（调用顺序红线同时写进了系统提示词 prompts.py 第 7/8 条与服务端门禁）。

**验证**（报告原场景 220V vs 110V）：
```bash
# 提问：帮我创建商品草稿：儿童电动积木，规格表标注电压 220V，详情页图片写的是 110V，营业执照、CCC证书、质检报告都齐全
# 预期：工具链 material_validate→qualification_audit→compliance_check→attribute_conflict_check→escalate_manual
# 回答：属性冲突预检未通过，本次不创建草稿；已转人工工单 MT-xxx，接手人：内容审核员
python scripts/demo_rehearsal.py   # Q8 自动复现
```

### REQ-2 人工修复队列

**落地**：`app/db.py :: ManualTask` + `app/templates/tasks.html` + `/admin/tasks`
- 冲突/缺依据入队，有歧义记录**不进入自动推荐流程**（检测到冲突即阻断建草稿）；
- 队列字段含冲突明细（字段位置+数值），管理员后台可见。

**验证**：登录 admin → `/admin/tasks`（导航「人工处置队列」）→ 看到工单与冲突明细。

### REQ-3 转人工兜底三硬字段

**落地**：`manual_tasks` 表结构强制：
- `trigger_reason`（触发原因，必填非空）
- `handler_role`（接手人，默认「内容审核员」）
- `confirmed_at`（确认时间，处置时由 `POST /api/admin/tasks/{id}/confirm` 回填，重复确认返回 409）
- **不会只给一句模糊提醒**：AI 收尾回答必须带工单号与接手人。

**验证**：
```bash
curl -b cookies http://127.0.0.1:8000/api/admin/tasks           # 列表：含三字段
curl -X POST -b cookies -d '{"note":"已联系商家"}' \
     http://127.0.0.1:8000/api/admin/tasks/1/confirm           # 回填确认时间闭环
```

### REQ-4 权限与授权岗位清单

**落地**：
- 《审核制度.md》第七章：各品类签核责任人（儿童玩具/食品→商品合规专员签核等）、
  授权查看范围、「不得借用他人账号」；
- 系统侧 RBAC 三角色硬边界：submitter 只能咨询与确认自己的草稿；
  reviewer/admin 才能看审计、处置工单；写操作工具仅可在 Agent 编排内按门禁顺序调用
  （直调接口 `/api/agent/tools` 对写类工具一律 403）。

**验证**：分别用 submitter / reviewer / admin 登录，观察左侧导航与接口权限差异；
`GET /api/admin/tasks` 用 submitter 请求返回 403。

### REQ-5 快照兜底（带时间戳、超周期标待刷新）

**落地**：`app/knowledge.py :: IndexState.snapshot_status()`
- 知识快照带 `built_at` 时间戳；超过 15 分钟刷新周期自动标 `stale: true`，
  健康探针显式暴露「暂未更新」状态，不冒充实时；
- 上传/删除规则文档即重建快照并刷新时间戳（对应"重连后补齐"）。

**验证**：
```bash
curl --noproxy '*' http://127.0.0.1:8000/api/health
# → "knowledge_snapshot": {"chunks": 4, "age_seconds": 7.8, "stale": false, ...}
```

### REQ-6 验收指标口径

**落地**：`GET /api/admin/metrics`
- 每个指标**带分母定义与排除规则**（"失败/中断计入分母不剔除"、"一次流程只签发一张
  确认单"），杜绝"同一业务拆多次成功抬高比例"；
- 分岗耗时：按工具统计 p50/avg 耗时（数据源 agent_steps 逐笔采集，非通知数量）。

**验证**：
```bash
curl -b cookies http://127.0.0.1:8000/api/admin/metrics
# → denominators / exclusions / agent{completion_rate} / confirmation{approve_rate}
#   / manual_queue{open,closed} / stage_latency{各工具 p50,avg,n}
```

## 报告红线遵守情况

| 报告红线 | 系统行为 |
|---|---|
| AI 只提示、不做资质/法律结论 | 冲突预检返回 advice 字段固定为「需商家确认，AI 不代为判断」；建草稿必须人工确认 |
| 不自动上架/不认证证书真实性 | `create_goods_draft` 只建草稿进待提交队列，三道门禁（E-STATE-001/002/003）+ 人工确认单 |
| 转人工不能只给模糊提醒 | 工单三硬字段 + 回答回执带工单号与接手人 |
| 不能把通知数量当完成量 | metrics 分母取全量执行/确认单，逐笔采集 |

## 复现全部验证（一条命令）

```bash
cd /e/Wuminnn/NewStock
python scripts/demo_rehearsal.py          # 9 问彩排：含冲突转人工（Q8）
python eval_runner.py                     # 30/30 评测回归
curl --noproxy '*' http://127.0.0.1:8000/api/health | grep knowledge   # 快照时间戳
```
