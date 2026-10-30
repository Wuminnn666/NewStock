# -*- coding: utf-8 -*-
"""Agent 编排演练（命令行）。

用途：
  1) 不启动 Web，也能完整看到「决策 → 工具 → 回填 → 再决策 → 中断确认」的执行轨迹；
  2) 三条典型对话（只问规则 / 走完整上架 / 判断单份资料）的预期轨迹可复现；
  3) 打印 agent_steps 里的真实日志（工具名、参数、耗时、结果摘要），
     这就是阶段 5 做分析看板的数据源。

运行：
    python agent_demo.py            # 跑三条内置场景
    python agent_demo.py "自定义问题"  # 跑单条
"""
import asyncio
import json
import sys
from pathlib import Path

from app.agent import AgentHarness
from app.agent import trace as agent_trace
from app.db import RuleDoc, SessionLocal, init_db, seed_basic
from app.knowledge import index_state, ingest_document

SCENES = [
    ("A", "3C 数码类商品上架的资料标准是什么？"),
    ("B", "帮我提交一款食品类商品的上架申请"),
    ("C", "这份商标授权书能通过审核吗"),
    # 补充场景：资料齐全，用来演示「强制中断 → 提交方确认 → 续跑建草稿」的完整闭环
    ("D", "帮我提交一款食品类商品的上架申请，已提供营业执照、食品经营许可证、质检报告"),
]


def bootstrap():
    """初始化库表、账号、示例知识库（与 Web 启动一致，保证检索有数据）。"""
    init_db()
    seed_basic()
    db = SessionLocal()
    try:
        if db.query(RuleDoc).count() == 0:
            p = Path("审核制度.md")
            if p.exists():
                ingest_document(db, "审核制度.md", p.read_bytes(), "审核规范",
                                title="商品上架审核制度（示例）")
        index_state.build(db)
    finally:
        db.close()


def _fmt_args(args: dict, limit: int = 90) -> str:
    s = json.dumps(args, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "…"


async def run_scene(tag: str, question: str, auto_confirm: bool = True):
    print(f"\n{'=' * 78}\n场景 {tag}：{question}\n{'=' * 78}")
    session_id = f"demo_{tag.lower()}"
    h = AgentHarness(session_id=session_id, user_id="1")

    confirm_id = None
    async for event, data in h.aevents(question):
        if event == "thinking":
            print(f"\n[第 {data['round']}/{data['max']} 轮] 模型决策…")
        elif event == "tool_call":
            print(f"  → 调用 {data['tool']}  参数 {_fmt_args(data['args'])}")
        elif event == "tool_result":
            ms = f" · {data['elapsed_ms']}ms" if data.get("elapsed_ms") else ""
            print(f"  ← {data['status']}{ms} · {data['summary'][:110]}")
        elif event == "draft_start":
            print("\n  ⏸  强制中断：草稿内容流式送达提交方确认")
        elif event == "draft_delta":
            print("     " + data["text"].rstrip())
        elif event == "confirm_required":
            confirm_id = data["confirm_id"]
            print(f"  ⏳ 等待确认（confirm_id={confirm_id}）")
        elif event == "delta":
            print(data["text"], end="", flush=True)
        elif event == "citations":
            print("\n\n  📎 引用来源：", ", ".join(
                f"{c['file']}#{c['chunk']}({c['score']})" for c in data))
        elif event == "done":
            print("\n  ✅ 执行结束")
        elif event == "error":
            print(f"\n  ❌ 错误 {data['code']}：{data['message']}")

    # 自动替提交方点「确认」（真实场景由前端按钮触发）
    if confirm_id and auto_confirm:
        print("\n  👤 提交方点击「确认创建草稿」→ 续跑循环")
        async for event, data in h.aevents_confirm(confirm_id, True, "submitter"):
            if event == "tool_result":
                print(f"  ← {data['tool']} {data['status']} · {data['summary'][:90]}")
            elif event == "delta":
                print(data["text"], end="", flush=True)
            elif event == "done":
                print("\n  ✅ 确认后续跑结束")

    # 打印真实落库的轨迹（阶段 5 分析口径）
    print(f"\n  —— agent_steps 落库明细（run_id={h.run_id}）——")
    for s in agent_trace.list_steps(h.run_id):
        print(f"    R{s.round_no}.{s.step_no:<2} {s.tool_name:<26} {s.status:<14} "
              f"{s.elapsed_ms:>5}ms  {(s.result_summary or '')[:70]}")


async def main():
    bootstrap()
    targets = SCENES
    if len(sys.argv) > 1:
        targets = [("X", " ".join(sys.argv[1:]))]
    for tag, q in targets:
        await run_scene(tag, q)
    print("\n全部场景执行完毕。")


if __name__ == "__main__":
    asyncio.run(main())
