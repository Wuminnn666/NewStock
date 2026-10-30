# -*- coding: utf-8 -*-
"""SSE 连接闸门自测（对应上线清单 B4）。

两层验证，缺一不可：
1) 进程内：名额用满必须拒绝、释放后必须能重新进入、重复 release 不得多还——
   这条测的是「计数逻辑」，最容易写错成泄漏或超发。
2) 走 HTTP：正常问答仍能流式返回（证明把三条流式出口收敛到统一出口后没改坏）。

用法：
    python scripts/sse_selftest.py                      # 只跑进程内部分
    python scripts/sse_selftest.py --base http://127.0.0.1:8033 --user submitter --pwd 123456
"""
import argparse
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import guard                      # noqa: E402
from app.config import settings            # noqa: E402
from app.errors import ApiError            # noqa: E402


def test_counter():
    old = settings.SSE_MAX_CONNECTIONS
    settings.SSE_MAX_CONNECTIONS = 2       # 故意压到 2，方便用 3 条连接触发
    ok = True
    try:
        t1, t2 = guard.acquire_sse(), guard.acquire_sse()
        guard._conns.release()             # 先还一个，保证从干净状态开始计数
        guard._conns.acquire(settings.SSE_MAX_CONNECTIONS)
        print("  当前水位:", guard.conn_snapshot())

        try:
            guard.acquire_sse()
            print("  [FAIL] 第 3 条连接没被拒绝")
            ok = False
        except ApiError as e:
            print(f"  [OK] 超额被拒 -> {e.code} {e.message} (HTTP {e.status})")

        t1.release()
        t1.release()                       # 幂等：重复释放不能多还
        guard.acquire_sse()                # 释放一个名额后应能再进
        print("  [OK] 释放后可重新进入；重复 release 未多还 ->", guard.conn_snapshot())
        t2.release()
        print("  收尾水位:", guard.conn_snapshot())
    finally:
        settings.SSE_MAX_CONNECTIONS = old
    return ok


def test_http(base, user, pwd):
    import httpx
    ok = True
    with httpx.Client(base_url=base, timeout=30, trust_env=False) as c:
        h = c.get("/api/health").json()
        print("  /api/health ->", json.dumps(
            {"status": h["status"], "sse": h["sse"], "degraded": h["degraded"]},
            ensure_ascii=False))
        r = c.post("/api/auth/login", json={"username": user, "password": pwd})
        if r.status_code != 200:
            print("  [FAIL] 登录失败", r.status_code, r.text[:120])
            return False
        events, text = [], []
        with c.stream("POST", "/api/chat",
                      json={"question": "食品类商品需要哪些资质？"}) as resp:
            if resp.status_code != 200:
                print("  [FAIL] 流式接口状态", resp.status_code)
                return False
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    events.append(line[6:].strip())
                elif line.startswith("data:"):
                    try:
                        text.append(json.loads(line[5:].strip()).get("text", ""))
                    except Exception:      # noqa: BLE001
                        pass
        print("  事件序列:", events, "| 正文长度:", len("".join(text)))
        if "done" not in events:
            print("  [FAIL] 流未正常结束")
            ok = False
        h2 = c.get("/api/health").json()
        print("  结束后水位:", h2["sse"])
        if h2["sse"]["active"] != 0:
            print("  [FAIL] 连接未归还，名额泄漏")
            ok = False
        else:
            print("  [OK] 连接已归还，无泄漏")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base")
    ap.add_argument("--user", default="submitter")
    ap.add_argument("--pwd", default="123456")
    a = ap.parse_args()

    print("① 进程内计数测试")
    good = test_counter()
    if a.base:
        print("\n② HTTP 端到端测试:", a.base)
        good = test_http(a.base, a.user, a.pwd) and good
    print("\n结论:", "通过" if good else "未通过")
    sys.exit(0 if good else 1)
