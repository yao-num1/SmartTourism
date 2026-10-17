# -*- coding: utf-8 -*-
"""
提示词前缀稳定化 + 缓存命中计量测试
==================================
省 token 的做法是把「每次都会变的值」（精确到分钟的时钟、IP 定位）全部排到提示词末尾，
让头部逐字不动——DeepSeek 的上下文缓存按**前缀**命中，命中价远低于未命中价。
但「前缀稳定了」和「真的命中了」是两件事，所以同时要求缓存命中 token 进台账、进面板。
覆盖：
  1. 静态头逐字不动：换时间、换定位、切 Skill，marker 之前的部分必须完全一致；
  2. 动态段只出现在末尾：时钟串整仓只有一处，且排在「本次请求的动态信息」之后；
  3. 头部仍然指路「用末尾给的时间」（挪走不能丢语义）；
  4. 五处提示词构建都接了尾段（含行程/营销/文创/博客四段内联 prompt）；
  5. _cache_tokens_from_usage：DeepSeek 形、OpenAI 形、缺字段、喂垃圾；
  6. 命中/未命中 token 累进入账本，折算调用不计命中（没有 usage 就不许报命中）；
  7. 命中率口径：没上报时是 None，不假装 0% 也不假装 100%；配了单价时金额只高估不低估；
  8. /api/admin/ops 与运维面板都暴露「缓存命中率」。

用临时库（SMARTTOURISM_DB），全程离线，不碰 data/ 下任何真实数据。

运行：  python server/test_prompt_cache.py
"""

from __future__ import annotations

import os
import re as _re
import sys
import tempfile
from datetime import datetime as _dt

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

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="st_prefix_test_"), "test.db")
os.environ["SMARTTOURISM_DB"] = _TMPDB
os.environ.setdefault("SMARTTOURISM_MEMORY", "0")
os.environ.setdefault("SMARTTOURISM_LLM_CACHE", "0")
os.environ.pop("SMARTTOURISM_PRICE_IN_PER_M", None)
os.environ.pop("SMARTTOURISM_PRICE_OUT_PER_M", None)

import app as A  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}{(' -> ' + str(extra)) if extra else ''}")


def section(t):
    print("\n" + t)


class FixedDatetime(_dt):
    """冻结时钟：只改 now()，其余（strftime/weekday）继承自真实 datetime。"""

    _frozen = None

    @classmethod
    def now(cls, tz=None):
        return cls._frozen


def at(iso, fn):
    """在「时间=iso」下执行 fn()，用完恢复原 datetime。"""
    old = A.datetime
    FixedDatetime._frozen = _dt.fromisoformat(iso)
    A.datetime = FixedDatetime
    try:
        return fn()
    finally:
        A.datetime = old


MARKER = "## 本次请求的动态信息"


def head(prompt: str) -> str:
    """marker 之前的部分——这段必须逐字不动才可能命中缓存。"""
    return prompt.split(MARKER)[0]


_SRC = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()

# ==================== [1] 静态头逐字不动 ====================
section("\n[1] 头部稳定：变的是尾部，不是前缀")
p_a = at("2026-10-03 09:12:00", lambda: A.build_system_prompt())
p_b = at("2026-11-27 23:59:00", lambda: A.build_system_prompt())
check("跨分钟/跨月：静态头逐字一致", head(p_a) == head(p_b))
check("但整段提示词确实带了本次时间（没把时钟弄丢）", p_a != p_b and "2026年10月03日" in p_a)
loc1 = at("2026-10-03 09:12:00", lambda: A.build_system_prompt(user_location="南宁市 青秀区"))
loc2 = at("2026-10-03 09:13:00", lambda: A.build_system_prompt(user_location="桂林市 象山区"))
check("换定位不影响头部", head(loc1) == head(loc2))
check("定位在尾部且当前时间也在尾部",
      MARKER in loc1 and loc1.index(MARKER) < loc1.index("南宁市 青秀区"))
no_loc = A.build_system_prompt()
check("没定位时写明确的『未获取到位置』而不是整行消失",
      "用户当前位置（出发点）：未获取到位置" in no_loc.split(MARKER)[1])

