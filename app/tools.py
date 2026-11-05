# -*- coding: utf-8 -*-
"""Agent 工具层：Function Calling 规范 + 服务端硬门禁（顺序约束）。

核心约束：
  material_validate -> qualification_audit -> compliance_check -> create_goods_draft
  任一前置未通过，后续工具直接抛 ApiError，模型无法绕过。
"""
import hashlib
import json
import re
import time

from .errors import ApiError

# 每次会话的预检状态（生产可换 Redis）
_STATE: dict = {}

CATEGORY_REQ = {
    "食品": ["营业执照", "食品经营许可证", "质检报告"],
    "化妆品": ["营业执照", "化妆品生产许可证", "质检报告"],
    "3C数码": ["营业执照", "CCC证书", "品牌授权书"],
    "服饰": ["营业执照", "品牌授权书"],
    "儿童玩具": ["营业执照", "CCC证书", "质检报告"],
}
BANNED_WORDS = ["最", "第一", "国家级", "全网最低", "绝对", "100%", "治疗", "根治"]


# ---------------- 工具函数 ----------------
def _payload_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def search_review_rules(query: str, top_k: int = 3):
    """检索审核规则条文（只读）。

    返回值里额外带一个 support 字段：检索层对「这批条文能否回答问题」的判断。
    为什么要把置信度显式吐出来？
        top-k 检索永远有返回，模型看到三段文字就容易硬编答案。
        把「依据是否充分」变成结构化信号，决策器/提示词才有据可依，
        该说「规则中未找到」的时候才说得出口。
    """
    from .knowledge import analyze_support, index_state
    hits = index_state.retrieve(query, top_k=top_k)
    support = analyze_support(query, [h.get("content", "") for h in hits])
    return {"tool": "search_review_rules", "results": hits, "support": support}


def get_category_requirements(category: str):
    return {"tool": "get_category_requirements", "category": category,
            "required": CATEGORY_REQ.get(category, [])}


def material_validate(session_id: str, payload: dict):
    """资料完整性校验（第一步）。"""
    issues = []
    for field in ("title", "category", "description"):
        if not payload.get(field):
            issues.append(f"缺少字段：{field}")
    if not payload.get("materials"):
        issues.append("未上传任何资质材料")
    passed = not issues
    st = _STATE.setdefault(session_id, {})
    st.update({
        "payload_hash": _payload_hash(payload),
        "material": passed,
        "qualification": False,
        "compliance": False,
        "ts": time.time(),
    })
    st["token"] = hashlib.sha256((st["payload_hash"] + str(time.time())).encode()).hexdigest() if passed else None
    return {"tool": "material_validate", "passed": passed, "issues": issues, "token": st.get("token")}


def qualification_audit(session_id: str, payload: dict):
    """资质审核（第二步）：必须先通过资料校验。"""
    st = _STATE.get(session_id, {})
    if not st.get("material"):
        raise ApiError("E-STATE-001", "前置资料校验未通过，禁止进行资质审核", 409)
    if _payload_hash(payload) != st.get("payload_hash"):
        raise ApiError("E-STATE-002", "商品资料已变更，请重新校验", 409)
    required = CATEGORY_REQ.get(payload.get("category", ""), [])
    have = set(payload.get("materials", []))
    missing = [m for m in required if m not in have]
    passed = not missing
    st["qualification"] = passed
    return {"tool": "qualification_audit", "passed": passed,
            "issues": [f"缺少资质：{m}" for m in missing]}


def compliance_check(session_id: str, payload: dict):
    """合规校验（第三步）：资质通过后才允许。"""
    st = _STATE.get(session_id, {})
    if not st.get("qualification"):
        raise ApiError("E-STATE-001", "资质审核未通过，禁止进行合规校验", 409)
    if _payload_hash(payload) != st.get("payload_hash"):
        raise ApiError("E-STATE-002", "商品资料已变更，请重新校验", 409)
    text = f"{payload.get('title', '')} {payload.get('description', '')}"
    hits = [w for w in BANNED_WORDS if w in text]
    passed = not hits
    st["compliance"] = passed
    return {"tool": "compliance_check", "passed": passed,
            "issues": [f"疑似违规用语：{w}" for w in hits]}


