# -*- coding: utf-8 -*-
"""
灵感橱窗「推荐原因」测试
========================
详情弹窗的「推荐原因」原先混着两条平台话术（内容每日联网更新 / 可一键进入工作台），
用户问的是「为什么推荐这一处」，卡片却回头介绍平台。话术删掉后只剩一条又显单薄，
现在固定三条、对应三个维度：①时机（为什么是现在）②体验（这一处独特在哪）
③客群与实用（适合谁、怎么玩省力）。

硬约束照旧：reasons 是真字段，由模型按维度产出，服务端只做去空/去重/去照抄 desc，
**宁缺不凑**——模型少给就少显示，绝不拿平台话术或 desc 复制来补第三条。

覆盖：
  1. 兜底卡片每张 3 条原因，分维度、不重复、不含话术、长度合理；
  2. 生成 prompt 要求三条与三个维度，并禁止平台功能/更新频率口径；
  3. _portal_clean_reasons 与 _portal_generate_cards 的清洗规则（去空、去重、
     去掉照抄 desc、超过 3 条截断、字符串误给也能收、缺字段返回空列表）；
  4. /api/portal/hot-showcase 成功、缓存命中、生成降级三条路径都带 3 条原因；
  5. 前端 buildHotDetail 只渲染 card.reasons 并 slice(0,3)，旧话术已从页面消失。

用临时库（SMARTTOURISM_DB），不调用真实大模型，也不打真实联网上下文。

运行：  python server/test_portal_showcase.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_HERE, _ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

# DB_PATH 在 database 模块加载时求值，必须先设好再 import app
_TMPDB = os.path.join(tempfile.mkdtemp(prefix="st_portal_"), "test.db")
os.environ["SMARTTOURISM_DB"] = _TMPDB
os.environ.setdefault("SMARTTOURISM_MEMORY", "0")
os.environ.setdefault("SMARTTOURISM_LLM_CACHE", "0")

import app as A  # noqa: E402

DASHBOARD = os.path.join(_ROOT, "dashboard", "index.html")
_PASS = 0
_FAIL = 0

# 平台话术：出现在「推荐原因」里就是自说自话，不是推荐这一处的理由
BOILERPLATE = ("每日联网更新", "紧跟最新文旅趋势", "一键进入对应工作台",
               "生成行程或营销文案", "每日更新")
# 三条原因：时机 / 体验 / 客群与实用
REASONS3 = ["初秋阴天21℃，早桂刚开，香气最清润。", "沿石屋洞步道慢行，全程约两小时。",
            "适合首访与亲子，午后出发避开团队客流。"]


def check(name, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}{(' -> ' + str(extra))[:300] if extra else ''}")


def reset_cache():
    A._portal_showcase_cache["date"] = ""
    A._portal_showcase_cache["data"] = None
    A._portal_showcase_cache["last_attempt"] = 0.0


def stub_online_context():
    """联网上下文替换成固定文本：测试不打天气与社媒接口。"""
    A._portal_fetch_online_context = lambda city: ["【测试上下文】本地固定数据"]


def stub_llm(payload):
    """call_llm 替身工厂：payload 为 None 时模拟生成失败。"""
    def _call(messages, *args, **kwargs):
        if payload is None:
            return {}
        return {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}
    return _call


def sample_cards(reasons=None):
    cls = ["scenic", "culture", "food", "night", "hotel", "market"]
    out = []
    for i, c in enumerate(cls):
        out.append({
            "tag": f"类别{i}", "icon": "🏞️", "cls": c,
            "title": f"标题{i}", "sub": f"副标题{i}",
            "desc": f"这是第{i}张卡片的推荐语，四十到六十字左右，讲清楚这一处玩什么。",
            "reasons": list(REASONS3 if reasons is None else reasons),
            "actionText": "去看看", "actionPage": "plan",
        })
    return out


def main():
    print("=" * 64)
    print("灵感橱窗 · 推荐原因（三条三维度）")
    print("=" * 64)

    print("\n[1] 兜底卡片：每张三条原因，分维度不重复")
    fallback = A._portal_fallback_cards()
    check("兜底卡片 6 张", len(fallback) == 6, len(fallback))
    check("每张都是 3 条原因", [len(c.get("reasons") or []) for c in fallback] == [3] * 6,
          [len(c.get("reasons") or []) for c in fallback])
    check("原因不为空且不超过 30 字",
          all(r.strip() and len(r) <= 30 for c in fallback for r in c["reasons"]),
          [r for c in fallback for r in c["reasons"] if len(r) > 30])
    hit = [(c["title"], w) for c in fallback for r in c["reasons"] for w in BOILERPLATE if w in r]
    check("原因不含平台话术", not hit, hit)
    dup = [c["title"] for c in fallback if len(set(c["reasons"])) != 3]
    check("同一张卡三条不重复", not dup, dup)
    same_desc = [c["title"] for c in fallback if any(r == c["desc"] for r in c["reasons"])]
    check("原因不照抄 desc", not same_desc, same_desc)
    # 三条分别覆盖三个维度：时机 / 体验 / 客群实用（用关键词粗判，防止三条同质）
    timing = ("清晨", "傍晚", "白天", "周末", "节假日", "入夜", "观展前后", "午晚", "场次", "房态")
    people = ("适合", "优先", "亲子", "情侣", "客群", "年轻", "首访", "研学", "团餐", "策划")
    bad_timing = [c["title"] for c in fallback if not any(k in c["reasons"][0] for k in timing)]
    bad_people = [c["title"] for c in fallback if not any(k in c["reasons"][2] for k in people)]
    check("第 1 条都落在「时机」维度", not bad_timing, bad_timing)
    check("第 3 条都落在「客群与实用」维度", not bad_people, bad_people)
    rotated = A._portal_fallback_cards()
    check("按日轮换不丢原因", all(len(r.get("reasons") or []) == 3 for r in rotated))

    print("\n[2] 生成 prompt：要求三条、对应三个维度")
    captured = {}

    def capture(messages, *args, **kwargs):
        captured["prompt"] = messages[-1]["content"]
        return {"choices": [{"message": {"content": "[]"}}]}
    stub_online_context()
    real_call_llm = A.call_llm
    A.call_llm = capture
    A._portal_generate_cards("杭州")
    prompt = captured.get("prompt", "")
    check("prompt 要求 reasons 数组", "reasons 是「推荐原因」数组" in prompt)
    check("prompt 要求恰好 3 条", "恰好 3 条" in prompt)
    check("prompt 限制每条 30 字", "每条不超过 30 字" in prompt)
    check("prompt 写明维度①时机", "时机" in prompt and "为什么是现在" in prompt)
    check("prompt 写明维度②体验", "体验" in prompt and "独特" in prompt)
    check("prompt 写明维度③客群与实用", "客群与实用" in prompt)
    check("prompt 禁止三条重复", "彼此不得重复" in prompt)
    check("prompt 禁止照抄 desc", "不要照抄 desc" in prompt)
    check("prompt 禁止平台功能口径", "不要写成平台功能" in prompt)
    check("prompt 禁止更新频率口径", "更新频率" in prompt)
    check("prompt 禁止编造具体价格", "不编具体价格" in prompt)
    check("JSON 模板带 reasons 数组", '"reasons":["时机…","体验…","客群与实用…"]' in prompt)
    check("类别与动作约束仍在", '["scenic","culture","food","night","hotel","market"]' in prompt
          and '["plan","marketing","chat"]' in prompt)

    print("\n[3] 清洗规则：去空 / 去重 / 去照抄 / 截三条")
    clean = A._portal_clean_reasons
    check("正常三条原样保留", clean(REASONS3, "") == REASONS3)
    check("超过三条截断到 3", clean(["a", "b", "c", "d", "e"], "") == ["a", "b", "c"])
    check("空串与空白条目丢弃", clean(["a", "", "   ", "b"], "") == ["a", "b"])
    check("None 条目丢弃", clean(["a", None, "b"], "") == ["a", "b"])
    check("重复条目去重", clean(["a", "a", "b"], "") == ["a", "b"])
    check("照抄 desc 的那条丢弃", clean(["同一句话", "b"], "同一句话") == ["b"])
    check("超长截断到 60 字", clean(["长" * 90], "")[0] == "长" * 60)
    check("模型给字符串也能收", clean("只有一句话", "") == ["只有一句话"])
    check("非列表非字符串返回空", clean({"a": 1}, "") == [] and clean(None, "") == [])
    check("全空返回空列表（不编第三条）", clean(["", None, "  "], "") == [])

    cards = sample_cards()
    A.call_llm = stub_llm(cards)
    got = A._portal_generate_cards("杭州")
    check("6 张卡片全部生成", len(got) == 6, len(got))
    check("三条原因透传", all(c["reasons"] == REASONS3 for c in got),
          [c["reasons"] for c in got][:1])
    check("卡片不再带旧 reason 字段", all("reason" not in c for c in got))

    A.call_llm = stub_llm(sample_cards(reasons=["第一条", "第一条", "第三条"]))
    got = A._portal_generate_cards("杭州")
    check("模型重复也只留两条（宁缺不凑）",
          all(c["reasons"] == ["第一条", "第三条"] for c in got), got[0]["reasons"])

    A.call_llm = stub_llm([{k: v for k, v in c.items() if k != "reasons"} for c in sample_cards()])
    got = A._portal_generate_cards("杭州")
    check("模型漏给 reasons 时字段存在且为空列表",
          all(c.get("reasons") == [] for c in got), [c.get("reasons") for c in got][:1])
    check("漏给不影响卡片数量", len(got) == 6, len(got))

    boiler = sample_cards(reasons=["灵感橱窗内容每日联网更新。", "可一键进入对应工作台。", "第三条。"])
    A.call_llm = stub_llm(boiler)
    got = A._portal_generate_cards("杭州")
    check("话术由 prompt 约束，服务端不擅自改写",
          "每日联网更新" in got[0]["reasons"][0], got[0]["reasons"])

    print("\n[4] /api/portal/hot-showcase：三条路径都带 3 条原因")
    reset_cache()
    A.call_llm = stub_llm(sample_cards())
    client = A.app.test_client()
    data = client.get("/api/portal/hot-showcase?city=杭州").get_json()
    api_cards = data.get("cards") or []
    check("mode 是 ai_online", data.get("mode") == "ai_online", data.get("mode"))
    check("接口返回 6 张卡片", len(api_cards) == 6, len(api_cards))
    check("接口卡片每张 3 条原因", all(len(c.get("reasons") or []) == 3 for c in api_cards),
          [c.get("reasons") for c in api_cards][:1])
    hit = [(c["title"], w) for c in api_cards for r in c["reasons"] for w in BOILERPLATE if w in r]
    check("接口原因不含平台话术", not hit, hit)

    def counting(messages, *args, **kwargs):
        counting.n += 1
        return {"choices": [{"message": {"content": "[]"}}]}
    counting.n = 0
    A.call_llm = counting
    second = client.get("/api/portal/hot-showcase?city=杭州").get_json()
    check("当日缓存命中，未再调模型", counting.n == 0, counting.n)
    check("缓存卡片仍带 3 条原因",
          all(len(c.get("reasons") or []) == 3 for c in second.get("cards") or []))

    reset_cache()
    A.call_llm = stub_llm(None)
    third = client.get("/api/portal/hot-showcase?city=杭州").get_json()
    check("生成失败时 mode 降级", third.get("mode") == "daily_fallback", third.get("mode"))
    fb_cards = third.get("cards") or []
    check("降级卡片每张 3 条原因", all(len(c.get("reasons") or []) == 3 for c in fb_cards),
          [c.get("reasons") for c in fb_cards][:1])
    A.call_llm = real_call_llm

    print("\n[5] 前端接线：只渲染 card.reasons，旧话术已删除")
    with open(DASHBOARD, "r", encoding="utf-8") as f:
        html = f.read()
    for phrase in ("灵感橱窗内容每日联网更新", "可一键进入对应工作台生成行程或营销文案"):
        check(f"页面已删除话术：{phrase[:12]}…", phrase not in html)
    check("buildHotDetail 用 card.reasons", "card.reasons.slice(0, 3)" in html)
    check("缺字段时回落单条 desc，不补话术",
          "[card.desc || card.sub || '结合当季与实时热点生成。']" in html)
    detail_block = re.search(r"function buildHotDetail\(card\).*?\n}", html, re.S)
    check("找到 buildHotDetail", bool(detail_block))
    if detail_block:
        block = detail_block.group(0)
        check("详情构建里不再拼话术", "每日联网更新" not in block and "工作台" not in block)
        check("原因最多取三条", "slice(0, 3)" in block)
    check("推荐原因仍走 escapeHtml 渲染",
          "portal-detail-reasons" in html
          and re.search(r"item\.reasons\.map\(function\s*\(reason\)\s*\{\s*return '<li>' \+ escapeHtml\(reason\)",
                        html) is not None)
    statics = re.findall(r"reasons:\s*\[([^\]]*)\]", html)
    boiler_static = [(i, w) for i, blk in enumerate(statics) for w in BOILERPLATE if w in blk]
    check("默认卡片的原因也不含话术", not boiler_static, boiler_static)
    check("默认卡片原因保持每条 3 项",
          all(len(re.findall(r"'[^']+'", blk)) == 3 for blk in statics if "card.reasons" not in blk),
          [len(re.findall(r"'[^']+'", b)) for b in statics])

    print("\n" + "=" * 64)
    print(f"结果: {_PASS} passed / {_FAIL} failed")
    print("=" * 64)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
