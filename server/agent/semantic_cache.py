# -*- coding: utf-8 -*-
"""
SmartTourism · 语义缓存（Semantic Cache）
==========================================
用「语义相似」而非「字符串相等」命中缓存，重复/近义问题直接复用上次答复，省 token、降延迟。

安全是第一原则——语义缓存最大的坑是「假命中」：
  「帮我规划杭州3日游」和「帮我规划三亚5日游」字面高度相似，若只按相似度命中，
  会把三亚的答案错发给问杭州的人。为此本实现加了**硬实体护栏**：
  城市 + 天数/数字 等关键实体必须完全一致才允许命中，相似度只在实体一致时才生效。
  → 宁可漏命中（miss，安全），也绝不错命中（返回错误答案）。

嵌入器可插拔：
  - TfidfEmbedder（默认）：sklearn HashingVectorizer 字符 n-gram，零模型下载、离线可用；
  - NeuralEmbedder（可选）：sentence-transformers 中文向量，语义更准（需已安装/下载模型）。

用法：把 cached_call_llm 包在 app.call_llm 外层塞进 graph/multi_agent 即可，
带 tools/stream 的调用自动跳过缓存（结构化与流式不宜缓存）。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("agent.semantic_cache")


# ==================== 归一化 & 关键实体抽取 ====================

_CN_NUM = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
           '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}


def _cn_to_int(s: str) -> Optional[int]:
    """把中文/阿拉伯数字串转成 int（支持 十、十几、二十几 的常见写法）。"""
    s = s.strip()
    if s.isdigit():
        return int(s)
    if not s:
        return None
    if s == '十':
        return 10
    if '十' in s:
        parts = s.split('十')
        tens = _CN_NUM.get(parts[0], 1) if parts[0] else 1
        ones = _CN_NUM.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
        return tens * 10 + ones
    if len(s) == 1 and s in _CN_NUM:
        return _CN_NUM[s]
    return None


def normalize(text: str) -> str:
    """小写、去标点与多余空白，用于稳定的相似度比较。"""
    if not text:
        return ""
    text = text.lower().strip()
    text = re.sub(r"[，。！？、；：,.!?;:\s\"'“”‘’（）()\[\]【】]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _cities() -> List[str]:
    """复用意图分类器的城市表，避免重复维护。"""
    try:
        from agent.agent_engine import IntentClassifier  # type: ignore
        return sorted(IntentClassifier.CITIES, key=len, reverse=True)
    except Exception:  # noqa: BLE001
        try:
            from agent_engine import IntentClassifier  # type: ignore
            return sorted(IntentClassifier.CITIES, key=len, reverse=True)
        except Exception:  # noqa: BLE001
            return []


def extract_key_tokens(text: str) -> frozenset:
    """抽取「必须一致才允许命中」的关键实体：城市、天数、显式数字、预算。"""
    tokens: set = set()
    if not text:
        return frozenset(tokens)

    for city in _cities():
        if city and city in text:
            tokens.add(f"city:{city}")

    # 天数：3天 / 三日 / 5日游
    for m in re.findall(r"([0-9]+|[零一二两三四五六七八九十]+)\s*[天日]", text):
        v = _cn_to_int(m)
        if v is not None:
            tokens.add(f"day:{v}")

    # 阿拉伯数字（预算/人数等强区分信息）
    for m in re.findall(r"\d+", text):
        tokens.add(f"num:{int(m)}")

    return frozenset(tokens)


# ==================== 嵌入器 ====================

class TfidfEmbedder:
    """字符 n-gram + L2 归一化的无状态向量化（cosine = 点积）。零模型下载。"""
    name = "tfidf"

    def __init__(self, ngram_range: Tuple[int, int] = (1, 2)) -> None:
        from sklearn.feature_extraction.text import HashingVectorizer
        self._vec = HashingVectorizer(analyzer="char", ngram_range=ngram_range,
                                      norm="l2", alternate_sign=False)

    def embed(self, text: str):
        return self._vec.transform([normalize(text)])

    def similarity(self, va, vb) -> float:
        try:
            return float((va * vb.T).todense()[0, 0])
        except Exception:  # noqa: BLE001
            return 0.0


class NeuralEmbedder:
    """sentence-transformers 中文向量（可选）。构造时加载模型，失败抛异常由工厂兜底。"""
    name = "neural"

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5") -> None:
        from sentence_transformers import SentenceTransformer  # 延迟导入
        self._model = SentenceTransformer(model_name)
        self._cache: Dict[str, Any] = {}

    def embed(self, text: str):
        key = normalize(text)
        if key not in self._cache:
            self._cache[key] = self._model.encode(key, normalize_embeddings=True)
        return self._cache[key]

    def similarity(self, va, vb) -> float:
        try:
            return float(va.dot(vb))
        except Exception:  # noqa: BLE001
            return 0.0


def make_embedder(prefer_neural: bool = False, neural_model: str = "BAAI/bge-small-zh-v1.5"):
    """工厂：优先神经嵌入（若可用），否则回退 TF-IDF。"""
    if prefer_neural:
        try:
            return NeuralEmbedder(neural_model)
        except Exception as e:  # noqa: BLE001
            logger.info("NeuralEmbedder 不可用，回退 TF-IDF: %s", e)
    return TfidfEmbedder()


# ==================== 缓存 ====================

@dataclass
class CacheEntry:
    query: str
    norm: str
    key_tokens: frozenset
    vector: Any
    answer: Any
    meta: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    last_hit: float = field(default_factory=time.time)
    hits: int = 0


class SemanticCache:
    """带实体护栏的语义缓存。"""

    def __init__(self, threshold: float = 0.6, ttl: Optional[float] = 3600.0,
                 embedder: Any = None, max_entries: int = 1000) -> None:
        self.threshold = threshold
        self.ttl = ttl
        self.embedder = embedder or TfidfEmbedder()
        self.max_entries = max_entries
        self._entries: List[CacheEntry] = []
        self.hits = 0
        self.misses = 0
        # 单例缓存被 waitress 多个工作线程（以及后台记忆写入线程）共享：
        # 遍历 _entries 时另一个线程 append/排序会抛 RuntimeError 或读到半截状态。
        # 锁只保护容器与计数，耗时的 embed()/similarity() 一律留在锁外。
        self._lock = threading.RLock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def _expired(self, e: CacheEntry) -> bool:
        return self.ttl is not None and (time.time() - e.ts) > self.ttl

    def get(self, query: str) -> Optional[Dict[str, Any]]:
        """命中返回 {answer, meta, similarity, matched_query}，否则 None。"""
        if not query or not query.strip():
            with self._lock:
                self.misses += 1
            return None
        qtokens = extract_key_tokens(query)
        qvec = self.embedder.embed(query)

        with self._lock:
            # 硬护栏：关键实体（城市/天数/数字）必须完全一致，杜绝跨城市假命中
            candidates = [e for e in self._entries if not self._expired(e)
                          and e.key_tokens == qtokens]

        best: Optional[CacheEntry] = None
        best_sim = -1.0
        for e in candidates:
            sim = self.embedder.similarity(qvec, e.vector)
            if sim >= self.threshold and sim > best_sim:
                best_sim = sim
                best = e

        if best is not None:
            with self._lock:
                self.hits += 1
                best.hits += 1
                best.last_hit = time.time()
                answer, meta = best.answer, dict(best.meta)
            return {"answer": answer, "meta": meta,
                    "similarity": round(best_sim, 4), "matched_query": best.query}
        with self._lock:
            self.misses += 1
        return None

    def set(self, query: str, answer: Any, meta: Optional[Dict[str, Any]] = None) -> None:
        if not query or not query.strip() or answer is None:
            return
        entry = CacheEntry(
            query=query, norm=normalize(query),
            key_tokens=extract_key_tokens(query),
            vector=self.embedder.embed(query),
            answer=answer, meta=meta or {},
        )
        with self._lock:
            self._entries.append(entry)
            self._evict()

    def _evict(self) -> None:
        # 调用方持锁：先清过期，再按 LRU 裁到上限
        if self.ttl is not None:
            self._entries = [e for e in self._entries if not self._expired(e)]
        while len(self._entries) > self.max_entries:
            self._entries.sort(key=lambda e: e.last_hit)
            self._entries.pop(0)

    def forget(self, query: str) -> int:
        """丢弃某个问句的缓存。答复里含实时数据（天气/价格）不该被复用时调用。返回删除条数。"""
        if not query or not query.strip():
            return 0
        norm_q = normalize(query)
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if e.norm != norm_q]
            return before - len(self._entries)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            size, hits, misses = len(self._entries), self.hits, self.misses
        total = hits + misses
        return {
            "size": size,
            "hits": hits,
            "misses": misses,
            "hit_rate": round(hits / total, 3) if total else 0.0,
            "embedder": getattr(self.embedder, "name", "unknown"),
        }

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self.hits = 0
            self.misses = 0


# ==================== call_llm 包装器 ====================

def _last_user_text(messages: List[Dict[str, Any]]) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):  # 多模态
                return " ".join(str(p.get("text", "")) for p in c if isinstance(p, dict))
    return ""


def cached_call_llm(call_llm_fn: Callable[..., Dict[str, Any]], cache: SemanticCache,
                    key_on_skill: bool = True) -> Callable[..., Dict[str, Any]]:
    """包装 call_llm：先查语义缓存，未命中再真实调用并回填。

    带 tools / stream 的调用跳过缓存（结构化输出与流式响应不宜复用）。
    签名与 app.call_llm 完全一致，可直接替换。
    """

    def wrapped(messages, stream: bool = False, skill_name: str = "",
                tools: Optional[list] = None, tool_choice=None) -> Dict[str, Any]:
        if stream or tools:
            return call_llm_fn(messages, stream=stream, skill_name=skill_name,
                               tools=tools, tool_choice=tool_choice)

        user_text = _last_user_text(messages)
        cache_key = f"[{skill_name}] {user_text}" if key_on_skill else user_text
        if not user_text:
            return call_llm_fn(messages, stream=stream, skill_name=skill_name)

        hit = cache.get(cache_key)
        if hit is not None:
            logger.info("语义缓存命中(sim=%.3f): %s", hit["similarity"], user_text[:30])
            cached = dict(hit["answer"])
            cached["_cache"] = {"hit": True, "similarity": hit["similarity"]}
            return cached

        resp = call_llm_fn(messages, stream=stream, skill_name=skill_name)
        if isinstance(resp, dict) and "error" not in resp:
            cache.set(cache_key, resp, meta={"skill": skill_name})
        return resp

    return wrapped
