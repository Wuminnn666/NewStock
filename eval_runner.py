# -*- coding: utf-8 -*-
"""评测执行器 —— 把 docs/eval_cases.json 的 30 条用例跑一遍。

两种模式：
    python eval_runner.py --static   # 只做静态校验（不跑 Agent，秒级，可进 CI）
    python eval_runner.py            # 真跑 Agent，逐条比对工具序列与关键回答

判定标准（三档，故意做得宽松一点，避免误杀）：
    1) 工具序列：期望序列必须是实际序列的「子序列」。
       允许模型多调只读工具（如多检索一次），但不允许缺步、不允许乱序。
       例：期望 [material_validate, qualification_audit]
           实际 [search_review_rules, material_validate, qualification_audit] → 通过
           实际 [qualification_audit, material_validate]                     → 失败（乱序）
    2) 陷阱题：期望回答含「规则中未找到」时，最终答案必须包含「未找到」，
       且不得出现编造的资质名（由人工抽查，脚本只做关键词兜底）。
    3) 不该建草稿的用例（期望序列里没有 create_goods_draft）：实际也不许出现。
"""
import argparse
import asyncio
import collections
import json
import re
import sys
from pathlib import Path

CASES = Path(__file__).with_name("docs") / "eval_cases.json"

# 轮次切分：兼容「第 1 轮：」「第2轮:」两种写法
TURN_RE = re.compile(r"第\s*\d+\s*轮\s*[：:]\s*")

# 只读工具集合：纯咨询类用例只允许出现这些，否则视为「过度执行」
READ_ONLY = {"search_review_rules", "get_category_requirements"}


def load_cases():
    return json.loads(CASES.read_text(encoding="utf-8"))


def split_turns(question: str):
    """把多轮用例拆成单轮问题列表；单轮用例原样返回。"""
    parts = [p.strip() for p in TURN_RE.split(question) if p.strip()]
    return parts or [question]


def is_subsequence(expected, actual):
    """expected 是否为 actual 的子序列（保持相对顺序，允许中间夹别的）。"""
    it = iter(actual)
    return all(any(x == e for x in it) for e in expected)


def static_check(cases):
    """静态校验：字段、分类配比、工具名合法性。"""
    from app.tools import DISPATCH

    ok = True
    keys = {"question", "expected_tools", "expected_behavior", "category"}
    print(f"用例总数：{len(cases)}")
    for i, c in enumerate(cases, 1):
        miss = keys - set(c)
        if miss:
            print(f"  ✗ #{i} 缺字段 {miss}")
            ok = False
    counter = collections.Counter(c["category"] for c in cases)
    for k, v in counter.items():
        print(f"  {k}: {v}")
    bad = {t for c in cases for t in c["expected_tools"]} - set(DISPATCH)
    if bad:
        print(f"  ✗ 不存在的工具：{bad}")
        ok = False
    traps = [c for c in cases if "规则中未找到" in c["expected_behavior"]]
    print(f"  陷阱题（期望“规则中未找到”）：{len(traps)} 条")
    print("静态校验：", "通过" if ok else "不通过")
    return ok


async def run_one(idx, case):
    """跑一条用例，返回 (是否通过, 实际工具序列, 最终答案, 说明)。"""
    from app.agent import AgentHarness

    turns = split_turns(case["question"])
    tools, final_text = [], ""
    # 多轮用例必须复用同一个 session_id，否则上下文断裂，等于没测
    harness = AgentHarness(session_id=f"eval_{idx}", user_id="1")

    for t in turns:
        try:
            async for ev, data in harness.aevents(t):
                if ev == "tool_call":
                    tools.append(data["tool"])
                elif ev == "delta":
                    final_text += data.get("text", "")
                elif ev == "error":
                    return False, tools, final_text, f"运行报错 {data.get('code')}: {data.get('message')}"
        except Exception as e:      # 单条失败不影响整批
            return False, tools, final_text, f"异常：{type(e).__name__}: {e}"

    expected = case["expected_tools"]
    # 判定 1：期望的工具必须按顺序出现过
    if not is_subsequence(expected, tools):
        return False, tools, final_text, f"工具序列不符（期望 {expected}）"
    # 判定 2：期望序列里没有 create_goods_draft，实际也不许出现
    if "create_goods_draft" not in expected and "create_goods_draft" in tools:
        return False, tools, final_text, "越权创建了草稿"
    # 判定 2b：纯咨询类（期望工具全为只读）不许顺手跑校验/写操作——
    #          用户只是问规则，Agent 不该替他推进流程（过度执行同样是缺陷）。
    if set(expected) <= READ_ONLY and not set(tools) <= READ_ONLY:
        extra = [t for t in tools if t not in READ_ONLY]
        return False, tools, final_text, f"纯咨询却执行了写/校验工具 {extra}"
    # 判定 3：陷阱题必须明确说“未找到”
    if "规则中未找到" in case["expected_behavior"] and "未找到" not in final_text:
        return False, tools, final_text, "陷阱题未回答“规则中未找到”"
    return True, tools, final_text, "OK"


async def run_all(cases):
    from agent_demo import bootstrap      # 复用演练脚本的初始化（建表/种子/知识库）
    bootstrap()

    rows, passed = [], 0
    for i, case in enumerate(cases, 1):
        ok, tools, text, note = await run_one(i, case)
        passed += bool(ok)
        rows.append((i, case["category"], ok, tools, note))
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] #{i:02d} {case['category']} | {case['question'][:26]}"
              f"\n        实际工具: {tools or '（无）'}"
              f"\n        {note}")

    print("\n" + "=" * 60)
    print(f"通过 {passed}/{len(cases)}    通过率 {passed / len(cases):.1%}")
    by_cat = collections.defaultdict(lambda: [0, 0])
    for _, cat, ok, _, _ in rows:
        by_cat[cat][0] += bool(ok)
        by_cat[cat][1] += 1
    for cat, (p, t) in by_cat.items():
        print(f"  {cat}: {p}/{t}")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--static", action="store_true", help="只做静态校验")
    args = ap.parse_args()

    cases = load_cases()
    if args.static:
        sys.exit(0 if static_check(cases) else 1)
    if not static_check(cases):
        sys.exit(1)
    print("-" * 60)
    asyncio.run(run_all(cases))


if __name__ == "__main__":
    main()
