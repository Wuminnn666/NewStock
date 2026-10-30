# -*- coding: utf-8 -*-
"""
商品上架与资料审核系统 —— 阶段1 最小原型（离线 CLI 版）
========================================================
目标：验证「RAG 混合检索 + 可配置 LLM」的审核/问答闭环，不涉及 Web。

核心流程：
  1) 读取本地「审核制度.md」→ 按 500 字切片 → 向量化存入 FAISS
  2) 命令行输入「商品资料描述 / 审核问题」
       → BM25(关键词) + 向量(语义) 混合检索 top3
       → 拼接提示词 → 调用可配置 LLM → 输出初步审核意见 / 规则解答
  3) LLM 调用封装为独立函数，供应商可配置（OpenAI 兼容协议）

复用说明：本脚本中标记为 【核心·可复用】 的模块（配置、切片、BM25、
         向量库、Embedding 客户端、LLM 网关、提示词装配、混合检索）
         在后续 Web 阶段会原样迁入：
            app/core/config.py      （配置）
            app/knowledge/ingest.py （切片）
            app/knowledge/bm25.py + vector_store.py（检索）
            app/agents/embeddings.py / llm_gateway.py / rag_retriever.py / prompt_builder.py

依赖安装：
    pip install numpy faiss-cpu requests
运行：
    python product_review_stage1.py
    （不配置 LLM_API_KEY 时自动进入离线 MOCK 模式，仅验证检索链路）
"""

from __future__ import annotations

import os
import re
import json
import math
from typing import List, Dict, Tuple

import numpy as np
import faiss


# =====================================================================
# 0. 全局配置（【核心·可复用】配置中心雏形）
# ---------------------------------------------------------------------
# 后续阶段：抽成 app/core/config.py（pydantic-settings），从环境变量/.env 读取，
#          不同部署环境切换供应商无需改代码，只改环境变量。
# =====================================================================
CONFIG: Dict = {
    # ---- LLM 供应商配置（OpenAI 兼容协议，可一键换供应商）----
    "llm": {
        "provider": "openai",            # openai / deepseek / qwen / 自建网关
        "api_key": os.getenv("LLM_API_KEY", ""),
        "base_url": os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
        "model": os.getenv("LLM_MODEL", "gpt-4o-mini"),
        "temperature": 0.2,
        "max_tokens": 1500,
    },
    # ---- Embedding 供应商配置（与 LLM 同源或另配）----
    "embedding": {
        "provider": "openai",
        "api_key": os.getenv("EMB_API_KEY", os.getenv("LLM_API_KEY", "")),
        "base_url": os.getenv("EMB_BASE_URL", "https://api.openai.com/v1"),
        "model": os.getenv("EMB_MODEL", "text-embedding-3-small"),
        "dim": 1536,
    },
    # ---- 检索 / 切片参数 ----
    "chunk_size": 500,                              # 切片字符数
    "top_k": 3,                                     # 混合检索返回条数
    "hybrid_weights": {"bm25": 0.5, "vector": 0.5}, # 混合权重（可调）
    # ---- 文件路径 ----
    "policy_md_path": os.getenv("POLICY_MD", "./审核制度.md"),
    "cache_dir": "./.rag_cache",   # 向量/索引缓存，避免每次重启重算 embedding
}


# =====================================================================
# 1. 知识库载入与切片（【核心·可复用】knowledge/ingest.py）
# ---------------------------------------------------------------------
# 后续阶段：扩展为支持 PDF/Word、按标题层级切片、去重、元信息提取。
# =====================================================================
def load_markdown(path: str) -> str:
    """读取本地 markdown 制度文件，返回纯文本。"""
    if not os.path.exists(path):
        raise FileNotFoundError(f"未找到制度文件: {path}")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    # 去掉代码块（```...```），避免噪声进入检索
    text = re.sub(r"```[\s\S]*?```", " ", text)
    return text


