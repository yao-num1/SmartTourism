# -*- coding: utf-8 -*-
"""
模型用量计量单元测试
====================
「运行成本」要能在答辩里报数，就得先证明这些数是真从调用里累出来的，而不是手写的。
覆盖：
  1. token 折算函数（只在拿不到上游 usage 时用）；
  2. 非流式调用：上游带 usage → 记为实测；不带 → 记为折算，两种来源分开计数；
  3. 首字耗时与总耗时入账，平均/最大值正确；
  4. 记账是旁路：喂垃圾参数、全局账本被破坏都不能抛异常，也不能影响答复返回；
  5. /api/admin/ops 暴露 llm_usage 段；未配单价时只报 token、不编造金额；
  6. 配了单价才折算费用，且折算是「上限估计」不是账单（口径写在返回里）；
  7. 没有偷偷给上游请求加 stream_options 之类会改变行为的新字段。

用临时库（SMARTTOURISM_DB），全程离线（requests 被替身接管），不碰 data/ 下任何真实数据。

运行：  python server/test_llm_usage.py
"""

from __future__ import annotations

import os
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

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="st_llmusage_test_"), "test.db")
os.environ["SMARTTOURISM_DB"] = _TMPDB
os.environ.setdefault("SMARTTOURISM_MEMORY", "0")
os.environ.setdefault("SMARTTOURISM_LLM_CACHE", "0")
# 单价默认不配：先验证「没有单价就只报 token」这条口径
os.environ.pop("SMARTTOURISM_PRICE_IN_PER_M", None)
os.environ.pop("SMARTTOURISM_PRICE_OUT_PER_M", None)

import app as A  # noqa: E402
import requests  # noqa: E402

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


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.text = ""
        self._body = body if body is not None else {"choices": [{"message": {"content": "ok"}}]}
        self.encoding = "utf-8"

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.exceptions.HTTPError(f"HTTP {self.status_code}")
            err.response = self
            raise err

    def json(self):
        return self._body

    def close(self):
        pass


class ShimRequests:
    """替身 requests：按脚本返回响应，并记下发出去的 payload（用于确认没加新字段）。"""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.sent = []
        self.exceptions = requests.exceptions

    def Request(self, method, url, headers=None, json=None):
        outer = self

        class _Req:
            def prepare(self):
                p = type("P", (), {})()
                p._info = {"payload": json}
                p._outer = outer
                return p

        return _Req()

    class _Session:
        def send(self, prep, stream=False, timeout=None):
            outer = prep._outer
            outer.sent.append(prep._info["payload"])
            kind, val = outer.outcomes.pop(0)
            if kind == "raise":
                raise val
            return val

    def Session(self):
        return ShimRequests._Session()


def snapshot():
    return {k: v for k, v in A._LLM_USAGE.items() if k != "by_skill"}


def run_with(body):
    """把 app.requests 换成替身，跑一次非流式 call_llm，返回 (结果, 替身)。"""
    shim = ShimRequests([("ok", FakeResp(body=body))])
    old = A.requests
    A.requests = shim
    try:
        return A.call_llm([{"role": "user", "content": "杭州西湖怎么玩"}],
                          stream=False, skill_name="plan"), shim
    finally:
        A.requests = old


