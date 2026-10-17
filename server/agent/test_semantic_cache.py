# -*- coding: utf-8 -*-
"""
语义缓存（Semantic Cache）单元测试
==================================
重点验证「实体护栏」：跨城市/跨天数绝不假命中；近义同城可命中；
TTL/LRU、统计、cached_call_llm 包装（命中不调用 LLM、tools/stream 跳过、error 不缓存）。
默认 TF-IDF 嵌入，零模型下载，全程离线。

运行：  python server/agent/test_semantic_cache.py
"""

from __future__ import annotations

import os
import sys
import time

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

from agent.semantic_cache import (  # noqa: E402
    normalize, extract_key_tokens, TfidfEmbedder, SemanticCache,
    cached_call_llm, make_embedder,
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


# ==================== 归一化 & 实体抽取 ====================

def test_normalize():
    print("\n[归一化]")
    check("去标点空白", normalize("杭州， 西湖！") == "杭州 西湖")
    check("小写化", normalize("ABC") == "abc")
    check("空串", normalize("") == "")


def test_key_tokens():
    print("\n[关键实体抽取]")
    t = extract_key_tokens("帮我规划杭州3日游")
    check("识别城市", "city:杭州" in t, str(t))
    check("识别天数(阿拉伯)", "day:3" in t, str(t))

    t2 = extract_key_tokens("杭州三天的行程")
    check("识别天数(中文)", "day:3" in t2, str(t2))

    t3 = extract_key_tokens("三亚5日游")
    check("不同城市不同 token", "city:三亚" in t3 and "city:杭州" not in t3)

    check("预算数字入 token", "num:2000" in extract_key_tokens("预算2000元"))
    check("无实体返回空", len(extract_key_tokens("你好")) == 0)


# ==================== 嵌入器 ====================

def test_embedder():
    print("\n[TF-IDF 嵌入器]")
    e = TfidfEmbedder()
    va = e.embed("杭州有什么好玩的景点")
    vb = e.embed("杭州有哪些好玩的景点推荐")
    vc = e.embed("三亚酒店价格多少钱")
    check("近义相似度更高", e.similarity(va, vb) > e.similarity(va, vc))
    check("自比=1", abs(e.similarity(va, va) - 1.0) < 1e-6)
    check("远义接近0", e.similarity(va, vc) < 0.2)


def test_make_embedder_fallback():
    print("\n[嵌入器工厂回退]")
    import agent.semantic_cache as sc
    # 不指定神经 → 恒为 tfidf
    check("prefer_neural=False → tfidf", sc.make_embedder(prefer_neural=False).name == "tfidf")
    # prefer_neural=True：环境装了模型则 neural，否则自动回退 tfidf —— 两者都算正确
    e = sc.make_embedder(prefer_neural=True)
    check("返回可用嵌入器", getattr(e, "name", None) in ("neural", "tfidf"), getattr(e, "name", "?"))

    # 强制 neural 构造失败 → 必须确定性回退到 tfidf
    orig = sc.NeuralEmbedder

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("模型不可用")

    sc.NeuralEmbedder = _Boom
    try:
        e2 = sc.make_embedder(prefer_neural=True)
        check("neural 不可用时回退 tfidf", getattr(e2, "name", None) == "tfidf", getattr(e2, "name", "?"))
    finally:
        sc.NeuralEmbedder = orig


# ==================== 缓存命中 / 护栏 ====================

def test_cache_hit_synonym():
    print("\n[命中：同城近义]")
    c = SemanticCache(threshold=0.6)
    c.set("杭州有什么好玩的景点", "推荐西湖、灵隐寺。")
    hit = c.get("杭州有哪些好玩的景点推荐")
    check("近义问题命中", hit is not None)
    if hit:
        check("返回缓存答案", hit["answer"] == "推荐西湖、灵隐寺。")
        check("相似度达标", hit["similarity"] >= 0.6, str(hit["similarity"]))


def test_cache_guard_cross_city():
    print("\n[护栏：跨城市绝不假命中]")
    c = SemanticCache(threshold=0.5)
    c.set("帮我规划杭州3日游", "杭州行程：第一天西湖……")
    # 字面高度相似但城市/天数不同
    check("三亚5日游 不命中", c.get("帮我规划三亚5日游") is None)
    check("杭州5日游 不命中(天数不同)", c.get("帮我规划杭州5日游") is None)
    check("杭州3日游 命中(完全一致)", c.get("帮我规划杭州3日游") is not None)


def test_cache_miss_and_ttl():
    print("\n[未命中 & TTL 过期]")
    c = SemanticCache(threshold=0.6, ttl=0.05)
    c.set("成都美食推荐", "火锅、串串。")
    check("无关问题不命中", c.get("北京故宫门票") is None)
    time.sleep(0.08)
    check("TTL 过期后不命中", c.get("成都美食推荐") is None)


def test_cache_lru_eviction():
    print("\n[LRU 淘汰]")
    c = SemanticCache(threshold=0.9, max_entries=2, ttl=None)
    c.set("查询A杭州景点", "a")
    c.set("查询B成都美食", "b")
    c.set("查询C三亚酒店", "c")  # 触发淘汰
    check("不超过上限", len(c) <= 2, str(len(c)))


def test_cache_stats():
    print("\n[统计]")
    c = SemanticCache(threshold=0.6)
    c.set("杭州有什么好玩的景点", "西湖")
    c.get("杭州有什么好玩的景点")   # hit
    c.get("完全不同的问题三亚")      # miss
    st = c.stats()
    check("记录命中", st["hits"] >= 1)
    check("记录未命中", st["misses"] >= 1)
    check("命中率在0-1", 0.0 <= st["hit_rate"] <= 1.0)
    check("含嵌入器名", st["embedder"] == "tfidf")


def test_cache_forget():
    print("\n[显式失效 forget]")
    c = SemanticCache(threshold=0.9)
    key = "杭州明天天气怎么样"
    c.set(key, "多云")
    check("写入后可命中", c.get(key) is not None)
    check("forget 删除了条目", c.forget(key) == 1 and len(c) == 0)
    check("删除后不再命中", c.get(key) is None)
    check("forget 空串安全", c.forget("") == 0 and c.forget("   ") == 0)
    c.set("三亚五星酒店价格", "1200")
    check("forget 不误伤其他键", c.forget(key) == 0 and c.get("三亚五星酒店价格") is not None)


# ==================== cached_call_llm ====================

def _resp(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def test_cached_call_llm():
    print("\n[cached_call_llm 包装]")
    calls = {"n": 0}

    def llm(messages, stream=False, skill_name="", tools=None, tool_choice=None):
        calls["n"] += 1
        return _resp(f"答复{calls['n']}")

    cache = SemanticCache(threshold=0.9)
    wrapped = cached_call_llm(llm, cache)
    msgs = [{"role": "user", "content": "杭州有什么好玩的景点"}]

    r1 = wrapped(msgs, skill_name="tourism-advisor")
    check("首次真实调用", calls["n"] == 1)
    check("首次无缓存标记", "_cache" not in r1)

    r2 = wrapped(msgs, skill_name="tourism-advisor")
    check("二次命中未再调用 LLM", calls["n"] == 1, str(calls["n"]))
    check("命中标记存在", r2.get("_cache", {}).get("hit") is True)


def test_cached_call_llm_skip_tools_stream():
    print("\n[包装器：tools/stream 跳过缓存]")
    calls = {"n": 0}

    def llm(messages, stream=False, skill_name="", tools=None, tool_choice=None):
        calls["n"] += 1
        return _resp("x")

    cache = SemanticCache(threshold=0.9)
    wrapped = cached_call_llm(llm, cache)
    msgs = [{"role": "user", "content": "带工具的查询"}]

    wrapped(msgs, tools=[{"type": "function"}], skill_name="s")
    wrapped(msgs, tools=[{"type": "function"}], skill_name="s")
    check("带 tools 每次都真实调用", calls["n"] == 2, str(calls["n"]))
    check("带 tools 不写缓存", len(cache) == 0)

    wrapped(msgs, stream=True, skill_name="s")
    check("stream 也真实调用", calls["n"] == 3)


def test_cached_call_llm_error_not_cached():
    print("\n[包装器：error 不缓存]")
    def llm(messages, **kw):
        return {"error": "超时"}
    cache = SemanticCache(threshold=0.9)
    wrapped = cached_call_llm(llm, cache)
    wrapped([{"role": "user", "content": "杭州景点"}])
    check("错误响应不入缓存", len(cache) == 0)


def test_cache_thread_safety():
    """waitress 多线程共用同一个缓存实例：并发读写不能抛，计数不能丢，容量不能越界。"""
    print("\n[并发安全]")
    import threading
    cache = SemanticCache(threshold=0.5, ttl=None, max_entries=8)
    errors: list = []

    def worker(tid):
        try:
            for i in range(40):
                q = f"帮我规划杭州{i % 3}日游（第{tid}线程第{i}次）"
                cache.get(q)
                cache.set(q, {"choices": [{"message": {"content": f"a{tid}-{i}"}}]})
                if i % 8 == 0:
                    cache.forget("帮我规划杭州0日游")
                    cache.stats()
        except Exception as e:  # noqa: BLE001
            errors.append(f"t{tid}: {type(e).__name__}: {e}")

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    check("并发读写不抛异常", not errors, str(errors[:2]))
    check("并发写入仍受 max_entries 约束", len(cache) <= 8, f"len={len(cache)}")
    st = cache.stats()
    check("并发计数一条不丢", st["hits"] + st["misses"] == 8 * 40, str(st))
    check("并发后 stats 仍可读", st["embedder"] == "tfidf", str(st))


def main():
    print("=" * 60)
    print("Semantic Cache 单元测试")
    print("=" * 60)
    test_normalize()
    test_key_tokens()
    test_embedder()
    test_make_embedder_fallback()
    test_cache_hit_synonym()
    test_cache_guard_cross_city()
    test_cache_miss_and_ttl()
    test_cache_lru_eviction()
    test_cache_stats()
    test_cache_forget()
    test_cached_call_llm()
    test_cached_call_llm_skip_tools_stream()
    test_cached_call_llm_error_not_cached()
    test_cache_thread_safety()
    print("\n" + "=" * 60)
    print(f"结果: {_PASS} passed, {_FAIL} failed")
    print("=" * 60)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