sk = A.build_system_prompt(skill_name="tourism-advisor")
check("通用头是 Skill 头的前缀（无 Skill 的请求也共享这段前缀）", sk.startswith(head(A.build_system_prompt())))
check("Skill 段排在动态段之前（Skill 变了才作废，时间变了不作废）",
      sk.index("## 当前激活的 Skill") < sk.index(MARKER))
unknown = A.build_system_prompt(skill_name="不存在的skill")
check("未注册的 Skill 不产生半截段落", "当前激活的 Skill" not in unknown)

# ==================== [2] 动态段只在末尾 ====================
section("\n[2] 时钟只有一处，且排在末尾")
check("marker 之前不出现任何日期（前缀里没有会变的值）",
      all(_re.search(r"\d{4}年\d{2}月\d{2}日", head(p)) is None
          for p in (p_a, loc1, sk, no_loc)))
check("『当前准确时间』全文件只出现 1 次（不可能在别的头部重复）",
      _SRC.count("当前准确时间") == 1, _SRC.count("当前准确时间"))
check("提示词里星期几跟着本次时间走",
      "(星期六)" in at("2026-10-03 09:12:00", lambda: A.build_system_prompt()))

# ==================== [3] 挪走不能丢语义 ====================
section("\n[3] 头部仍指路到末尾")
check("头部要求用末尾给出的时间",
      "必须使用本提示词末尾「本次请求的动态信息」给出的时间" in head(no_loc))
check("头部说清位置取值见末尾",
      "位置取值见末尾「本次请求的动态信息」" in head(no_loc))
check("出发点≠目的地这条规矩仍在头部（不是被一起挪走了）",
      "绝不能把当前位置当作目的地" in head(no_loc))

# ==================== [4] 五处构建都接了尾段 ====================
section("\n[4] 行程/营销/文创/博客的 prompt 同样头部不带时钟")
check("_dynamic_prompt_tail 被调用 5 处（1 处定义 + 5 处使用 = 6）",
      _SRC.count("_dynamic_prompt_tail(") == 6, _SRC.count("_dynamic_prompt_tail("))
check("4 段内联 prompt 以拼接方式接尾段",
      _SRC.count('""" + _dynamic_prompt_tail') == 4,
      _SRC.count('""" + _dynamic_prompt_tail'))
check("没有任何 prompt 头部还写死 datetime.now() 的当前时间",
      "当前时间：{datetime.now()" not in _SRC and "当前时间：{now" not in _SRC)

# ==================== [5] usage 里的缓存字段 ====================
section("\n[5] _cache_tokens_from_usage 的四种上游")
check("DeepSeek 形：直接给 hit/miss",
      A._cache_tokens_from_usage({"prompt_cache_hit_tokens": 2048,
                                  "prompt_cache_miss_tokens": 12}) == (2048, 12))
check("OpenAI 形：只有 cached_tokens 时未命中=总数-命中",
      A._cache_tokens_from_usage({"prompt_tokens": 1000,
                                  "prompt_tokens_details": {"cached_tokens": 768}}) == (768, 232))
check("cached_tokens 大于总数时未命中不可能是负数",
      A._cache_tokens_from_usage({"prompt_tokens": 100,
                                  "prompt_tokens_details": {"cached_tokens": 500}}) == (500, 0))
check("上游没报缓存字段 → (0,0)，不猜",
      A._cache_tokens_from_usage({"prompt_tokens": 10, "completion_tokens": 5}) == (0, 0))
check("喂垃圾不抛异常", A._cache_tokens_from_usage(None) == (0, 0)
      and A._cache_tokens_from_usage({"prompt_cache_hit_tokens": "abc"}) == (0, 0))

# ==================== [6] 命中 token 入账 ====================
section("\n[6] 记账：只有实测调用才计命中")
before = dict(A._LLM_USAGE)
b_hit, b_miss = before["cache_hit_tokens"], before["cache_miss_tokens"]
msgs = [{"role": "system", "content": A.build_system_prompt()},
        {"role": "user", "content": "杭州西湖怎么玩"}]