# ==================== [1] token 折算 ====================
section("\n[1] 拿不到上游 usage 时的字符折算")
check("空串折成 0", A._est_tokens_from_chars("") == 0)
cjk = A._est_tokens_from_chars("杭州西湖很美")     # 6 个中文字
check("中文按 0.75 token/字折算并向上取整", cjk == int(6 * 0.75) + 1, cjk)
asc = A._est_tokens_from_chars("a" * 40)
check("ASCII 按 4 字符 1 token", asc == 40 // 4 + 1, asc)
check("更长的正文折算不减", A._est_tokens_from_chars("杭州西湖很美杭州") > cjk)

# ==================== [2] 实测与折算分开计数 ====================
section("\n[2] 非流式调用入账")
before = snapshot()
res, shim = run_with({"choices": [{"message": {"content": "一天的行程"}}],
                      "usage": {"prompt_tokens": 1234, "completion_tokens": 567,
                                "total_tokens": 1801}})
after = snapshot()
check("上游带 usage 时照常返回结果", "choices" in res, res)
check("实测调用计数 +1", after["upstream_calls"] == before["upstream_calls"] + 1)
check("折算调用计数不变", after["estimated_calls"] == before["estimated_calls"])
check("输入 token 用的是上游真实值", after["prompt_tokens"] - before["prompt_tokens"] == 1234)
check("输出 token 用的是上游真实值", after["completion_tokens"] - before["completion_tokens"] == 567)
check("总调用数 +1", after["calls"] == before["calls"] + 1)
check("按 skill 分账", A._LLM_USAGE["by_skill"]["plan"]["calls"] >= 1)

before = snapshot()
res2, _ = run_with({"choices": [{"message": {"content": "西湖十景，半日足矣"}}]})
after = snapshot()
check("上游没给 usage 时不影响答复", "choices" in res2)
check("没 usage 就记成折算调用", after["estimated_calls"] == before["estimated_calls"] + 1)
check("折算 token 大于 0", after["prompt_tokens"] > before["prompt_tokens"] and
      after["completion_tokens"] > before["completion_tokens"])

# ==================== [3] 耗时与首字 ====================
section("\n[3] 耗时与首字延迟")
before = snapshot()
A._note_llm_call("chat", 2.5, [{"role": "user", "content": "x" * 40}], "正文", None, 0.5)
after = snapshot()
check("总耗时入账", after["duration_ms_total"] - before["duration_ms_total"] == 2500)
check("最大耗时更新", after["duration_ms_max"] >= 2500)
check("首字样本数 +1", after["first_token_count"] == before["first_token_count"] + 1)
check("首字耗时入账", after["first_token_ms_total"] - before["first_token_ms_total"] == 500)
before = snapshot()
A._note_llm_call("chat", 1.0, [], "正文", None)          # 非流式没有首字
after = snapshot()
check("没有首字就不增加首字样本", after["first_token_count"] == before["first_token_count"])
st = A._llm_usage_stats()
snap = snapshot()
check("平均首字只按有首字的样本算",
      st["avg_first_token_ms"] == round(snap["first_token_ms_total"] / snap["first_token_count"]),
      (st["avg_first_token_ms"], snap))
check("首字样本数在 stats 里可见", st["stream_calls_with_first_token"] >= 1)

# ==================== [4] 记账是旁路，不能把答复带崩 ====================
section("\n[4] 记账异常不外泄")
try:
    A._note_llm_call("chat", "不是秒数", "消息不是列表", None, "usage不是字典", "首字不是数")
    check("喂垃圾参数不抛异常", True)
except Exception as e:  # noqa: BLE001
    check("喂垃圾参数不抛异常", False, e)
_saved = A._LLM_USAGE
try:
    A._LLM_USAGE = None
    A._note_llm_call("chat", 0.1, [{"role": "user", "content": "x"}], "y", None)
    check("账本被破坏也照常吞掉，不影响调用方", True)
except Exception as e:  # noqa: BLE001
    check("账本被破坏也照常吞掉，不影响调用方", False, e)
finally:
    A._LLM_USAGE = _saved
before_calls = snapshot()["calls"]
res3, _ = run_with({"choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 3}})
check("账本坏过一次后仍能继续记账", snapshot()["calls"] == before_calls + 1)

# ==================== [5] 运维接口口径 ====================
section("\n[5] /api/admin/ops 的 llm_usage 段")
os.environ.pop("SMARTTOURISM_PRICE_IN_PER_M", None)
os.environ.pop("SMARTTOURISM_PRICE_OUT_PER_M", None)
client = A.app.test_client()
ops = client.get("/api/admin/ops").get_json()
lu = ops.get("llm_usage") or {}
check("ops 里有 llm_usage 段", isinstance(lu, dict) and "calls" in lu, list((lu or {}).keys())[:6])
for k in ("prompt_tokens", "completion_tokens", "upstream_calls", "estimated_calls",
          "avg_duration_ms", "by_skill", "token_source", "model"):
    check(f"llm_usage 带 {k} 字段", k in lu)
check("未配单价时不报金额", lu.get("cost") is None)
check("未配单价时说明为什么没有钱", "单价" in (lu.get("cost_note") or ""))
check("口径里写清实测与折算的区分", "estimated" in (lu.get("token_source") or ""))
check("报的是当前真实模型名而不是写死的字符串", bool(lu.get("model")))

# ==================== [6] 配了单价才折算费用 ====================
section("\n[6] 单价折算")
os.environ["SMARTTOURISM_PRICE_IN_PER_M"] = "2"
os.environ["SMARTTOURISM_PRICE_OUT_PER_M"] = "8"
c = A._llm_usage_stats()["cost"]
check("配了单价才有 cost", isinstance(c, dict), c)
expect = (A._LLM_USAGE["prompt_tokens"] / 1e6 * 2
          + A._LLM_USAGE["completion_tokens"] / 1e6 * 8)
check("金额按输入/输出分别折算", abs(c["amount"] - round(expect, 6)) < 1e-9,
      (c["amount"], expect))
check("金额口径标明含折算部分", "estimated" in c["note"])
os.environ.pop("SMARTTOURISM_PRICE_IN_PER_M", None)
os.environ.pop("SMARTTOURISM_PRICE_OUT_PER_M", None)

# ==================== [7] 不给上游加新字段 ====================
section("\n[7] 请求体没有被计量改动")
_, shim7 = run_with({"choices": [{"message": {"content": "ok"}}],
                     "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
check("没有偷偷加 stream_options 之类会改变上游行为的字段",
      all("stream_options" not in p for p in shim7.sent), shim7.sent)
check("payload 仍是标准字段",
      all(set(p.keys()) <= {"model", "messages", "temperature", "max_tokens", "stream",
                            "tools", "tool_choice"} for p in shim7.sent),
      [sorted(p.keys()) for p in shim7.sent])

src = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
check("对话流式站点捕获 usage 且防住空 choices",
      "if chunk.get('usage')" in src and "chunk.get('choices') or [{}]" in src)
check("流式在循环外补记一次账（含首字与中断也记）",
      src.count("_note_llm_call(") >= 4)
admin_src = open(os.path.join(_HERE, "ops_log.py"), encoding="utf-8").read()
check("运维面板渲染 token 实测/折算两个计数",
      "token 实测调用" in admin_src and "token 折算调用" in admin_src)

# 营销/文创这两个独立接口没有 Skill 名，不标就全挤进「(未标注)」，
# 成本表按能力就分不开——所以标签必须真的传到位（且不参与 temperature）
seg = src[src.index("def _stream_llm_response"):src.index("def _stream_llm_response") + 3000]
check("流式辅助函数有独立的用量标签参数", "usage_label" in seg)
check("记账用标签兜底而不是丢掉", "skill_name or usage_label" in seg)
check("temperature 仍只看 skill_name，标签不影响生成参数",
      "skill_name=skill_name" in seg and "temperature=usage_label" not in seg)
check("营销接口带了用量标签", '_stream_llm_response(messages, usage_label="marketing")' in src)
check("文创接口带了用量标签",
      '_stream_llm_response(messages, usage_label="creative_product")' in src)
_before = A._llm_usage_stats()["by_skill"].get("marketing", {}).get("calls", 0)
A._note_llm_call("marketing", 0.5, [{"content": "问题"}], "答复",
                 {"prompt_tokens": 7, "completion_tokens": 3})
_after = A._llm_usage_stats()["by_skill"].get("marketing", {})
check("标签落进 by_skill 分组并单独计数", _after.get("calls") == _before + 1, _after)
check("标签分组里 token 与耗时同账",
      _after.get("prompt_tokens", 0) >= 7 and _after.get("duration_ms_total", 0) >= 500, _after)

# ==================== [8] 计量不许把答复带崩 ====================
section("\n[8] 时钟取不到时答复仍要交付")


class _Clockless:
    """只给 sleep、不给 time 的时钟替身（test_llm_retry 打进去的就是这种形状）。"""

    @staticmethod
    def sleep(seconds):
        return None


shim8 = ShimRequests([("ok", FakeResp(body={
    "choices": [{"message": {"content": "照旧交付"}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5}}))])
_old_req, _old_time = A.requests, A.time
_max_before = A._LLM_USAGE["duration_ms_max"]
_calls_before = A._LLM_USAGE["calls"]
A.requests, A.time = shim8, _Clockless()
try:
    r8 = A.call_llm([{"role": "user", "content": "问题"}], skill_name="plan")
finally:
    A.requests, A.time = _old_req, _old_time
check("时钟不可用时答复仍完整返回",
      (r8.get("choices") or [{}])[0].get("message", {}).get("content") == "照旧交付", r8)
check("取不到时间就记 0，不编一个耗时出来",
      A._LLM_USAGE["duration_ms_max"] == _max_before, (_max_before, A._LLM_USAGE["duration_ms_max"]))
check("时钟坏了照样入账（不能悄悄漏计）",
      A._LLM_USAGE["calls"] == _calls_before + 1)

print("\n" + "=" * 64)
print(f"结果: {_PASS} passed / {_FAIL} failed")
print("=" * 64)
sys.exit(0 if _FAIL == 0 else 1)
