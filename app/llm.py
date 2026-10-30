# -*- coding: utf-8 -*-
"""LLM 网关：供应商可配置、支持流式；提示词从数据库读取并即时生效。"""
import asyncio
import json

import httpx

from .config import settings

# 提示词缓存：以 (id, version) 为键，版本变化即失效
_prompt_cache = {"key": None, "content": None}


def get_active_prompt(db) -> str:
    """取数据库中启用的系统提示词（管理员修改后下一问即生效）。"""
    from .db import PromptTemplate
    row = db.query(PromptTemplate).filter(PromptTemplate.is_active == True).first()  # noqa: E712
    if not row:
        return settings.DEFAULT_PROMPT
    key = (row.id, row.version)
    if _prompt_cache["key"] != key:
        _prompt_cache["key"] = key
        _prompt_cache["content"] = row.content
    return _prompt_cache["content"]


def build_user_content(question: str, contexts) -> str:
    """把检索到的规则片段拼成上下文。"""
    ctx = "\n\n".join(
        f"[片段{i + 1}·{c['file']}#{c['chunk_index'] + 1}]\n{c['content']}"
        for i, c in enumerate(contexts)
    )
    return (
        f"【审核规则参考片段】\n{ctx}\n\n"
        f"【用户问题】\n{question}\n\n"
        "请严格依据上述片段回答，并在末尾注明引用的文件名与片段号。"
    )


async def stream_llm(system: str, history, user_content: str):
    """异步生成器，逐段 yield 文本增量。无 API Key 时走离线演示。"""
    messages = [{"role": "system", "content": system}]
    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_content})

    # 离线演示：无密钥时返回固定说明，便于本地跑通链路
    if not settings.LLM_API_KEY:
        demo = (
            "【离线演示模式】已检索到审核规则片段。初步意见：\n"
            "1. 请核对标题/描述是否存在绝对化用语或夸大宣传；\n"
            "2. 确认类目所需资质齐全且在有效期内；\n"
            "3. 确认不属于禁售品类。\n"
            "配置 LLM_API_KEY 后，此处将输出真实模型审核结论。"
        )
        for ch in demo:
            yield ch
            await asyncio.sleep(0.01)
        return

    url = settings.LLM_BASE_URL.rstrip("/") + "/chat/completions"
    payload = {
        "model": settings.LLM_MODEL,
        "temperature": settings.LLM_TEMPERATURE,
        "max_tokens": settings.LLM_MAX_TOKENS,
        "stream": True,
        "messages": messages,
    }
    headers = {"Authorization": f"Bearer {settings.LLM_API_KEY}"}
    # 必须显式设超时。之前写 timeout=None，等于「等一辈子」：
    # 上游网关不返回、也不关闭连接时，这个协程永远不会结束，
    # 前端一直看到「正在匹配审核规则…」，服务端连接数只增不减。
    timeout = httpx.Timeout(
        connect=settings.LLM_CONNECT_TIMEOUT,
        read=settings.LLM_READ_TIMEOUT,
        write=settings.LLM_WRITE_TIMEOUT,
        pool=settings.LLM_POOL_TIMEOUT,
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                    delta = obj["choices"][0]["delta"].get("content") or ""
                    if delta:
                        yield delta
                except Exception:
                    continue
