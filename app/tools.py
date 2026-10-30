# -*- coding: utf-8 -*-
"""Agent 工具层：Function Calling 规范 + 服务端硬门禁（顺序约束）。

核心约束：
  material_validate -> qualification_audit -> compliance_check -> create_goods_draft
  任一前置未通过，后续工具直接抛 ApiError，模型无法绕过。
"""
import hashlib
import json
import time

from .errors import ApiError

# 每次会话的预检状态（生产可换 Redis）
_STATE: dict = {}

CATEGORY_REQ = {
    "食品": ["营业执照", "食品经营许可证", "质检报告"],
    "化妆品": ["营业执照", "化妆品生产许可证", "质检报告"],
    "3C数码": ["营业执照", "CCC证书", "品牌授权书"],
    "服饰": ["营业执照", "品牌授权书"],
    "儿童玩具": ["营业执照", "CCC证书"],
}
BANNED_WORDS = ["最", "第一", "国家级", "全网最低", "绝对", "100%", "治疗", "根治"]


# ---------------- 工具函数 ----------------
def _payload_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def search_review_rules(query: str, top_k: int = 3):
    from .knowledge import index_state
    return {"tool": "search_review_rules", "results": index_state.retrieve(query, top_k=top_k)}


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
]

DISPATCH = {
    "search_review_rules": search_review_rules,
    "get_category_requirements": get_category_requirements,
    "material_validate": material_validate,
    "qualification_audit": qualification_audit,
    "compliance_check": compliance_check,
    "create_goods_draft": create_goods_draft,
    "request_missing_material": request_missing_material,
}
