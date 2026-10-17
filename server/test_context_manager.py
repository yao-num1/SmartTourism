# -*- coding: utf-8 -*-
"""
上下文管理 / 记忆持久化单元测试
================================
覆盖三件事，全程离线（不需要 LLM，不需要网络）：
  1. 预算装配：超预算的旧轮次被清退，最近轮永远保留，摘要放前部；
  2. 压缩管理：分级压缩 + 硬约束保底 + 句子级裁剪；压缩模型缺失时走抽取式降级而不是整段丢弃；
  3. 记忆持久化：滚动摘要落 sessions.history_summary、已确认事实落 session_state.facts_json，
     「重启」（清空内存 store）后能恢复。

用临时库（SMARTTOURISM_DB），不碰 data/smarttourism.db。

运行：  python server/test_context_manager.py
"""

from __future__ import annotations

import os
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_HERE, _ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

# 必须在 import database 之前设好：DB_PATH 在模块加载时求值
_TMPDB = os.path.join(tempfile.mkdtemp(prefix="st_ctx_test_"), "test.db")
os.environ["SMARTTOURISM_DB"] = _TMPDB
os.environ.setdefault("CTX_PERSIST", "1")
# 用小预算，好让合成的短历史真的触发「超预算清退 + 折叠压缩」
os.environ.setdefault("CTX_BUDGET", "400")

import database as db  # noqa: E402
import context_manager as cm  # noqa: E402
from anti_hallucination import SessionFactTracker  # noqa: E402

db.init_db()

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


def _rounds(n, char="哈"):
    """造 n 轮完整对话，每轮内容长度可控。"""
    out = []
    for i in range(n):
        out.append({"role": "user", "content": f"第{i}轮提问" + char * 20})
        out.append({"role": "assistant", "content": f"第{i}轮回答" + char * 20})
    return out


# ==================== 1. 基础工具 ====================

def test_estimate_and_group():
    print("\n[1] token 估算与轮次配对")
    check("CJK 每字约 1 token", cm.estimate_tokens("杭州三日游") >= 5)
    check("空串为 0", cm.estimate_tokens("") == 0)
    check("英文按 4 字符/Token", cm.estimate_tokens("a" * 40) == 10)

    rounds, tail = cm._group_rounds(_rounds(3) + [{"role": "user", "content": "半个轮"}])
    check("完整轮配对正确", len(rounds) == 3, f"got {len(rounds)}")
    check("未配对尾巴被单独返回", len(tail) == 1)


# ==================== 2. 预算装配 ====================

def test_budget_keeps_recent():
    print("\n[2] 预算装配：最近轮必保，旧轮清退进摘要")
    conv = "t-budget-1"
    cm.clear_summary(conv)
    hist = _rounds(12)
    made = []

    def fake_summarizer(payload):
        made.append(payload)
        return "摘要：讨论杭州行程，预算3000元。"

    msgs, meta = cm.manage_context(conv, "你是助手", hist, "再来一版行程",
                                   summarizer=fake_summarizer)
    check("system 永远在第一条", msgs[0]["role"] == "system")
    check("当前用户消息永远在最后", msgs[-1]["content"] == "再来一版行程")
    check("最近轮全部保留", meta["rounds_included"] >= cm.CONTEXT_RECENT_ROUNDS,
          f"included={meta['rounds_included']}")
    check("确有旧轮被清退", meta["rounds_total"] > meta["rounds_included"],
          f"total={meta['rounds_total']} included={meta['rounds_included']}")
    check("清退的轮次进入了压缩器", len(made) >= 1)
    check("meta 带压缩观测字段",
          {"summary_chars", "compress_ratio", "compress_tier", "summary_src_chars"} <= set(meta),
          f"keys={sorted(meta)}")
    check("摘要计数与清退一致", meta["rounds_dropped"] + meta["rounds_included"]
          <= meta["rounds_total"] + 1)


# ==================== 3. 压缩管理 ====================

def test_constraints_survive_without_summarizer():
    print("\n[3] 无压缩模型 → 抽取式降级，硬约束不丢")
    conv = "t-degrade-1"
    cm.clear_summary(conv)
    hist = ([{"role": "user", "content": "我想去杭州玩5天，预算人均800元，不吃辣"},
             {"role": "assistant", "content": "好的，给你安排西湖周边的行程" * 20}]
            * 8)
    msgs, meta = cm.manage_context(conv, "你是助手", hist, "继续", summarizer=None)
    summary = cm.get_summary(conv)
    check("降级路径也产出摘要", bool(summary), f"meta={meta}")
    check("压缩层级标记为 degraded", meta["compress_tier"] == "degraded",
          f"tier={meta['compress_tier']}")
    check("目的地被保住", "杭州" in summary, summary)
    check("预算被保住", "人均800" in summary or "预算" in summary, summary)
    check("忌口被保住", "不吃辣" in summary, summary)
    check("摘要放进了 messages 前部", msgs[1]["role"] == "system" and "摘要" in msgs[1]["content"])