def create_goods_draft(session_id: str, payload: dict, precheck_token: str):
    """创建草稿（最终步）：三项校验全通过 + 令牌有效 + payload 未变更。"""
    st = _STATE.get(session_id, {})
    if not (st.get("material") and st.get("qualification") and st.get("compliance")):
        raise ApiError("E-STATE-001", "前置校验未通过，禁止创建商品草稿", 409)
    if _payload_hash(payload) != st.get("payload_hash"):
        raise ApiError("E-STATE-002", "商品资料已变更，请重新校验", 409)
    if precheck_token != st.get("token"):
        raise ApiError("E-STATE-003", "预检令牌无效，禁止创建草稿", 409)
    return {"tool": "create_goods_draft", "draft_id": "D" + str(int(time.time())),
            "status": "draft_created"}


def request_missing_material(session_id: str, items: list):
    return {"tool": "request_missing_material", "task_id": "T" + str(int(time.time())),
            "missing": items}


# ---------------- 属性冲突预检（调查报告 REQ-1） ----------------
# 维度词表：单位 -> 业务维度名。跨字段比对时同一维度出现不同值即冲突。
_UNIT_DIMS = [
    (r"(\d+(?:\.\d+)?)\s*(?:v|伏)(?![a-z])", "电压"),
    (r"(\d+(?:\.\d+)?)\s*(?:w|瓦)(?![a-z])", "功率"),
    (r"(\d+(?:\.\d+)?)\s*(?:ml|毫升)(?![a-z])", "容量"),
    (r"(\d+(?:\.\d+)?)\s*(?:mah)(?![a-z])", "电池容量"),
    (r"(\d+(?:\.\d+)?)\s*(?:kg|千克|公斤)(?![a-z])", "重量"),
    (r"(\d+(?:\.\d+)?)\s*(?:cm|厘米)(?![a-z])", "尺寸"),
]


def attribute_conflict_check(session_id: str, payload: dict):
    """属性冲突预检：跨资料比对同一维度的数值（如规格 220V vs 图片 110V）。

    合规红线（报告 REQ-1）：只做只读提示，不自动处置、不替审核员下结论——
    发现冲突即标记「需人工核对」，由 escalate_manual 升级到人工处置队列。
    """
    fields = {k: (payload.get(k) or "") for k in ("title", "description")}
    found = {}          # 维度 -> {字段: [值]}
    for fname, text in fields.items():
        low = str(text).lower()
        for pat, dim in _UNIT_DIMS:
            for m in re.finditer(pat, low):
                val = m.group(1)
                found.setdefault(dim, {}).setdefault(fname, [])
                if val not in found[dim][fname]:
                    found[dim][fname].append(val)

    conflicts = []
    for dim, per_field in found.items():
        values = sorted({v for vs in per_field.values() for v in vs})
        if len(values) > 1:                      # 同一维度出现两个不同值 → 冲突
            where = "；".join(
                f"{fname}字段：{'、'.join(vs)}{dim}"
                for fname, vs in per_field.items() if vs)
            conflicts.append({
                "dimension": dim,
                "values": values,
                "where": where,
                "advice": "两处数值不一致，需商家确认后修改，AI 不代为判断哪边正确",
            })
    return {"tool": "attribute_conflict_check",
            "passed": not conflicts,
            "conflicts": conflicts,
            "checked_fields": [f for f, t in fields.items() if t]}


def escalate_manual(session_id: str, biz_ref: str, reason: str,
                    category: str = "", conflict_detail: str = "",
                    handler_role: str = "内容审核员"):
    """转人工兜底（报告 REQ-3）：把 AI 提示升级到人工处置队列。

    硬性要求：触发原因、接手人、（确认后回填的）确认时间缺一不可，
    不能只给一句模糊提醒。写入 manual_tasks 表，供后台 /admin/tasks 处置。
    """
    import time as _t
    from .db import SessionLocal, ManualTask
    db = SessionLocal()
    try:
        task_no = "MT" + str(int(_t.time() * 1000))[-10:]
        db.add(ManualTask(
            task_no=task_no,
            biz_ref=(biz_ref or session_id)[:110],
            category=(category or "")[:30],
            trigger_reason=(reason or "未注明")[:490],
            conflict_detail=(conflict_detail or "")[:490],
            handler_role=handler_role or "内容审核员",
            status="待处理",
        ))
        db.commit()
        return {"tool": "escalate_manual", "task_no": task_no,
                "handler_role": handler_role, "status": "待处理",
                "trigger_reason": reason}
    finally:
        db.close()


