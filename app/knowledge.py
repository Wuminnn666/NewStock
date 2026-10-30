# -*- coding: utf-8 -*-
"""知识库：文档解析、切片、向量化、BM25 + FAISS 混合检索。"""
import hashlib
import io
import json
import math
import re
import threading

import faiss
import numpy as np

from .config import settings


# ---------------- 文本处理 ----------------
def tokenize(text: str):
    """中英混合极简分词：中文单字 + 英文/数字词。"""
    return re.findall(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+", text.lower())


def chunk_text(text: str, size: int = 500):
    """按固定字符数切片。"""
    text = (text or "").strip()
    chunks = [text[i:i + size] for i in range(0, len(text), size)]
    return [c for c in chunks if len(c.strip()) >= 20]


def parse_markdown(raw: bytes) -> str:
    return raw.decode("utf-8", errors="ignore")


def parse_pdf(raw: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(raw))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


# ---------------- Embedding ----------------
class EmbeddingClient:
    """供应商可配置：有 key 走 API，否则退化为哈希词袋（仅演示）。"""

    def __init__(self, dim: int | None = None):
        self.api_key = settings.EMB_API_KEY
        self.dim = dim or settings.EMB_DIM

    def embed(self, texts):
        if self.api_key:
            try:
                import httpx
                url = settings.EMB_BASE_URL.rstrip("/") + "/embeddings"
                with httpx.Client(timeout=60) as client:
                    resp = client.post(
                        url,
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json={"model": settings.EMB_MODEL, "input": texts},
                    )
                    resp.raise_for_status()
                    data = resp.json()["data"]
                    arr = np.vstack([np.array(d["embedding"], dtype=np.float32) for d in data])
                    self.dim = arr.shape[1]
                    return arr.astype(np.float32)
            except Exception:
                pass  # 失败则降级到本地伪向量
        return self._local(texts)

    def _local(self, texts):
        mat = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in tokenize(t):
                # 用稳定哈希（非 Python hash），保证重启后向量可复现
                idx = int(hashlib.md5(w.encode("utf-8")).hexdigest(), 16) % self.dim
                mat[i, idx] += 1.0
        mat += 1e-3
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return mat / norms


# ---------------- BM25 ----------------
class BM25:
    def __init__(self, corpus, k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        self.docs = [tokenize(d) for d in corpus]
        self.N = len(self.docs)
        self.avgdl = sum(len(d) for d in self.docs) / max(self.N, 1)
        self.df = {}
        for doc in self.docs:
            for w in set(doc):
                self.df[w] = self.df.get(w, 0) + 1
        self.idf = {w: math.log(1 + (self.N - df + 0.5) / (df + 0.5)) for w, df in self.df.items()}

    def get_scores(self, query):
        q = tokenize(query)
        scores = []
        for doc in self.docs:
            tf = {}
            for w in doc:
                tf[w] = tf.get(w, 0) + 1
            dl = len(doc)
            s = 0.0
            for t in q:
                if t not in tf:
                    continue
                idf = self.idf.get(t, 0.0)
                denom = tf[t] + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
                s += idf * (tf[t] * (self.k1 + 1)) / denom
            scores.append(s)
        return scores


def _minmax(scores):
    arr = np.array(scores, dtype=np.float32)
    if arr.size == 0 or arr.max() == arr.min():
        return np.zeros_like(arr)
    return (arr - arr.min()) / (arr.max() - arr.min())


# ---------------- 索引管理器 ----------------
class IndexState:
    def __init__(self):
        self.lock = threading.Lock()
        self.chunks = []      # [{id, rule_doc_id, file, chunk_index, content}]
        self.bm25 = None
        self.index = None
        self.dim = settings.EMB_DIM

    def build(self, db):
        """从数据库重建 BM25 与 FAISS 索引（上传/删除后调用）。"""
        from .db import KnowledgeChunk, RuleDoc
        with self.lock:
            rows = db.query(KnowledgeChunk).order_by(KnowledgeChunk.id.asc()).all()
            docs = {d.id: d for d in db.query(RuleDoc).all()}
            chunks, vecs = [], []
            for r in rows:
                doc = docs.get(r.rule_doc_id)
                chunks.append({
                    "id": r.id, "rule_doc_id": r.rule_doc_id,
                    "file": doc.filename if doc else "未知",
                    "chunk_index": r.chunk_index, "content": r.content,
                })
                if r.embedding:
                    vecs.append(np.frombuffer(r.embedding, dtype=np.float32))
            self.chunks = chunks
            if not chunks:
                self.bm25, self.index = None, None
                return
            self.bm25 = BM25([c["content"] for c in chunks])
            if vecs and len(vecs) == len(chunks):
                arr = np.vstack(vecs).astype(np.float32)
                faiss.normalize_L2(arr)
                self.index = faiss.IndexFlatIP(arr.shape[1])
                self.index.add(arr)
                self.dim = arr.shape[1]
            else:
                self.index = None

    def retrieve(self, query, top_k=None, w_bm25=None, w_vec=None):
        top_k = top_k or settings.TOP_K
        w_bm25 = settings.HYBRID_BM25 if w_bm25 is None else w_bm25
        w_vec = settings.HYBRID_VECTOR if w_vec is None else w_vec
        with self.lock:
            n = len(self.chunks)
            if n == 0:
                return []
            bm = _minmax(self.bm25.get_scores(query)) if self.bm25 else np.zeros(n, dtype=np.float32)
            vec = np.zeros(n, dtype=np.float32)
            if self.index is not None:
                q = EmbeddingClient(dim=self.dim).embed([query]).astype(np.float32)
                if q.shape[1] == self.dim:
                    faiss.normalize_L2(q)
                    scores, idxs = self.index.search(q, min(max(top_k, 1), n))
                    for rank, idx in enumerate(idxs[0]):
                        if idx >= 0:
                            vec[idx] = scores[0][rank]
                    vec = _minmax(vec.tolist())
            fused = w_bm25 * np.array(bm, dtype=np.float32) + w_vec * vec
            order = np.argsort(fused)[::-1][:top_k]
            out = []
            for i in order:
                if fused[i] <= 0:
                    continue
                c = dict(self.chunks[i])
                c["score"] = float(fused[i])
                out.append(c)
            return out


index_state = IndexState()


# ---------------- 入库 / 删除 ----------------
def ingest_document(db, filename: str, raw: bytes, category: str, title: str | None = None):
    """解析 -> 切片 -> 向量化 -> 落库。返回 RuleDoc。"""
    from .db import RuleDoc, KnowledgeChunk

    text = parse_pdf(raw) if filename.lower().endswith(".pdf") else parse_markdown(raw)
    chunks = chunk_text(text, settings.CHUNK_SIZE)
    if not chunks:
        raise ValueError("文档内容为空或无法解析")

    doc = RuleDoc(title=title or filename, filename=filename, category=category,
                  chunk_count=len(chunks), status="已生效")
    db.add(doc)
    db.commit()
    db.refresh(doc)

    vecs = EmbeddingClient().embed(chunks)
    for i, (content, vec) in enumerate(zip(chunks, vecs)):
        db.add(KnowledgeChunk(
            rule_doc_id=doc.id, chunk_index=i, content=content,
            embedding=vec.astype(np.float32).tobytes(),
            meta=json.dumps({"file": filename, "chunk_no": i + 1}, ensure_ascii=False),
        ))
    db.commit()
    return doc


def delete_document(db, doc_id: int):
    from .db import KnowledgeChunk, RuleDoc
    db.query(KnowledgeChunk).filter(KnowledgeChunk.rule_doc_id == doc_id).delete()
    db.query(RuleDoc).filter(RuleDoc.id == doc_id).delete()
    db.commit()