def chunk_text(text: str, size: int = 500) -> List[str]:
    """
    按固定字符数切片（中文场景常用做法）。
    每个切片保留原顺序，便于回溯出处。
    注：生产环境可改为按句子/段落边界切，减少语义截断。
    """
    text = text.strip()
    chunks = [text[i:i + size] for i in range(0, len(text), size)]
    return [c for c in chunks if len(c.strip()) >= 20]  # 过滤过短碎片


# =====================================================================
# 2. BM25 检索器（【核心·可复用】knowledge/bm25.py）
# ---------------------------------------------------------------------
# 自己实现 Okapi BM25，逻辑透明可调试，零额外依赖。
# 后续阶段：可替换为 Elasticsearch/Lucene 的 BM25，接口保持一致。
# =====================================================================
def tokenize(text: str) -> List[str]:
    """
    极简中英混合分词：中文按单字切（CJK 逐个），英文/数字按词切。
    原型阶段足够；生产可换 jieba / 专业分词器。
    """
    return re.findall(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+", text.lower())


class BM25:
    def __init__(self, corpus: List[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = [tokenize(d) for d in corpus]
        self.N = len(self.docs)
        self.avgdl = sum(len(d) for d in self.docs) / max(self.N, 1)
        # 文档频率 df：包含某词的文档数
        self.df = {}
        for doc in self.docs:
            for w in set(doc):
                self.df[w] = self.df.get(w, 0) + 1
        # 预计算 IDF
        self.idf = {
            w: math.log(1 + (self.N - df + 0.5) / (df + 0.5))
            for w, df in self.df.items()
        }

    def get_scores(self, query: str) -> List[float]:
        """返回 query 对每篇文档的 BM25 得分（长度=文档数）。"""
        q_tokens = tokenize(query)
        scores = []
        for doc in self.docs:
            tf = {}
            for w in doc:
                tf[w] = tf.get(w, 0) + 1
            dl = len(doc)
            score = 0.0
            for t in q_tokens:
                if t not in tf:
                    continue
                idf = self.idf.get(t, 0.0)
                denom = tf[t] + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
                score += idf * (tf[t] * (self.k1 + 1)) / denom
            scores.append(score)
        return scores


# =====================================================================
# 3. Embedding 客户端（【核心·可复用】agents/embeddings.py）
# ---------------------------------------------------------------------
# 设计要点：供应商可配置。
#   - 配置了 api_key + base_url → 调用 OpenAI 兼容 embedding 接口
#   - 未配置（离线/原型演示）   → 退化为「哈希词袋」伪向量，
#     保证 FAISS 与混合检索在无网络时也能跑通（仅演示，非真实语义）。
# 后续阶段：生产统一走供应商 embedding；本地化可换 sentence-transformers。
# =====================================================================
class EmbeddingClient:
    def __init__(self, cfg: Dict):
        self.cfg = cfg
        self.dim = cfg.get("dim", 1536)
        self._use_api = bool(cfg.get("api_key"))

    def embed(self, texts: List[str]) -> np.ndarray:
        return self._embed_api(texts) if self._use_api else self._embed_local(texts)

    def _embed_api(self, texts: List[str]) -> np.ndarray:
        """调用 OpenAI 兼容 embedding 接口（供应商可换）。"""
        import requests  # 延迟导入，离线模式无需安装
        url = self.cfg["base_url"].rstrip("/") + "/embeddings"
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {self.cfg['api_key']}"},
            json={"model": self.cfg["model"], "input": texts},
            timeout=60,
        )
        resp.raise_for_status()
        vecs = [np.array(d["embedding"], dtype=np.float32)
                for d in resp.json()["data"]]
        return np.vstack(vecs).astype(np.float32)

    def _embed_local(self, texts: List[str]) -> np.ndarray:
        """
        离线伪向量：哈希词袋 + L2 归一。
        仅用于本地演示检索链路，不具真实语义，切勿用于生产。
        """
        mat = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in tokenize(t):
                mat[i, hash(w) % self.dim] += 1.0
        mat += 1e-3  # 避免全零
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return mat / norms


# =====================================================================
# 4. 向量库（【核心·可复用】knowledge/vector_store.py）
# ---------------------------------------------------------------------
# FAISS IndexFlatIP（内积）+ 向量归一化 ≈ 余弦相似度。
# 后续阶段：可换 Milvus / pgvector，接口保持一致（add / search）。
# =====================================================================
class VectorStore:
    def __init__(self, dim: int):
        self.index = faiss.IndexFlatIP(dim)  # 内积索引
        self.dim = dim

    def add(self, embeddings: np.ndarray) -> None:
        faiss.normalize_L2(embeddings)       # 入索引前归一，保证内积=余弦
        self.index.add(embeddings)

    def search(self, query_vec: np.ndarray, k: int) -> Tuple[np.ndarray, np.ndarray]:
        faiss.normalize_L2(query_vec)
        scores, idxs = self.index.search(query_vec, k)
        return scores, idxs


# =====================================================================
# 5. 混合检索（【核心·可复用】agents/rag_retriever.py）
# ---------------------------------------------------------------------
# BM25(关键词精确) + 向量(语义) 归一化后加权融合。这是 RAG 质量命门。
# =====================================================================
def _minmax(scores: List[float]) -> np.ndarray:
    arr = np.array(scores, dtype=np.float32)
    if arr.max() == arr.min():
        return np.zeros_like(arr)
    return (arr - arr.min()) / (arr.max() - arr.min())


def hybrid_retrieve(query: str, bm25: BM25, store: VectorStore,
                    emb: EmbeddingClient, chunks: List[str],
                    top_k: int, weights: Dict[str, float]) -> List[Tuple[int, float]]:
    # 1) 关键词召回（BM25）
    bm25_scores = _minmax(bm25.get_scores(query))
    # 2) 语义召回（向量）
    q_vec = emb.embed([query])
    vec_scores, vec_idx = store.search(q_vec, k=max(top_k, len(chunks)))
    vec_align = np.zeros(len(chunks), dtype=np.float32)
    for rank, idx in enumerate(vec_idx[0]):
        if idx >= 0:
            vec_align[idx] = vec_scores[0][rank]
    vec_align = _minmax(vec_align.tolist())
    # 3) 加权融合取 top_k
    fused = weights["bm25"] * bm25_scores + weights["vector"] * vec_align
    top = np.argsort(fused)[::-1][:top_k]
    return [(int(i), float(fused[i])) for i in top if fused[i] > 0]


# =====================================================================
# 6. LLM 网关（【核心·可复用】agents/llm_gateway.py）
# ---------------------------------------------------------------------
# 与具体供应商解耦：统一走 OpenAI 兼容 /chat/completions。
# 换供应商只改 CONFIG（base_url + model + api_key），代码不动。
# 无 api_key → mock 模式，便于离线验证检索链路。
# 后续阶段：增加重试/限流/超时/流式/多模型路由。
# =====================================================================
SYSTEM_PROMPT = (
    "你是一名资深商品上架审核专家，熟悉电商平台的商品合规规范、"
    "资质要求与禁售规则。请严格基于下方【审核制度参考片段】作答，"
    "不要编造参考片段之外的规则。\n"
    "若用户输入是「商品资料描述」，请输出：①初步审核意见；"
    "②疑似违规/风险点；③需补充的资质材料。\n"
    "若用户输入是「审核规则问题」，请基于片段给出准确解答并引用出处。"
)


def call_llm(prompt: str, cfg: Dict, system: str = SYSTEM_PROMPT) -> str:
    """调用 LLM 生成审核意见 / 规则解答。供应商可配置。"""
    # —— 离线 mock：未配置密钥时返回检索依据，方便演示检索效果 ——
    if not cfg.get("api_key"):
        start = prompt.find("【审核制度参考片段】")
        end = prompt.find("【用户输入】")
        ctx = prompt[start:end] if start != -1 and end != -1 else prompt
        return ("[离线 MOCK 模式·未配置 LLM_API_KEY，以下为检索到的制度依据]\n"
                + ctx + "\n…(配置 LLM_API_KEY 后此处输出真实模型结论)")

    import requests
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "temperature": cfg.get("temperature", 0.2),
        "max_tokens": cfg.get("max_tokens", 1500),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {cfg['api_key']}"},
        json=payload,
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


# =====================================================================
# 7. 提示词装配（【核心·可复用】agents/prompt_builder.py）
# ---------------------------------------------------------------------
# 把检索到的 top-k 片段拼成上下文，再附用户问题。RAG 效果很大程度取决于此。
# 后续阶段：加入引用标注、片段去重、长度裁剪。
# =====================================================================
def build_prompt(query: str, retrieved: List[Tuple[int, float]],
                 chunks: List[str]) -> str:
    ctx_parts = []
    for rank, (idx, score) in enumerate(retrieved, 1):
        ctx_parts.append(f"[片段{idx}·相关度{score:.2f}]\n{chunks[idx]}")
    context = "\n\n".join(ctx_parts)
    return (f"【审核制度参考片段】\n{context}\n\n"
            f"【用户输入】\n{query}\n\n"
            "请基于上述片段给出回答：")


# =====================================================================
# 8. 知识库构建：切片 + 向量化 + 缓存（【核心·可复用】）
# ---------------------------------------------------------------------
# 缓存到磁盘，避免每次启动重算 embedding；制度文件 mtime 变化自动重建。
# =====================================================================
def build_knowledge_base(cfg: Dict):
    md_path = cfg["policy_md_path"]
    cache_dir = cfg["cache_dir"]
    os.makedirs(cache_dir, exist_ok=True)
    cache_vec = os.path.join(cache_dir, "emb.npy")
    cache_idx = os.path.join(cache_dir, "faiss.index")
    cache_meta = os.path.join(cache_dir, "meta.json")
    md_mtime = os.path.getmtime(md_path)

    # 命中缓存且制度未变 → 直接加载
    if os.path.exists(cache_vec) and os.path.exists(cache_meta):
        meta = json.load(open(cache_meta, encoding="utf-8"))
        if meta.get("md_mtime") == md_mtime:
            chunks = meta["chunks"]
            emb = EmbeddingClient(cfg["embedding"])
            store = VectorStore(emb.dim)
            store.index = faiss.read_index(cache_idx)
            return chunks, store, BM25(chunks), emb

    # 否则重新切片 + 向量化
    text = load_markdown(md_path)
    chunks = chunk_text(text, cfg["chunk_size"])
    emb = EmbeddingClient(cfg["embedding"])
    vectors = emb.embed(chunks)
    store = VectorStore(emb.dim)
    store.add(vectors)

    np.save(cache_vec, vectors)
    faiss.write_index(store.index, cache_idx)
    json.dump({"md_mtime": md_mtime, "chunks": chunks},
              open(cache_meta, "w", encoding="utf-8"), ensure_ascii=False)
    return chunks, store, BM25(chunks), emb


# =====================================================================
# 9. 命令行交互（阶段1 验证入口）
# ---------------------------------------------------------------------
# 后续阶段：本文件的检索/LLM 能力会被 FastAPI 路由 + 异步任务调用，
#          CLI 仅作为离线验证与单测入口保留。
# =====================================================================
def run_cli(cfg: Dict):
    print(">>> 正在构建知识库（首次会调用 embedding，请稍候）...")
    chunks, store, bm25, emb = build_knowledge_base(cfg)
    print(f">>> 知识库就绪：共 {len(chunks)} 个片段。"
          f"输入商品描述或审核问题，输入 exit 退出。\n")

    while True:
        try:
            query = input("审核助手> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not query:
            continue
        if query.lower() in ("exit", "quit", "退出"):
            break

        retrieved = hybrid_retrieve(
            query, bm25, store, emb, chunks,
            cfg["top_k"], cfg["hybrid_weights"]
        )
        if not retrieved:
            print("（未检索到相关片段，请换个说法或直接提问）\n")
            continue

        prompt = build_prompt(query, retrieved, chunks)
        answer = call_llm(prompt, cfg["llm"])
        print("\n===== 初步审核意见 / 规则解答 =====")
        print(answer)
        print("==================================\n")


if __name__ == "__main__":
    run_cli(CONFIG)
