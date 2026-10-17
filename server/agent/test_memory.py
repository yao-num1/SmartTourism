# -*- coding: utf-8 -*-
"""
长期记忆模块（agent/memory.py）单元测试
========================================
全程离线：规则抽取用正则、向量用 TF-IDF（无需下载模型）、LLM 抽取用假的 call_llm。
重点验证四件事：
  1. 抽取 —— 正向/否定式不互相打架（「不喜欢杭州」不能记成「想去杭州」）；
  2. 写入 —— 同义重复要合并、单值槽位要覆盖、多值槽位要并存；
  3. 召回 —— 相关问题排得上来、无关问题不硬塞、命中会强化；
  4. 降级 —— DB / 嵌入 / LLM 任一不可用都不能抛异常打断对话。

运行：  python server/agent/test_memory.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVER = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_SERVER)
for p in (_SERVER, _ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

# 必须在 import database 之前设好
_TMPDB = os.path.join(tempfile.mkdtemp(prefix="st_mem_test_"), "test.db")
os.environ["SMARTTOURISM_DB"] = _TMPDB
os.environ.setdefault("MEM_NEURAL", "0")

import database as db  # noqa: E402
from agent import memory as M  # noqa: E402
from agent.semantic_cache import TfidfEmbedder  # noqa: E402

db.init_db()
M.set_embedder(TfidfEmbedder())

_PASS = 0
_FAIL = 0


def check(name, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def _slots(cands):
    return {(c.slot, c.content, c.kind) for c in cands}


def _has(cands, needle):
    return any(needle in c.content for c in cands)


# ==================== 1. 规则抽取 ====================

def test_extraction_basics():
    print("\n[1] 规则抽取：偏好/约束/事实")
    c = M.extract_rule_based("我一般喜欢海岛，这次想去三亚，人均预算3000元左右")
    check("抽到目的地偏好", _has(c, "三亚"), c)
    check("抽到预算", _has(c, "3000"), c)

    c = M.extract_rule_based("我不吃辣，也不喝牛奶，带两个宝宝出门")
    check("抽到忌口(辣)", any(x.slot == "diet" and "辣" in x.content for x in c), c)
    check("抽到忌口(奶)", any(x.slot == "diet" and "牛奶" in x.content for x in c), c)
    check("抽到人群=亲子", any(x.slot == "crowd" and "亲子" in x.content for x in c), c)

    c = M.extract_rule_based("我妈坐轮椅，行程最多5天，别安排爬山")
    check("抽到无障碍约束", any(x.slot == "accessibility" for x in c), c)
    check("抽到天数上限", any(x.slot == "days" and "5" in x.content for x in c), c)

    c = M.extract_rule_based("我恐高，也不坐飞机")
    check("恐高被识别", any("恐高" in x.content for x in c), c)
    check("不坐飞机被识别成约束", any(x.kind == "constraint" and "飞机" in x.content for x in c), c)

    c = M.extract_rule_based("出去住一般优先民宿，喜欢慢节奏，不要特种兵赶场")
    check("住宿偏好被识别", any(x.slot == "lodging" and "民宿" in x.content for x in c), c)
    check("风格偏好被识别", any(x.slot == "style" for x in c), c)

    c = M.extract_rule_based("去杭州高铁很方便，我们习惯坐高铁")
    check("高铁偏好被识别", any(x.slot == "transport" and "高铁" in x.content for x in c), c)


def test_extraction_negation_guard():
    print("\n[2] 否定式不能记成正面（关键防自相矛盾）")
    c = M.extract_rule_based("我不喜欢杭州，太堵了，别推荐苏州")
    contents = [x.content for x in c]
    check("「不喜欢杭州」不会记成想去杭州", not any(x.startswith("想去") and "杭州" in x for x in contents),
          contents)
    check("「不喜欢杭州」记成排斥杭州", any(x.startswith("排斥") and "杭州" in x for x in contents), contents)
    check("「别推荐苏州」记成排斥苏州", any(x.startswith("排斥") and "苏州" in x for x in contents), contents)

    c = M.extract_rule_based("我不坐飞机，晕车")
    check("「不坐飞机」不产生正向交通偏好",
          not any(x.slot == "transport" and x.kind == "preference" for x in c), c)
    check("晕车被记成约束", any("晕车" in x.content and x.kind == "constraint" for x in c), c)


def test_extraction_ignores_small_talk():
    print("\n[3] 闲聊不该污染记忆库")
    for junk in ["你好", "在吗", "帮我看看", "继续", ""]:
        check(f"「{junk or '空'}」不产出记忆", M.extract_rule_based(junk) == [],
              M.extract_rule_based(junk))


# ==================== 4. 向量编解码 ====================

def test_vector_codec():
    print("\n[4] 向量编解码与相似度")
    emb = M.get_embedder()
    check("嵌入器可用", emb is not None and getattr(emb, "name", "") == "tfidf",
          getattr(emb, "name", None))
    v1 = M.encode_vector(emb.embed("我喜欢海岛度假"))
    v2 = M.encode_vector(emb.embed("喜欢海岛度假"))
    v3 = M.encode_vector(emb.embed("今天股市行情如何"))
    check("向量可编码为 JSON", isinstance(v1, str) and json.loads(v1)["k"] == "s", v1[:60] if v1 else None)
    sim_near = M.vector_dot(v1, v2)
    sim_far = M.vector_dot(v1, v3)
    check("近义文本相似度高", sim_near is not None and sim_near > 0.5, sim_near)
    check("无关文本相似度低", sim_far is not None and sim_far < sim_near, (sim_far, sim_near))
    check("缺向量返回 None", M.vector_dot(None, v1) is None and M.vector_dot(v1, "") is None)

    # 稠密向量分支
    dense = M.encode_vector([0.6, 0.8, 0.0, 0.0])
    check("稠密向量可编码", isinstance(dense, str) and json.loads(dense)["k"] == "d", dense)
    check("稠密自身点积≈1", abs(M.vector_dot(dense, dense) - 1.0) < 1e-3, M.vector_dot(dense, dense))

    check("词面兜底相似度可用", M.lexical_sim("不吃辣", "我不吃辣") > 0.4)
    check("词面兜底对空串安全", M.lexical_sim("", "abc") == 0.0)


# ==================== 5. 写入：去重 / 覆盖 ====================

def test_remember_dedupe_and_override():
    print("\n[5] 写入去重、同槽位覆盖、多值槽位并存")
    uid = "u-dedup"
    M.forget(user_id=uid)

    r1 = M.remember(uid, [M.MemoryCandidate("人均预算约3000元", "preference", "budget", 0.6)])
    check("首次写入计为 added", r1["added"] == 1, r1)

    # 同义改写（标点/空白差异）→ 合并而非新增副本
    r2 = M.remember(uid, [M.MemoryCandidate("人均预算约 3000 元。", "preference", "budget", 0.6)])
    check("同义写法被合并", r2["added"] == 0 and r2["updated"] == 1, r2)
    rows = M.list_memories(uid, kind="preference")
    check("合并后只有一条预算记忆", sum(1 for r in rows if r["slot"] == "budget") == 1, rows)
    check("重复表达提升置信度",
          any(r["slot"] == "budget" and float(r["confidence"]) > 0.6 for r in rows), rows)

    # 单值槽位：最新说法覆盖旧说法
    r3 = M.remember(uid, [M.MemoryCandidate("人均预算约5000元", "preference", "budget", 0.7)])
    rows = M.list_memories(uid)
    budgets = [r["content"] for r in rows if r["slot"] == "budget"]
    check("预算覆盖后仍只有一条", len(budgets) == 1, budgets)
    check("预算以最新说法为准", budgets and "5000" in budgets[0], budgets)

    # 多值槽位（忌口）：不同约束并存，不互相覆盖
    M.remember(uid, [M.MemoryCandidate("不吃辣", "constraint", "diet", 0.8),
                     M.MemoryCandidate("不吃海鲜", "constraint", "diet", 0.8)])
    diets = [r["content"] for r in M.list_memories(uid, kind="constraint") if r["slot"] == "diet"]
    check("两条忌口并存不互相覆盖", len(diets) == 2, diets)

    check("空候选列表安全", M.remember(uid, []) == {"added": 0, "updated": 0, "skipped": 0})
    check("过短内容被跳过",
          M.remember(uid, [M.MemoryCandidate("嗯", "fact", "other")])["skipped"] == 1)


# ==================== 6. 召回 + 注入 ====================

def test_recall_and_injection():
    print("\n[6] 语义召回与提示词注入")
    uid = "u-recall"
    M.forget(user_id=uid)
    db.create_session(user_id=uid, session_id="s-a")
    M.remember(uid, [
        M.MemoryCandidate("不吃辣，口味清淡", "constraint", "diet", 0.8),
        M.MemoryCandidate("偏好休闲慢节奏", "preference", "style", 0.7),
        M.MemoryCandidate("想去三亚", "preference", "destination", 0.7),
        M.MemoryCandidate("住宿偏好民宿", "preference", "lodging", 0.65),
    ], session_id="s-a")

    hits = M.recall(uid, "帮我安排一个节奏轻松、吃饭清淡的行程")
    contents = " ".join(h["content"] for h in hits)
    check("相关问题召回到位", "清淡" in contents or "慢节奏" in contents, hits)
    check("召回结果按分数降序", [h["score"] for h in hits] == sorted([h["score"] for h in hits], reverse=True))
    check("召回条数不超 top_k", len(hits) <= M.RECALL_TOP_K)
    check("召回带可解释因子", all({"sim", "conf", "decay", "hit"} <= set(h["factors"]) for h in hits))

    before = {h["memory_id"]: h for h in M.recall(uid, "不吃辣", touch=False)}
    M.recall(uid, "不吃辣的东西", touch=True)
    after = M.list_memories(uid)
    bumped = [r for r in after if r["memory_id"] in before and int(r["hit_count"]) > 0]
    check("命中后 hit_count 强化", len(bumped) >= 1, after[:3])

    block = M.build_memory_block(uid, "推荐点吃的")
    check("注入块非空", bool(block), repr(block))
    check("注入块标注降级语义", "以本轮为准" in block, block)
    check("注入块带类型标签", "[约束]" in block or "[偏好]" in block, block)
    check("无关用户不受影响（跨用户隔离）",
          M.build_memory_block("u-someone-else", "推荐点吃的") == "")

    # 时间衰减：把记忆改到很久以前，分数应显著下降
    row = M.list_memories(uid)[0]
    conn = db.get_connection()
    conn.execute("UPDATE memories SET updated_at = '2020-01-01 00:00:00' WHERE memory_id = ?",
                 (row["memory_id"],))
    conn.commit()
    conn.close()
    sc_old, parts = M.score_memory(row["content"], M._embed_json(row["content"]),
                                   {**row, "updated_at": "2020-01-01 00:00:00"})
    check("老记忆时间衰减生效", parts["decay"] < 0.05, parts)


# ==================== 7. LLM 抽取（假模型） ====================

def test_llm_extraction():
    print("\n[7] LLM 抽取与解析容错")
    raw = "```json\n[{\"kind\":\"preference\",\"slot\":\"style\",\"content\":\"偏爱小众目的地\"}," \
          "{\"kind\":\"constraint\",\"slot\":\"avoid\",\"content\":\"不想去人多景点\"}]\n```"
    cands = M.parse_llm_candidates(raw)
    check("围栏 JSON 可解析", len(cands) == 2, cands)
    check("解析保留槽位", cands[0].slot == "style" and cands[1].slot == "avoid", cands)
    check("非法 kind 归一为 preference",
          M.parse_llm_candidates('[{"kind":"weird","content":"爱住青旅"}]')[0].kind == "preference")
    check("垃圾输出返回空", M.parse_llm_candidates("抱歉我不会") == [])
    check("空输出返回空", M.parse_llm_candidates("") == [] and M.parse_llm_candidates("[]") == [])

    def fake_llm(msgs):
        return {"choices": [{"message": {"content":
                '[{"kind":"fact","slot":"crowd","content":"常和家人一起出行"}]'}}]}

    got = M.extract_with_llm("我一般和家人一起出行", fake_llm)
    check("假模型抽出一条事实", len(got) == 1 and got[0].slot == "crowd", got)

    def err_llm(msgs):
        return {"error": "网络异常"}
    check("模型报错时返回空", M.extract_with_llm("我喜欢民宿", err_llm) == [])

    def boom_llm(msgs):
        raise RuntimeError("boom")
    check("模型抛异常时返回空", M.extract_with_llm("我喜欢民宿", boom_llm) == [])

    check("闸门：含偏好线索的话轮才跑 LLM", M.should_extract("我平时不喜欢太赶的行程"))
    check("闸门：闲聊不放行", not M.should_extract("继续"))
    check("闸门：太短不放行", not M.should_extract("喜欢"))


def test_end_to_end_write_then_cross_session_recall():
    print("\n[8] 端到端：会话A写入 → 会话B跨会话召回")
    uid = "u-e2e"
    M.forget(user_id=uid)
    sessA = db.create_session(user_id=uid, session_id="sA")["session_id"]
    res = M.extract_and_remember(uid, "我们一家三口亲子游，不吃辣，偏好慢节奏，人均预算4000元",
                                 sessA, llm_fn=None)
    check("一轮话沉淀出多条记忆", res["added"] >= 3, res)

    # 换一个会话（模拟用户下周新开对话）
    block = M.build_memory_block(uid, "帮我做个三天的行程规划")
    check("新会话能读到旧偏好", "辣" in block or "慢节奏" in block, block)

    st = M.stats(uid)
    check("统计可用", st["total"] >= 3 and st["persisted"] is True and st["embedder"] == "tfidf", st)
    check("统计按槽位聚合", "diet" in st["by_slot"], st["by_slot"])


# ==================== 9. 降级与治理 ====================

def test_degradation_paths():
    print("\n[9] 依赖缺失时静默降级，绝不抛异常")
    uid = "u-degrade"
    old = os.environ.get("MEM_PERSIST")
    os.environ["MEM_PERSIST"] = "0"
    try:
        r = M.remember(uid, [M.MemoryCandidate("不吃辣", "constraint", "diet", 0.8)])
        check("关闭持久化时只计 skipped", r["skipped"] == 1 and r["added"] == 0, r)
        check("关闭持久化时召回为空", M.recall(uid, "吃什么") == [])
        check("关闭持久化时注入块为空", M.build_memory_block(uid, "吃什么") == "")
        check("关闭持久化时列表为空", M.list_memories(uid) == [])
        check("关闭持久化时 stats 不报错", M.stats(uid)["persisted"] is False)
    finally:
        if old is None:
            os.environ.pop("MEM_PERSIST", None)
        else:
            os.environ["MEM_PERSIST"] = old

    # 嵌入器不可用 → 退回词面相似度，仍能判重
    saved = M._embedder
    M.set_embedder(False)
    try:
        uid2 = "u-no-embed"
        M.forget(user_id=uid2)
        M.remember(uid2, [M.MemoryCandidate("不吃辣", "constraint", "diet", 0.8)])
        r2 = M.remember(uid2, [M.MemoryCandidate("不吃辣", "constraint", "diet", 0.8)])
        check("无嵌入器仍能去重", r2["updated"] == 1 and r2["added"] == 0, r2)
        hits = M.recall(uid2, "我不吃辣行吗")
        check("无嵌入器仍能词面召回", any("辣" in h["content"] for h in hits), hits)
    finally:
        M._embedder = saved


def test_forget_and_prune():
    print("\n[10] 记忆治理：删除与容量上限")
    uid = "u-gov"
    M.forget(user_id=uid)
    for i in range(6):
        M.remember(uid, [M.MemoryCandidate(f"想去城市{i}", "preference", "destination", 0.5 + i / 20)])
    rows = M.list_memories(uid)
    check("写入 6 条", len(rows) == 6, len(rows))

    n = M.forget(memory_id=rows[0]["memory_id"])
    check("按 id 删除生效", n == 1 and len(M.list_memories(uid)) == 5, n)

    left = db.prune_memories(uid, keep=3)
    check("超出容量按置信度淘汰", left == 2 and len(M.list_memories(uid)) == 3, left)
    kept = M.list_memories(uid)
    check("保留的是高置信度记忆", all(float(r["confidence"]) >= 0.65 for r in kept),
          [(r["content"], r["confidence"]) for r in kept])

    M.forget(user_id=uid)
    check("按用户清空", M.list_memories(uid) == [])


def test_async_write():
    print("\n[11] 后台写入不阻塞主流程")
    uid = "u-async"
    M.forget(user_id=uid)
    M.remember_async(uid, "我偏好海边，不住青旅，人均预算2000元", None, None)
    done = M.wait_bg(timeout=8.0)
    check("后台线程完成", done)
    check("后台写入落库", len(M.list_memories(uid)) >= 1, M.list_memories(uid))


def main():
    print("=" * 60)
    print("长期记忆模块测试")
    print("=" * 60)
    test_extraction_basics()
    test_extraction_negation_guard()
    test_extraction_ignores_small_talk()
    test_vector_codec()
    test_remember_dedupe_and_override()
    test_recall_and_injection()
    test_llm_extraction()
    test_end_to_end_write_then_cross_session_recall()
    test_degradation_paths()
    test_forget_and_prune()
    test_async_write()
    total = _PASS + _FAIL
    print("\n" + "=" * 60)
    print(f"结果：{_PASS} passed, {_FAIL} failed（共 {total}）")
    print("=" * 60)
    try:
        os.remove(_TMPDB)
    except Exception:
        pass
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