A._note_llm_call("plan", 1.0, msgs, "答复",
                 usage={"prompt_tokens": 2060, "completion_tokens": 300,
                        "prompt_cache_hit_tokens": 2048, "prompt_cache_miss_tokens": 12},
                 first_token_seconds=0.4)
A._note_llm_call("plan", 0.5, msgs, "答复", usage=None)  # 折算调用
d = A._llm_usage_stats()
check("命中 token 累加", d["cache_hit_tokens"] - b_hit == 2048,
      d["cache_hit_tokens"] - b_hit)
check("未命中 token 累加", d["cache_miss_tokens"] - b_miss == 12,
      d["cache_miss_tokens"] - b_miss)
check("折算调用不贡献命中（没 usage 就不许报命中）",
      d["estimated_calls"] > before["estimated_calls"] and
      d["cache_hit_tokens"] - b_hit == 2048)

# 记账仍是旁路：喂垃圾参数不能带崩答复
A._note_llm_call("plan", 0.1, None, "", usage={"prompt_cache_hit_tokens": None})
check("垃圾 usage 不抛异常", True)

# ==================== [7] 命中率与金额口径 ====================
section("\n[7] 命中率口径")
A._LLM_USAGE.update({"cache_hit_tokens": 2048, "cache_miss_tokens": 512})
st = A._llm_usage_stats()
check("命中率=命中/(命中+未命中)", st["cache_hit_rate"] == 0.8, st["cache_hit_rate"])
check("口径写明只有实测调用才有", "实测" in (st.get("cache_note") or ""))
check("口径写明按前缀命中", "前缀" in (st.get("cache_note") or ""))
A._LLM_USAGE.update({"cache_hit_tokens": 0, "cache_miss_tokens": 0})
check("上游什么都没报时命中率是 None（不假装 0%）",
      A._llm_usage_stats()["cache_hit_rate"] is None)
os.environ["SMARTTOURISM_PRICE_IN_PER_M"] = "2"
os.environ["SMARTTOURISM_PRICE_OUT_PER_M"] = "8"
A._LLM_USAGE.update({"cache_hit_tokens": 1000, "cache_miss_tokens": 100})
c = A._llm_usage_stats()["cost"]
check("有命中时金额仍按未命中单价乘（只高估不低估）",
      "命中" in c["note"] and "只高估不低估" in c["note"], c["note"])
check("金额没有偷偷按命中价打折",
      c["amount"] == round(A._LLM_USAGE["prompt_tokens"] / 1e6 * 2
                           + A._LLM_USAGE["completion_tokens"] / 1e6 * 8, 6))
os.environ.pop("SMARTTOURISM_PRICE_IN_PER_M", None)
os.environ.pop("SMARTTOURISM_PRICE_OUT_PER_M", None)

# ==================== [8] 接口与面板 ====================
section("\n[8] /api/admin/ops 与运维面板")
client = A.app.test_client()
lu = (client.get("/api/admin/ops").get_json() or {}).get("llm_usage") or {}
for k in ("cache_hit_tokens", "cache_miss_tokens", "cache_hit_rate", "cache_note"):
    check(f"ops 的 llm_usage 带 {k}", k in lu)
check("ops 面板拿到的是同一个账本", lu.get("calls", 0) >= 1)
_ops_src = open(os.path.join(_HERE, "ops_log.py"), encoding="utf-8").read()
check("面板渲染缓存命中率", "缓存命中率" in _ops_src and "cache_hit_rate" in _ops_src)
check("面板在没数据时显示『上游未报』而不是 0.0%", "上游未报" in _ops_src)
check("面板展示命中/未命中的绝对量", "cache_hit_tokens" in _ops_src
      and "cache_miss_tokens" in _ops_src)
_meas = open(os.path.join(_HERE, "measure_llm_cost.py"), encoding="utf-8").read()
check("实测脚本也抓缓存命中", "prompt_cache_hit_tokens" in _meas
      or "cache_hit_tokens" in _meas)
check("实测脚本说明为什么前缀稳定会命中", "前缀" in _meas)

print(f"\n{'=' * 46}\n结果: {_PASS} passed / {_FAIL} failed"
      f"（共 {_PASS + _FAIL}）")
sys.exit(1 if _FAIL else 0)
