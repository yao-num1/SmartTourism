# -*- coding: utf-8 -*-
"""
SmartTourism · 混合检索 RAG 升级（Hybrid RAG + Rerank + Query 改写 + CRAG）
============================================================================
在原 rag_engine.py（纯向量、英文 MiniLM）之上做工程升级，**不改动原文件**：

  1. 中文向量：bge-small-zh（NeuralEmbedder），比 all-MiniLM 更懂中文；
  2. 混合检索：BM25（jieba 分词，稀疏/关键词强）+ 稠密向量（语义强），用 RRF 融合，
     兼顾「精确关键词命中」与「语义近似」——单靠任一路都会漏；
  3. 重排（rerank）：对融合候选再打分排序，默认离线词面重排，可选 CrossEncoder 神经重排；
  4. Query 改写：规则扩展（城市别名/同义词）+ 可选 LLM 多查询，提升召回；
  5. CRAG（纠错式 RAG）：对检索结果做质量分级，低质则触发「改写重检 / 兜底」，
     并如实标注可信度，避免把不相关内容硬塞进 prompt 造成幻觉。

全部组件可插拔、可离线（测试用 MockEmbedder，零下载）；生产用 NeuralEmbedder。
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("agent.rag_hybrid")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ==================== 中文分词 ====================

def zh_tokenize(text: str) -> List[str]:
    """jieba 中文分词（不可用时回退到字符 bigram），去停用词与标点。"""
    if not text:
        return []
    text = text.lower()
    try:
        import jieba  # type: ignore
        toks = [t.strip() for t in jieba.lcut(text) if t.strip()]
    except Exception:  # noqa: BLE001
        toks = re.findall(r"[\u4e00-\u9fff]|[a-z0-9]+", text)
    return [t for t in toks if t and t not in _STOPWORDS and not re.fullmatch(r"[\W_]+", t)]


_STOPWORDS = set("的 了 和 是 在 我 有 就 不 人 都 一 一个 上 也 很 到 说 要 去 会 着 没有 好 自己 这 那 你 请问 什么 怎么 如何 吗 呢 啊".split())


# ==================== 嵌入器 ====================

class BaseEmbedder:
    name = "base"
    dim = 0

    def encode(self, texts: List[str]) -> np.ndarray:  # pragma: no cover
        raise NotImplementedError


class MockEmbedder(BaseEmbedder):
    """确定性哈希嵌入（测试用，零下载、可复现）。词袋哈希 → L2 归一化。"""
    name = "mock"

    def __init__(self, dim: int = 64) -> None:
        self.dim = dim

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        for tok in zh_tokenize(text):
            h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
            v[h % self.dim] += 1.0
        # 字符 bigram 兜底，增强近义区分
        for i in range(len(text) - 1):
            bg = text[i:i + 2]
            h = int(hashlib.md5(bg.encode("utf-8")).hexdigest(), 16)
            v[h % self.dim] += 0.5
        n = np.linalg.norm(v)
        return v / n if n > 0 else v

    def encode(self, texts: List[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.vstack([self._vec(t) for t in texts])


class NeuralEmbedder(BaseEmbedder):
    """bge-small-zh 中文向量（生产用）。首次构造会加载/下载模型。"""
    name = "neural"

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5") -> None:
        from sentence_transformers import SentenceTransformer  # 延迟导入
        self._model = SentenceTransformer(model_name)
        get_dim = getattr(self._model, "get_embedding_dimension",
                          None) or self._model.get_sentence_embedding_dimension
        self.dim = get_dim()
        self.model_name = model_name

    def encode(self, texts: List[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        emb = self._model.encode(texts, normalize_embeddings=True,
                                 convert_to_numpy=True, show_progress_bar=False)
        return np.asarray(emb, dtype=np.float32)


def make_embedder(prefer_neural: bool = True, model_name: str = "BAAI/bge-small-zh-v1.5") -> BaseEmbedder:
    try:
        if prefer_neural:
            return NeuralEmbedder(model_name)
    except Exception as e:  # noqa: BLE001
        logger.info("NeuralEmbedder 不可用，回退 MockEmbedder: %s", e)
    return MockEmbedder()


# ==================== BM25 稀疏索引 ====================

class BM25Index:
    def __init__(self) -> None:
        self._bm25 = None
        self._corpus_tokens: List[List[str]] = []

    def build(self, documents: List[str]) -> None:
        from rank_bm25 import BM25Okapi  # 延迟导入
        self._corpus_tokens = [zh_tokenize(d) or ["<empty>"] for d in documents]
        self._bm25 = BM25Okapi(self._corpus_tokens)

    def search(self, query: str, top_k: int) -> List[Tuple[int, float]]:
        if self._bm25 is None:
            return []
        q = zh_tokenize(query) or ["<empty>"]
        scores = self._bm25.get_scores(q)
        order = np.argsort(scores)[::-1][:top_k]
        return [(int(i), float(scores[i])) for i in order if scores[i] > 0]


# ==================== 稠密索引（内存 numpy） ====================

class DenseIndex:
    def __init__(self, embedder: BaseEmbedder) -> None:
        self.embedder = embedder
        self._mat: Optional[np.ndarray] = None

    def build(self, documents: List[str]) -> None:
        self._mat = self.embedder.encode(documents) if documents else np.zeros((0, self.embedder.dim))

    def search(self, query: str, top_k: int) -> List[Tuple[int, float]]:
        if self._mat is None or self._mat.shape[0] == 0:
            return []
        qv = self.embedder.encode([query])[0]
        sims = self._mat @ qv  # 已归一化 → cosine
        order = np.argsort(sims)[::-1][:top_k]
        return [(int(i), float(sims[i])) for i in order if sims[i] > 0]


# ==================== 检索结果 ====================

@dataclass
class RetrievedDoc:
    text: str
    score: float
    metadata: Dict[str, Any] = field(default_factory=dict)
    bm25_rank: Optional[int] = None
    dense_rank: Optional[int] = None


# ==================== 重排器 ====================

class LexicalReranker:
    """离线词面重排：查询与文档的 token 覆盖率 + 原融合分，零下载。"""
    name = "lexical"

    def rerank(self, query: str, docs: List[RetrievedDoc], top_k: int) -> List[RetrievedDoc]:
        q = set(zh_tokenize(query))
        if not q:
            return docs[:top_k]
        for d in docs:
            dt = set(zh_tokenize(d.text))
            overlap = len(q & dt) / len(q)
            d.score = round(0.6 * d.score + 0.4 * overlap, 4)
        docs.sort(key=lambda d: d.score, reverse=True)
        return docs[:top_k]


class CrossEncoderReranker:
    """神经重排（可选，需下载 bge-reranker）。失败自动回退词面重排。"""
    name = "cross_encoder"

    def __init__(self, model_name: str = "BAAI/bge-reranker-base") -> None:
        from sentence_transformers import CrossEncoder  # 延迟导入
        self._ce = CrossEncoder(model_name)
        self._fallback = LexicalReranker()

    def rerank(self, query: str, docs: List[RetrievedDoc], top_k: int) -> List[RetrievedDoc]:
        if not docs:
            return docs
        try:
            pairs = [[query, d.text[:512]] for d in docs]
            scores = self._ce.predict(pairs)
            for d, s in zip(docs, scores):
                d.score = float(s)
            docs.sort(key=lambda d: d.score, reverse=True)
            return docs[:top_k]
        except Exception as e:  # noqa: BLE001
            logger.debug("CrossEncoder 重排失败，回退词面: %s", e)
            return self._fallback.rerank(query, docs, top_k)


# ==================== 混合检索器 ====================

def _rrf(rank_lists: List[List[int]], k: int = 60) -> Dict[int, float]:
    """Reciprocal Rank Fusion：score(d)=Σ 1/(k+rank)。"""
    fused: Dict[int, float] = {}
    for ranks in rank_lists:
        for rank, idx in enumerate(ranks):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (k + rank + 1)
    return fused


class HybridRetriever:
    """BM25 + 稠密向量 混合检索（RRF 融合）+ 可选重排。"""

    def __init__(self, documents: Optional[List[str]] = None,
                 metadatas: Optional[List[Dict[str, Any]]] = None,
                 embedder: Optional[BaseEmbedder] = None,
                 reranker: Any = None,
                 use_bm25: bool = True, use_dense: bool = True) -> None:
        self.documents = documents or []
        self.metadatas = metadatas or [{} for _ in self.documents]
        self.embedder = embedder or MockEmbedder()
        self.reranker = reranker or LexicalReranker()
        self.use_bm25 = use_bm25
        self.use_dense = use_dense
        self._bm25 = BM25Index() if use_bm25 else None
        self._dense = DenseIndex(self.embedder) if use_dense else None
        if self.documents:
            self.build(self.documents, self.metadatas)

    def build(self, documents: List[str], metadatas: Optional[List[Dict[str, Any]]] = None) -> None:
        self.documents = documents
        self.metadatas = metadatas or [{} for _ in documents]
        if self._bm25 is not None:
            self._bm25.build(documents)
        if self._dense is not None:
            self._dense.build(documents)

    def retrieve(self, query: str, top_k: int = 5, candidate_k: Optional[int] = None,
                 rerank: bool = True) -> List[RetrievedDoc]:
        if not self.documents:
            return []
        ck = candidate_k or max(top_k * 4, 20)
        rank_lists: List[List[int]] = []
        bm25_hits = dense_hits = []
        if self._bm25 is not None:
            bm25_hits = self._bm25.search(query, ck)
            rank_lists.append([i for i, _ in bm25_hits])
        if self._dense is not None:
            dense_hits = self._dense.search(query, ck)
            rank_lists.append([i for i, _ in dense_hits])

        fused = _rrf(rank_lists)
        if not fused:
            return []

        bm25_pos = {i: r for r, (i, _) in enumerate(bm25_hits)}
        dense_pos = {i: r for r, (i, _) in enumerate(dense_hits)}

        docs = [RetrievedDoc(text=self.documents[i], score=round(s, 5),
                             metadata=self.metadatas[i],
                             bm25_rank=bm25_pos.get(i), dense_rank=dense_pos.get(i))
                for i, s in fused.items()]
        docs.sort(key=lambda d: d.score, reverse=True)

        if rerank and self.reranker is not None:
            return self.reranker.rerank(query, docs, top_k)
        return docs[:top_k]


# ==================== Query 改写 ====================

_CITY_ALIAS = {"杭州": ["西湖", "杭城"], "北京": ["故宫", "京城"], "成都": ["蓉城", "熊猫"],
               "三亚": ["海边", "度假"], "大理": ["苍山", "洱海"]}
_SYNONYMS = {"景点": ["景区", "游玩", "好玩"], "酒店": ["住宿", "宾馆", "民宿"],
             "美食": ["吃的", "餐厅", "小吃"], "门票": ["票价", "价格"], "便宜": ["实惠", "性价比", "经济"]}


class QueryRewriter:
    """规则扩展 + 可选 LLM 多查询改写，提升召回。"""

    def __init__(self, call_llm_fn: Optional[Callable] = None) -> None:
        self.call_llm_fn = call_llm_fn

    def expand(self, query: str) -> List[str]:
        """规则改写：追加城市别名与同义词，生成若干变体（含原句）。"""
        variants = [query]
        extra_terms: List[str] = []
        for city, alias in _CITY_ALIAS.items():
            if city in query:
                extra_terms.extend(alias)
            for a in alias:
                if a in query:
                    extra_terms.append(city)
        for w, syns in _SYNONYMS.items():
            if w in query:
                extra_terms.extend(syns)
        if extra_terms:
            uniq = list(dict.fromkeys(t for t in extra_terms if t not in query))[:4]
            if uniq:
                variants.append(f"{query} {' '.join(uniq)}")
        return variants

    def rewrite(self, query: str, max_variants: int = 3) -> List[str]:
        """优先 LLM 多查询；不可用则回退规则扩展。"""
        if self.call_llm_fn is not None:
            try:
                prompt = (
                    "把下面的旅游查询改写成最多3个不同角度的检索式，用于知识库检索，"
                    "每行一个，不要编号不要解释。\n查询：" + query
                )
                resp = self.call_llm_fn([{"role": "user", "content": prompt}])
                if isinstance(resp, dict) and "error" not in resp:
                    content = resp["choices"][0]["message"]["content"]
                    lines = [ln.strip(" -•\t") for ln in content.splitlines() if ln.strip()]
                    lines = [re.sub(r"^\d+[.)、]\s*", "", ln) for ln in lines if ln]
                    if lines:
                        out = list(dict.fromkeys([query] + lines))
                        return out[:max_variants]
            except Exception as e:  # noqa: BLE001
                logger.debug("LLM 改写失败，回退规则: %s", e)
        return self.expand(query)[:max_variants]


# ==================== CRAG（纠错式 RAG） ====================

@dataclass
class CRAGResult:
    context: str
    docs: List[RetrievedDoc]
    grade: str                 # sufficient / partial / insufficient
    action: str                # use / rewrite / fallback
    best_score: float = 0.0


class CRAGPipeline:
    """检索 → 质量分级 → 纠错（改写重检 / 兜底）→ 生成上下文。"""

    def __init__(self, retriever: HybridRetriever,
                 rewriter: Optional[QueryRewriter] = None,
                 sufficiency_threshold: float = 0.25,
                 partial_threshold: float = 0.12) -> None:
        self.retriever = retriever
        self.rewriter = rewriter or QueryRewriter()
        self.sufficiency = sufficiency_threshold
        self.partial = partial_threshold

    def _grade(self, docs: List[RetrievedDoc]) -> Tuple[str, float]:
        if not docs:
            return "insufficient", 0.0
        best = max(d.score for d in docs)
        if best >= self.sufficiency:
            return "sufficient", best
        if best >= self.partial:
            return "partial", best
        return "insufficient", best

    def run(self, query: str, top_k: int = 4) -> CRAGResult:
        docs = self.retriever.retrieve(query, top_k=top_k)
        grade, best = self._grade(docs)

        if grade == "insufficient":
            # 纠错：改写后重检
            rewritten = [v for v in self.rewriter.rewrite(query) if v != query]
            merged: Dict[str, RetrievedDoc] = {d.text: d for d in docs}
            for v in rewritten:
                for d in self.retriever.retrieve(v, top_k=top_k):
                    if d.text not in merged or d.score > merged[d.text].score:
                        merged[d.text] = d
            docs = sorted(merged.values(), key=lambda d: d.score, reverse=True)[:top_k]
            grade, best = self._grade(docs)
            action = "rewrite"
        else:
            action = "use"

        if grade == "insufficient":
            action = "fallback"
            return CRAGResult(
                context="", docs=docs, grade=grade, action=action, best_score=best)

        context = self._render(query, docs, grade)
        return CRAGResult(context=context, docs=docs, grade=grade,
                          action=action, best_score=round(best, 4))

    @staticmethod
    def _render(query: str, docs: List[RetrievedDoc], grade: str) -> str:
        lines = ["【知识库检索结果】"]
        for i, d in enumerate(docs, 1):
            src = d.metadata.get("source", d.metadata.get("city", "本地知识库"))
            lines.append(f"{i}. [{src}] {d.text[:300]}")
        if grade == "partial":
            lines.append("（相关度一般，以下资料仅供参考，关键价格/时间请二次确认）")
        return "\n".join(lines)


# ==================== 语料构建（真实本地知识库） ====================

def build_corpus_from_local_knowledge() -> Tuple[List[str], List[Dict[str, Any]]]:
    """把 LOCAL_KNOWLEDGE 拍平成可检索文本文档（一实体一文档），离线可用。"""
    try:
        if _ROOT not in sys.path:
            sys.path.insert(0, _ROOT)
        from plugins.travel_data import LOCAL_KNOWLEDGE  # type: ignore
    except Exception as e:  # noqa: BLE001
        logger.warning("无法加载 LOCAL_KNOWLEDGE: %s", e)
        return [], []

    docs: List[str] = []
    metas: List[Dict[str, Any]] = []
    for city, data in (LOCAL_KNOWLEDGE or {}).items():
        if not isinstance(data, dict):
            continue
        for h in data.get("hotels", []) or []:
            text = (f"{city} {h.get('name','')} 酒店 {h.get('stars','')}星 "
                    f"价格区间{h.get('price_range','')} 评分{h.get('rating','')} "
                    f"位置{h.get('location','')} 特色{'、'.join(h.get('features',[]) or [])}")
            docs.append(text)
            metas.append({"city": city, "type": "hotel", "name": h.get("name", ""), "source": "本地知识库"})
        for s in data.get("scenic_spots", []) or []:
            text = (f"{city} {s.get('name','')} 景点 {s.get('level','')} "
                    f"门票{s.get('ticket','')} 游玩时长{s.get('duration','')} "
                    f"最佳季节{s.get('best_season','')} 亮点{'、'.join(s.get('highlights',[]) or [])}")
            docs.append(text)
            metas.append({"city": city, "type": "scenic", "name": s.get("name", ""), "source": "本地知识库"})
        for f in data.get("food", []) or []:
            text = (f"{city} {f.get('name','')} 美食 {f.get('type','')} "
                    f"人均{f.get('avg_price','')} 评分{f.get('rating','')} "
                    f"招牌{'、'.join(f.get('must_try',[]) or [])} 提示{f.get('tips','')}")
            docs.append(text)
            metas.append({"city": city, "type": "food", "name": f.get("name", ""), "source": "本地知识库"})
        t = data.get("transport") or {}
        if isinstance(t, dict) and t:
            text = f"{city} 交通 " + " ".join(f"{k}:{v}" for k, v in t.items())
            docs.append(text)
            metas.append({"city": city, "type": "transport", "name": f"{city}交通", "source": "本地知识库"})
    return docs, metas


def build_default_retriever(prefer_neural: bool = False, use_reranker: bool = False) -> HybridRetriever:
    """便捷工厂：用真实本地知识库构建混合检索器（默认离线 Mock 嵌入，可切神经）。"""
    docs, metas = build_corpus_from_local_knowledge()
    embedder = make_embedder(prefer_neural=prefer_neural)
    reranker = CrossEncoderReranker() if use_reranker else LexicalReranker()
    return HybridRetriever(docs, metas, embedder=embedder, reranker=reranker)
