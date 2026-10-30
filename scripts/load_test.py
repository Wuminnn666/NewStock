# -*- coding: utf-8 -*-
"""最小压测脚本（对应上线清单 B5）：给出可复核的容量基线，替代「感觉挺快」。

只压最贵的那条链路——`POST /api/chat`（SSE 流式 + 检索 + 模型）。
指标：成功率、首字节时延（TTFB）P50/P95、整条流完成时延 P50/P95、
      结束后 /api/health 里的 SSE 水位（判断连接是否泄漏）。

用法：
    python scripts/load_test.py --base http://127.0.0.1:8034 --concurrency 50 --total 200
注意：压测前必须把限流/配额调高（否则测的是限流器，不是系统容量）：
    RATE_CHAT_PER_MIN=100000 LLM_DAILY_TOKENS_PER_USER=999999999
"""
import argparse
import asyncio
import json
import statistics
import time

import httpx

QUESTIONS = [
    "食品类商品需要哪些资质材料？",
    "这款儿童玩具有 3C 认证，还需要质检报告吗？",
    "化妆品上架对生产许可证有什么要求？",
    "商品详情页可以写「全网最低价」吗？",
    "营业执照和品牌授权书缺一份能先上架吗？",
]


async def one(client, q, idx):
    """跑一条完整流，返回 (成功?, TTFB, 总耗时, 事件数)。"""
    t0 = time.perf_counter()
    ttfb = None
    events = 0
    try:
        async with client.stream("POST", "/api/chat", json={"question": q}) as r:
            if r.status_code != 200:
                await r.aread()
                return False, None, time.perf_counter() - t0, 0, r.status_code
            async for line in r.aiter_lines():
                if line.startswith("event:"):
                    events += 1
                    if ttfb is None:
                        ttfb = time.perf_counter() - t0
        return True, ttfb, time.perf_counter() - t0, events, 200
    except Exception as e:                # noqa: BLE001
        return False, ttfb, time.perf_counter() - t0, events, type(e).__name__


def pct(vals, p):
    if not vals:
        return 0.0
    vals = sorted(vals)
    k = max(0, min(len(vals) - 1, int(round(p / 100 * (len(vals) - 1)))))
    return vals[k]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8034")
    ap.add_argument("--concurrency", type=int, default=50)
    ap.add_argument("--total", type=int, default=200)
    ap.add_argument("--user", default="submitter")
    ap.add_argument("--pwd", default="123456")
    a = ap.parse_args()

    limits = httpx.Limits(max_connections=a.concurrency + 10,
                          max_keepalive_connections=a.concurrency)
    async with httpx.AsyncClient(base_url=a.base, timeout=120, limits=limits,
                                 trust_env=False) as c:
        r = await c.post("/api/auth/login", json={"username": a.user, "password": a.pwd})
        print("登录:", r.status_code)
        print("开压前水位:", (await c.get("/api/health")).json()["sse"])

        sem = asyncio.Semaphore(a.concurrency)
        results = []

        async def worker(i):
            async with sem:
                results.append(await one(c, QUESTIONS[i % len(QUESTIONS)], i))

        t0 = time.perf_counter()
        await asyncio.gather(*[worker(i) for i in range(a.total)])
        wall = time.perf_counter() - t0

        ok = [x for x in results if x[0]]
        fail = [x for x in results if not x[0]]
        ttfb = [x[1] for x in ok if x[1] is not None]
        dur = [x[2] for x in ok]

        print("\n===== 结果 =====")
        print(f"总请求 {a.total} | 成功 {len(ok)} | 失败 {len(fail)} | "
              f"成功率 {len(ok) / a.total * 100:.1f}%")
        print(f"并发 {a.concurrency} | 总耗时 {wall:.1f}s | 吞吐 {a.total / wall:.1f} req/s")
        print(f"TTFB  P50 {pct(ttfb,50)*1000:.0f}ms  P95 {pct(ttfb,95)*1000:.0f}ms  "
              f"max {max(ttfb or [0])*1000:.0f}ms")
        print(f"完成  P50 {pct(dur,50)*1000:.0f}ms  P95 {pct(dur,95)*1000:.0f}ms  "
              f"max {max(dur or [0])*1000:.0f}ms")
        if fail:
            codes = {}
            for f in fail:
                codes[f[4]] = codes.get(f[4], 0) + 1
            print("失败分布:", codes)
        print("压测后水位:", (await c.get("/api/health")).json()["sse"])
        print("（若 active 不为 0，说明连接未回收，属于 P0 缺陷）")


if __name__ == "__main__":
    asyncio.run(main())