def get_precheck_state(session_id: str) -> dict:
    """【编排层专用】读取当前会话的预检状态。

    Agent 在调用 create_goods_draft 前，会先用它做一次「门禁预判」：
    若三项校验未全通过，就直接中断流程、把错误回灌给模型，而不是等工具抛异常。
    这样模型能拿到结构化的失败原因（而不是一段堆栈），便于它自我修正。
    """
    st = _STATE.get(session_id, {})
    return {
        "material": bool(st.get("material")),
        "qualification": bool(st.get("qualification")),
        "compliance": bool(st.get("compliance")),
        "token": st.get("token"),
        "payload_hash": st.get("payload_hash"),
    }


def clear_precheck_state(session_id: str) -> None:
    """清理会话预检状态（新一轮上架流程开始时可重置，避免脏状态复用）。"""
    _STATE.pop(session_id, None)


# ---------------- Function Calling 规范 ----------------
TOOL_SCHEMAS = [
    {"name": "search_review_rules",
     "description": "检索审核规则条文，只读。",
     "parameters": {"type": "object", "additionalProperties": False,
                    "required": ["query"],
                    "properties": {"query": {"type": "string"}, "top_k": {"type": "integer", "default": 3}}}},
    {"name": "get_category_requirements",
     "description": "查询某类目需提交的资质清单，只读。",
     "parameters": {"type": "object", "additionalProperties": False,
                    "required": ["category"],
                    "properties": {"category": {"type": "string"}}}},
    {"name": "material_validate",
     "description": "资料完整性校验。第一步，必须最先执行。",
     "parameters": {"type": "object", "additionalProperties": False,
                    "required": ["session_id", "payload"],
                    "properties": {"session_id": {"type": "string"}, "payload": {"type": "object"}}}},
    {"name": "qualification_audit",
     "description": "资质审核。仅当 material_validate.passed=true 时调用。",
     "parameters": {"type": "object", "additionalProperties": False,
                    "required": ["session_id", "payload"],
                    "properties": {"session_id": {"type": "string"}, "payload": {"type": "object"}}}},
    {"name": "compliance_check",
     "description": "违规点识别。仅当 qualification_audit.passed=true 时调用。",
     "parameters": {"type": "object", "additionalProperties": False,
                    "required": ["session_id", "payload"],
                    "properties": {"session_id": {"type": "string"}, "payload": {"type": "object"}}}},
    {"name": "create_goods_draft",
     "description": "创建商品上架草稿。仅当三项校验全部 passed=true 且持有有效 precheck_token 时调用；否则服务端拒绝。",
     "parameters": {"type": "object", "additionalProperties": False,
                    "required": ["session_id", "payload", "precheck_token"],
                    "properties": {"session_id": {"type": "string"}, "payload": {"type": "object"},
                                   "precheck_token": {"type": "string"}}}},
    {"name": "request_missing_material",
     "description": "任一校验失败时，生成待补资料清单。",
     "parameters": {"type": "object", "additionalProperties": False,
                    "required": ["session_id", "items"],
                    "properties": {"session_id": {"type": "string"},
                                   "items": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "attribute_conflict_check",
     "description": "属性冲突预检（只读）：跨字段比对同维度数值（电压/功率/容量等），"
                    "发现不一致只提示、不处置。create_goods_draft 之前必须先执行。",
     "parameters": {"type": "object", "additionalProperties": False,
                    "required": ["session_id", "payload"],
                    "properties": {"session_id": {"type": "string"}, "payload": {"type": "object"}}}},
    {"name": "escalate_manual",
     "description": "转人工兜底：冲突预检发现问题或缺依据时升级到人工处置队列，"
                    "必须写明触发原因。发现属性冲突后禁止调用 create_goods_draft。",
     "parameters": {"type": "object", "additionalProperties": False,
                    "required": ["session_id", "biz_ref", "reason"],
                    "properties": {"session_id": {"type": "string"}, "biz_ref": {"type": "string"},
                                   "reason": {"type": "string"}, "category": {"type": "string"},
                                   "conflict_detail": {"type": "string"},
                                   "handler_role": {"type": "string"}}}},
]

DISPATCH = {
    "search_review_rules": search_review_rules,
    "get_category_requirements": get_category_requirements,
    "material_validate": material_validate,
    "qualification_audit": qualification_audit,
    "compliance_check": compliance_check,
    "create_goods_draft": create_goods_draft,
    "request_missing_material": request_missing_material,
    "attribute_conflict_check": attribute_conflict_check,
    "escalate_manual": escalate_manual,
}
