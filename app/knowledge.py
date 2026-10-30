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
# 中文分词：优先 jieba（词级），没装则退化为二元组。
# 为什么必须上词级？
#    单字/二元组切分会让「商品」「上架」这类高频字在任意文档里都命中，
#    BM25 失去区分度——问「图书 ISBN」和问「食品资质」得分几乎一样，
#    检索层等于没在工作。词级切分才能让「图书」「直播」「家政」这些
#    语料中根本不存在的词自然拿不到 IDF，从而具备「查不到就说查不到」的能力。
try:                                  # jieba 是纯 Python 包，缺失时不影响启动
    import jieba

    jieba.setLogLevel(60)             # 关掉 "Building prefix dict" 之类的日志噪音
    _HAS_JIEBA = True
except Exception:                     # pragma: no cover
    _HAS_JIEBA = False

# 检索/覆盖判定时忽略的功能词（问句骨架，不承载「问的是什么」）
STOPWORDS = {
    "商品", "上架", "审核", "需要", "提交", "提供", "请问", "什么", "哪些", "哪个",
    "可以", "怎么", "如何", "是否", "有没有", "以及", "或者", "这个", "那个", "这款",
    "要求", "标准", "问题", "一下", "帮我", "麻烦", "具体", "相关", "情况", "时候",
    "多久", "多少", "属于", "范围", "内容", "变化", "我的", "我们", "你们", "一个",
}


def tokenize(text: str):
    """中英混合分词：中文走 jieba 词级（兜底二元组）+ 英文/数字词。"""
    text = (text or "").lower()
    out = []
    for run in re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+", text):
        if re.match(r"[a-zA-Z0-9]", run):
            out.append(run)
        elif _HAS_JIEBA:
            out.extend(w for w in jieba.cut_for_search(run) if w.strip())
        elif len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return out


def content_words(text: str):
    """抽取「内容词」——去掉问句功能词后的实义词，用于判断检索是否有依据。"""
    out = []
    for w in tokenize(text):
        if len(w) < 2 or w in STOPWORDS or w.isdigit():
            continue
        if w not in out:
            out.append(w)
    return out


# 不能当「主语」的词：否定词、情态词、动作词、疑问代词。
# 「我没办食品经营许可证，能不能先上架后补」如果把"没办"当主语，
# 就会判成"规则里没有"——而制度其实明确说了"过期/缺证视为不合规"。
NON_SUBJECT = {
    "没办", "没有", "未办", "不能", "可以", "能否", "是否", "需要", "要交", "后补",
    "先上", "办理", "申请", "提供", "提交", "准备", "了解", "知道", "哪里", "哪些",
    "什么", "为什么", "怎么", "如何", "规定", "我想", "一款", "一个", "这个", "那个",
}

# 一份制度里「给出资质清单」的段落通常长这样：多个证照名并列
_MATERIAL_HINT = ("许可证", "证书", "报告", "凭证", "授权书", "备案")


def analyze_support(query: str, texts):
    """判断「检索到的条文到底能不能回答这个问题」（检索置信度）。

    为什么需要它？
        向量检索永远会返回 top-k，哪怕库里一个字都不相关——HTTP 200 + 三条
        片段，看起来像有依据，实则全是噪声。缺了这个判据，模型只会硬编答案，
        这正是「陷阱题答不出“规则中未找到”」的根因。

    判据（两条，都很朴素但可解释）：
        1) 问题的核心实义词要有一定比例出现在条文里（覆盖率）；
        2) 如果问的是「要交什么材料」，那么类目主体词必须出现在
           **给出清单的那一段**里——否则就是"提到了这个词，但没回答这个问题"。
           例：制度在"禁售规则"里提过"无证医疗器械"，但资质清单段落只列了
           食品/化妆品/3C/服饰，所以问"二类医疗器械备案要交什么"必须答未找到。

    返回：{"supported": bool, "coverage": float, "subject": str,
           "missing": [...], "reason": str}
    """
    words = content_words(query)
    if not texts:
        return {"supported": False, "coverage": 0.0, "subject": "",
                "missing": words, "reason": "知识库中没有可检索的条文"}
    if not words:
        return {"supported": True, "coverage": 1.0, "subject": "",
                "missing": [], "reason": ""}

    joined = "\n".join(texts).lower()      # 统一小写，"3C" 与 "3c" 才算同一个词
    # jieba 会把「食品类」切成「食品类/食品/类」，用子串兜底避免漏命中
    hit = [w for w in words if w in joined]
    coverage = len(hit) / len(words)

    # 主语 = 第一个「实义名词」，跳过否定/情态/动作词（见 NON_SUBJECT 说明）
    subject = next((w for w in words if w not in NON_SUBJECT), words[0])
    subject_found = subject in joined

    # 「要交什么材料」类问题：主语必须出现在「给出清单」的那一段。
    # 触发条件必须收紧——「证件类图片支持什么格式」虽然含"证件"，但它问的是格式，
    # 不是问要交哪些证件，不能误判成材料类问题。
    ask_material = any(k in query for k in (
        "需要提交", "要提交", "提交哪些", "需要提供", "要提供", "需要交", "要交",
        "哪些材料", "什么材料", "哪些资质", "什么资质", "准备哪些", "需要哪些",
    ))
    if ask_material:
        listing = [t for t in texts if sum(t.count(h) for h in _MATERIAL_HINT) >= 2]
        if listing and not any(subject in t.lower() for t in listing):
            return {"supported": False, "coverage": coverage, "subject": subject,
                    "missing": [w for w in words if w not in hit],
                    "reason": f"条文未在资质清单中列出「{subject}」"}

    if not subject_found:
        return {"supported": False, "coverage": coverage, "subject": subject,
                "missing": [w for w in words if w not in hit],
                "reason": f"规则中未出现「{subject}」相关条文"}
    if coverage < 0.34:
        return {"supported": False, "coverage": coverage, "subject": subject,
                "missing": [w for w in words if w not in hit],
                "reason": "问题主体与检索条文匹配度过低"}
    return {"supported": True, "coverage": coverage, "subject": subject,
            "missing": [], "reason": ""}


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
