# -*- coding: utf-8 -*-
"""
混合检索 RAG 升级 单元测试
==========================
用确定性 MockEmbedder（零下载、可复现）验证：中文分词、BM25、稠密检索、RRF 融合、
混合检索排序、词面重排、Query 改写（规则+LLM+回退）、CRAG 分级与纠错（use/rewrite/fallback）、
真实本地知识库语料构建与检索。

运行：  python server/agent/test_rag_hybrid.py
"""

from __future__ import annotations

import os
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVER = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_SERVER)
for p in (_SERVER, _ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from agent.rag_hybrid import (  # noqa: E402
    zh_tokenize, MockEmbedder, BM25Index, DenseIndex, HybridRetriever,
    LexicalReranker, QueryRewriter, CRAGPipeline, _rrf,
    build_corpus_from_local_knowledge, RetrievedDoc,
)

_PASS = 0
_FAIL = 0


def check(name, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ✅ {name}")
    else:
        _FAIL += 1
        print(f"  ❌ {name}  {extra}")


_CORPUS = [
    "杭州西湖国宾馆 五星级酒店 西湖景观 园林式",
    "三亚海边度假村 海景房 潜水 亲子",
    "成都熊猫基地 门票 亲子游玩",
    "杭州楼外楼 杭帮菜 西湖醋鱼 餐厅",
]
_META = [{"city": "杭州", "type": "hotel"}, {"city": "三亚", "type": "hotel"},
         {"city": "成都", "type": "scenic"}, {"city": "杭州", "type": "food"}]


def test_tokenize():
    print("\n[中文分词]")
    toks = zh_tokenize("杭州有什么好玩的景点")
    check("分出词", len(toks) >= 2, str(toks))
    check("去停用词", "什么" not in toks and "的" not in toks, str(toks))
    check("含杭州", "杭州" in toks, str(toks))
    check("空文本", zh_tokenize("") == [])


def test_mock_embedder():
    print("\n[Mock 嵌入器]")
    e = MockEmbedder(dim=64)
    v = e.encode(["杭州西湖", "杭州西湖"])
    check("确定性（同文本同向量）", (v[0] == v[1]).all())
    check("已归一化", abs(float(v[0] @ v[0]) - 1.0) < 1e-4, str(float(v[0] @ v[0])))
    v2 = e.encode(["三亚潜水"])
    check("不同文本向量不同", float(v[0] @ v2[0]) < 0.999)
    check("空输入返回空矩阵", e.encode([]).shape[0] == 0)


def test_bm25():
    print("\n[BM25 稀疏检索]")
    idx = BM25Index()
    idx.build(_CORPUS)
    hits = idx.search("杭州 西湖 酒店", top_k=3)
    check("有命中", len(hits) > 0)
    check("Top1 是西湖国宾馆", hits[0][0] == 0, str(hits))
    check("无关查询无命中", idx.search("火星xyz", top_k=3) == [])


def test_dense():
    print("\n[稠密向量检索]")
    idx = DenseIndex(MockEmbedder())
    idx.build(_CORPUS)
    hits = idx.search("杭州西湖国宾馆", top_k=2)
    check("有命中", len(hits) > 0)
    check("Top1 是西湖国宾馆", hits[0][0] == 0, str(hits))


def test_rrf():
    print("\n[RRF 融合]")
    fused = _rrf([[0, 1, 2], [0, 2, 3]])
    check("两榜都第1的得分最高", max(fused, key=fused.get) == 0)
    check("仅单榜命中的也有分", 3 in fused and fused[3] > 0)


def test_hybrid_retriever():
    print("\n[混合检索器]")
    r = HybridRetriever(_CORPUS, _META, embedder=MockEmbedder())
    docs = r.retrieve("杭州西湖附近的酒店", top_k=2)
    check("返回结果", len(docs) > 0)
    check("Top1 命中酒店", docs[0].metadata.get("type") == "hotel" and "国宾馆" in docs[0].text,
          docs[0].text[:30])
    check("结果含双路排名信息", docs[0].bm25_rank is not None or docs[0].dense_rank is not None)

    # 只用 BM25
    r2 = HybridRetriever(_CORPUS, _META, embedder=MockEmbedder(), use_dense=False)
    d2 = r2.retrieve("成都熊猫", top_k=1)
    check("纯BM25可用", len(d2) == 1 and "熊猫" in d2[0].text)

    # 空语料
    r3 = HybridRetriever([], [], embedder=MockEmbedder())
    check("空语料返回空", r3.retrieve("杭州") == [])


def test_lexical_reranker():
    print("\n[词面重排]")
    rr = LexicalReranker()
    docs = [
        RetrievedDoc(text="成都熊猫基地 门票", score=0.5),
        RetrievedDoc(text="杭州西湖国宾馆 酒店 西湖", score=0.4),
    ]
    out = rr.rerank("杭州 西湖 酒店", docs, top_k=2)
    check("重排后西湖酒店靠前", out[0].text.startswith("杭州"), out[0].text[:20])


def test_query_rewriter_rule():
    print("\n[Query 改写：规则扩展]")
    qw = QueryRewriter()
    variants = qw.expand("杭州景点门票")
    check("含原句", variants[0] == "杭州景点门票")
    check("生成扩展变体", len(variants) >= 2, str(variants))
    check("扩展含别名/同义词",
          any(t in variants[1] for t in ["西湖", "景区", "票价", "杭城"]), variants[1] if len(variants) > 1 else "")


def test_query_rewriter_llm():
    print("\n[Query 改写：LLM + 回退]")
    def mock_llm(messages, **kw):
        return {"choices": [{"message": {"role": "assistant",
                "content": "1. 杭州 西湖 酒店\n2. 西湖边住宿推荐\n- 杭州景区宾馆"}}]}
    qw = QueryRewriter(call_llm_fn=mock_llm)
    out = qw.rewrite("西湖附近住哪", max_variants=3)
    check("保留原句在首位", out[0] == "西湖附近住哪")
    check("含 LLM 改写并去编号", "杭州 西湖 酒店" in out, str(out))
    check("数量受控", len(out) <= 3)

    def err_llm(messages, **kw):
        return {"error": "超时"}
    qw2 = QueryRewriter(call_llm_fn=err_llm)
    out2 = qw2.rewrite("杭州景点")
    check("LLM 失败回退规则", out2[0] == "杭州景点" and len(out2) >= 1)


class _StubRewriter:
    def __init__(self, variants):
        self._v = variants

    def rewrite(self, query, max_variants=3):
        return self._v


def test_crag_sufficient():
    print("\n[CRAG：命中充分]")
    r = HybridRetriever(_CORPUS, _META, embedder=MockEmbedder())
    pipe = CRAGPipeline(r)
    res = pipe.run("杭州西湖附近的酒店", top_k=3)
    check("grade=sufficient/partial", res.grade in ("sufficient", "partial"), res.grade)
    check("action=use", res.action == "use", res.action)
    check("上下文非空", "知识库检索结果" in res.context)


def test_crag_rewrite_recovers():
    print("\n[CRAG：不足→改写重检恢复]")
    r = HybridRetriever(_CORPUS, _META, embedder=MockEmbedder())
    stub = _StubRewriter(["zqxy 无关词", "杭州 西湖 酒店"])
    pipe = CRAGPipeline(r, rewriter=stub)
    res = pipe.run("zqxy 无关词", top_k=3)
    check("改写后恢复", res.action == "rewrite", res.action)
    check("最终检到酒店", any("国宾馆" in d.text for d in res.docs), str([d.text[:12] for d in res.docs]))
    check("上下文非空", len(res.context) > 0)


def test_crag_fallback():
    print("\n[CRAG：始终不足→兜底]")
    r = HybridRetriever(_CORPUS, _META, embedder=MockEmbedder())
    stub = _StubRewriter(["qqq www zzz 完全无关"])
    pipe = CRAGPipeline(r, rewriter=stub)
    res = pipe.run("qqq www zzz 完全无关", top_k=3)
    check("action=fallback", res.action == "fallback", res.action)
    check("grade=insufficient", res.grade == "insufficient", res.grade)
    check("上下文为空（不硬塞）", res.context == "")


def test_real_corpus():
    print("\n[真实本地知识库语料]")
    docs, metas = build_corpus_from_local_knowledge()
    check("语料非空", len(docs) > 30, str(len(docs)))
    check("元数据对齐", len(docs) == len(metas))
    check("覆盖多城市", len({m.get("city") for m in metas}) >= 5)

    r = HybridRetriever(docs, metas, embedder=MockEmbedder())
    res = r.retrieve("杭州西湖边的五星级酒店", top_k=5)
    check("检到杭州酒店", any(d.metadata.get("city") == "杭州" and d.metadata.get("type") == "hotel"
                          for d in res), str([(d.metadata.get('city'), d.metadata.get('type')) for d in res]))


def main():
    print("=" * 60)
    print("Hybrid RAG 单元测试")
    print("=" * 60)
    test_tokenize()
    test_mock_embedder()
    test_bm25()
    test_dense()
    test_rrf()
    test_hybrid_retriever()
    test_lexical_reranker()
    test_query_rewriter_rule()
    test_query_rewriter_llm()
    test_crag_sufficient()
    test_crag_rewrite_recovers()
    test_crag_fallback()
    test_real_corpus()
    print("\n" + "=" * 60)
    print(f"结果: {_PASS} passed, {_FAIL} failed")
    print("=" * 60)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