def test_sentence_trim_and_constraints():
    print("\n[4] 句子级裁剪与约束抽取")
    text = "第一句目的地杭州五日游。" + "无关寒暄。" * 40 + "最后一句预算3000元。"
    trimmed = cm.sentence_trim(text, 60)
    check("裁剪到上限内", len(trimmed) <= 60 + 6, f"len={len(trimmed)}")
    check("不会切出半句话结尾", not trimmed.endswith("的") and trimmed[-1] in "。；;！!？?。",
          repr(trimmed[-6:]))
    check("含硬约束的句子优先保留", "预算3000元" in trimmed or "目的地杭州" in trimmed, trimmed)
    check("短文本原样返回", cm.sentence_trim("短短一句。", 100) == "短短一句。")

    cons = cm.extract_constraints("目的地杭州，预算3000元，我不吃辣，也不要爬山")
    check("约束抽取命中多条", len(cons) >= 3, f"cons={cons}")
    check("约束抽取去重", len(cons) == len(set(cons)))
    check("空文本不炸", cm.extract_constraints("") == [])


def test_chunked_tier_for_long_transcript():
    print("\n[5] 超长原文 → 分段压缩（chunked）")
    conv = "t-chunk-1"
    cm.clear_summary(conv)
    calls = []

    def summarizer(payload):
        calls.append(len(payload))
        return "压缩结果：杭州行程要点。"

    # 造一段远超 SUMMARY_INPUT_CHARS 的历史，确保走分段分支
    long_turn = "很多字的对话内容" * (cm.SUMMARY_INPUT_CHARS // 24 + 40)
    hist = [{"role": "user", "content": long_turn},
            {"role": "assistant", "content": long_turn}] * 16
    msgs, meta = cm.manage_context(conv, "sys", hist, "继续", summarizer=summarizer)
    check("触发了分段压缩", meta["compress_tier"] == "chunked", f"tier={meta['compress_tier']}")
    check("多次调用压缩器", len(calls) >= 2, f"calls={len(calls)}")
    check("单次入参不超分段上限", max(calls) <= cm.SUMMARY_INPUT_CHARS + 400 or len(calls) == 1)


# ==================== 6. 记忆持久化 ====================

def test_summary_persist_and_restore():
    print("\n[6] 摘要落库 + 重启恢复")
    conv = "t-persist-1"
    sess = db.create_session(user_id="default", session_id=conv)
    cm.clear_summary(conv)
    hist = []
    for i in range(10):
        hist.append({"role": "user", "content": f"第{i}轮：杭州西溪湿地怎么玩，预算3000元" + "补充" * 30})
        hist.append({"role": "assistant", "content": f"第{i}轮回答" + "详情" * 60})
    msgs, meta = cm.manage_context(conv, "sys", hist, "继续",
                                   summarizer=lambda p: "摘要：杭州西溪，预算3000元。")
    row = db.get_session(conv)
    check("摘要写入 sessions.history_summary",
          (row.get("history_summary") or "").find("杭州") >= 0, row.get("history_summary"))
    st = db.get_session_state(conv)
    check("折叠轮数写入 session_state", st and st["folded_rounds"] > 0, st)

    # 模拟服务重启：内存 store 与 hydrated 标记全清
    cm._summary_store.clear()
    cm._hydrated.clear()
    restored = cm.get_summary(conv)
    check("重启后摘要从 DB 恢复", "杭州" in restored, repr(restored))
    m2 = cm.get_summary_meta(conv)
    check("重启后 folded 计数恢复", m2["folded"] == st["folded_rounds"], m2)
    check("恢复标记可见", m2["restored"] is True, m2)

    # 重启后继续对话：不应把已折叠的轮次重复折叠
    msgs2, meta2 = cm.manage_context(conv, "sys", hist, "再来一版",
                                     summarizer=lambda p: "摘要：杭州西溪，预算3000元。")
    check("恢复后不重复折叠", meta2["rounds_in_summary"] >= m2["folded"])

    cm.clear_summary(conv)
    check("clear_summary 清内存", cm.get_summary(conv) == "")
    check("clear_summary 清 DB", (db.get_session(conv).get("history_summary") or "") == "")
    db.update_session_summary(conv, "")


def test_facts_persist_and_restore():
    print("\n[7] 已确认事实落库 + 重启恢复")
    conv = "t-facts-1"
    db.create_session(user_id="default", session_id=conv)
    tracker = SessionFactTracker()
    tracker.clear(conv)
    tracker.update_facts(conv, "我们一家三代人去大理玩7天，人均预算2000元，老人不吃辣")
    f1 = tracker.get_facts(conv)
    check("目的地被抽取", f1.get("destination") == "大理", f1)
    check("天数被抽取", f1.get("days") == 7, f1)
    check("预算被抽取（前置说法）", f1.get("budget") == 2000, f1)
    check("人群被抽取", f1.get("crowd_type") in ("亲子", "老年"), f1)
    row = db.get_session_state(conv)
    check("事实写入 session_state.facts_json", row and "大理" in (row["facts_json"] or ""), row)

    # 模拟重启：换一个全新的 tracker 实例
    f2 = SessionFactTracker().get_facts(conv)
    check("重启后事实从 DB 恢复", f2.get("destination") == "大理", f2)
    check("恢复后天数字段完整", f2.get("days") == 7, f2)

    # 锚定块能直接用上恢复出来的事实
    block = cm.build_facts_block(conv)
    check("事实锚定块含恢复后的目的地", "大理" in block, block)

    SessionFactTracker().clear(conv)
    check("clear 后 DB 事实被清", (db.get_session_state(conv) or {}).get("facts_json") == "{}")


def test_budget_phrasing_variants():
    print("\n[8] 预算表述变体（回归：此前「预算3000元」提不出来）")
    conv = "t-facts-2"
    t = SessionFactTracker()
    t.update_facts(conv, "预算3000元，去三亚")
    check("「预算3000元」可识别", t.get_facts(conv).get("budget") == 3000, t.get_facts(conv))
    conv = "t-facts-3"
    t.update_facts(conv, "人均1万预算去厦门")
    check("「人均1万」换算成元", t.get_facts(conv).get("budget") == 10000, t.get_facts(conv))


def test_persist_disabled_is_pure_memory():
    print("\n[9] CTX_PERSIST=0 时降级为纯内存且不报错")
    conv = "t-nopersist"
    old = os.environ.get("CTX_PERSIST")
    os.environ["CTX_PERSIST"] = "0"
    try:
        cm.clear_summary(conv)
        hist = [{"role": "user", "content": "去杭州" + "字" * 100},
                {"role": "assistant", "content": "好的" + "字" * 100}] * 10
        msgs, meta = cm.manage_context(conv, "sys", hist, "继续",
                                       summarizer=lambda p: "纯内存摘要")
        check("关闭持久化仍有摘要", "纯内存" in cm.get_summary(conv))
        check("关闭持久化不写库", (db.get_session(conv).get("history_summary") if db.get_session(conv) else "") in ("", None))
        tr = SessionFactTracker()
        tr.update_facts(conv, "去杭州5天")
        check("关闭持久化事实仍可用", tr.get_facts(conv).get("destination") == "杭州")
    finally:
        if old is None:
            os.environ.pop("CTX_PERSIST", None)
        else:
            os.environ["CTX_PERSIST"] = old
    cm._summary_store.pop(conv, None)


def test_compress_trigger_and_meter():
    print("\n[10] 压缩触发线：到线即压 + 前端进度条字段")
    conv = "t-trigger-1"
    hist = _rounds(20)
    orig = cm.COMPRESS_TRIGGER
    if not os.environ.get("CTX_COMPRESS_TRIGGER"):
        check("默认触发线 75%", abs(orig - 0.75) < 1e-9, orig)
    try:
        cm.COMPRESS_TRIGGER = 1.0          # 撑满预算才压
        cm.clear_summary(conv)
        _, loose = cm.manage_context(conv, "你是助手", hist, "继续", summarizer=None)
        cm.COMPRESS_TRIGGER = 0.4          # 到四成线就开始折叠旧轮
        cm.clear_summary(conv)
        _, tight = cm.manage_context(conv, "你是助手", hist, "继续", summarizer=None)

        check("触发线越低，保留的旧轮越少",
              tight["rounds_included"] < loose["rounds_included"],
              f"tight={tight['rounds_included']} loose={loose['rounds_included']}")
        check("触发线越低，本轮清退越多",
              tight["rounds_dropped"] > loose["rounds_dropped"],
              f"tight={tight['rounds_dropped']} loose={loose['rounds_dropped']}")
        check("收紧触发线也不丢最近轮",
              tight["rounds_included"] >= cm.CONTEXT_RECENT_ROUNDS,
              f"included={tight['rounds_included']}")

        check("meta 带进度条字段",
              {"pct", "trigger", "trigger_tokens"} <= set(loose), f"keys={sorted(loose)}")
        check("pct = 占用 / 预算",
              abs(loose["pct"] - loose["est_input_tokens"] / loose["budget"]) < 1e-3, loose)
        check("trigger_tokens = 预算 × 触发线",
              loose["trigger_tokens"] == int(loose["budget"] * loose["trigger"]), loose)
    finally:
        cm.COMPRESS_TRIGGER = orig
    cm._summary_store.pop(conv, None)


def main():
    print("=" * 60)
    print("上下文管理 / 记忆持久化测试")
    print("=" * 60)
    test_estimate_and_group()
    test_budget_keeps_recent()
    test_constraints_survive_without_summarizer()
    test_sentence_trim_and_constraints()
    test_chunked_tier_for_long_transcript()
    test_summary_persist_and_restore()
    test_facts_persist_and_restore()
    test_budget_phrasing_variants()
    test_persist_disabled_is_pure_memory()
    test_compress_trigger_and_meter()
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
