# -*- coding: utf-8 -*-
"""数据保留清理（对应上线清单 A3）。

用法：
    python scripts/retention.py                 # 只统计，不删除（默认安全模式）
    python scripts/retention.py --apply         # 真正执行删除
    python scripts/retention.py --apply --archive backups/   # 先归档成 JSONL 再删

为什么默认 dry-run：清理任务最典型的线上事故是「天数配错一位」，
一个 -30 写成 -3 就把三个月的数据删了。默认只看不动，是最后一道保险。
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings                      # noqa: E402
from app.db import (                                 # noqa: E402
    AgentRun, AgentStep, ChatMessage, ChatSession, SecurityEvent, SessionLocal,
)


def _cutoff(days: int) -> datetime:
    # 与库里的 now() 保持一致：都是不带时区的 UTC 时间
    return datetime.utcnow() - timedelta(days=days)


def _dump(rows, path, name):
    """把待删数据先写成 JSONL，删错了还能捡回来。"""
    if not rows:
        return 0
    os.makedirs(path, exist_ok=True)
    fn = os.path.join(path, f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl")
    n = 0
    with open(fn, "w", encoding="utf-8") as f:
        for r in rows:
            d = {c.name: getattr(r, c.name) for c in r.__table__.columns}
            for k, v in list(d.items()):
                if isinstance(v, datetime):
                    d[k] = v.isoformat()
            f.write(json.dumps(d, ensure_ascii=False, default=str) + "\n")
            n += 1
    print(f"  归档 {name}: {n} 条 → {fn}")
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正删除（默认只统计）")
    ap.add_argument("--archive", default="", help="删除前归档到该目录（JSONL）")
    ap.add_argument("--chat-days", type=int, default=settings.RETENTION_CHAT_DAYS)
    ap.add_argument("--agent-days", type=int, default=settings.RETENTION_AGENT_DAYS)
    ap.add_argument("--security-days", type=int, default=settings.RETENTION_SECURITY_DAYS)
    args = ap.parse_args()

    print(f"模式：{'执行删除' if args.apply else '仅统计（dry-run）'}")
    print(f"保留期：对话 {args.chat_days} 天 / 轨迹 {args.agent_days} 天 / 安全事件 {args.security_days} 天")

    db = SessionLocal()
    try:
        c_chat = _cutoff(args.chat_days)
        c_agent = _cutoff(args.agent_days)
        c_sec = _cutoff(args.security_days)

        old_msgs = (db.query(ChatMessage)
                    .filter(ChatMessage.created_at < c_chat).all())
        old_runs = db.query(AgentRun).filter(AgentRun.created_at < c_agent).all()
        old_steps = db.query(AgentStep).filter(AgentStep.created_at < c_agent).all()
        old_sec = db.query(SecurityEvent)
        old_sec = old_sec.filter(SecurityEvent.created_at < c_sec).all()

        # 会话本身没有独立保留期：消息清空后，没有任何消息的会话也一并清掉
        live_sids = {s for (s,) in db.query(ChatMessage.session_id).distinct().all()}
        orphan = [s for s in db.query(ChatSession).all() if s.session_id not in live_sids]

        print(f"\n待处理：消息 {len(old_msgs)} / 执行记录 {len(old_runs)} / 轨迹 {len(old_steps)} "
              f"/ 安全事件 {len(old_sec)} / 空会话 {len(orphan)}")

        if not args.apply:
            print("\n结论：dry-run 结束，未删除任何数据。确认无误后加 --apply 执行。")
            return 0

        if args.archive:
            _dump(old_msgs, args.archive, "chat_messages")
            _dump(old_runs, args.archive, "agent_runs")
            _dump(old_steps, args.archive, "agent_steps")
            _dump(old_sec, args.archive, "security_events")

        for r in old_steps:
            db.delete(r)
        for r in old_msgs:
            db.delete(r)
        for r in old_runs:
            db.delete(r)
        for s in orphan:
            db.delete(s)
        # 安全事件按更长的保留期单独执行（不合规的留痕不能跟业务数据一起删）
        for r in old_sec:
            db.delete(r)
        db.commit()
        print("\n✅ 已删除完成")
        print(f"  消息 {len(old_msgs)} / 执行记录 {len(old_runs)} / 轨迹 {len(old_steps)} "
              f"/ 安全事件 {len(old_sec)} / 空会话 {len(orphan)}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
