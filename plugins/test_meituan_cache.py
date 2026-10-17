# -*- coding: utf-8 -*-
"""
美团插件结果缓存测试（plugins/meituan.py）
==========================================
美团单次查询实测 11-44s，是对话链路最慢的一环。这条测试钉住三件事：
  1. TTL 内同城同问不再重复打接口（省钱省时），且 origin_query 不参与 key；
  2. 接口超时/繁忙/403/500/空结果时回退最近一次成功数据（stale-if-error），
     但必须标 stale + age_seconds，提示词里明确「这是多久前的数据、仅供参考」；
  3. 超出 stale 上限或本来就没有缓存时，照原样报错——不能拿旧数据假装实时数据。

全程离线：requests.post 被换成假网关，不打真实美团接口、不消耗 Token。

运行：  python plugins/test_meituan_cache.py
"""

from __future__ import annotations

import os
import sys
import threading
from types import SimpleNamespace

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import plugins.meituan as M  # noqa: E402
from plugins.meituan import MeituanTravelPlugin  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}{(' -> ' + str(extra))[:300] if extra else ''}")


ANSWER = (
    "### 杭州西湖周边真实商家数据\n"
    "1. [西湖游船体验](https://www.meituan.com/s/youchuan) ¥55 起，含三潭印月上岛，班次 10 分钟一班\n"
    "2. [如家精选酒店(西湖景区店)](https://www.meituan.com/hotel/123) ￥1XX起/晚，步行 10 分钟到断桥\n"
    "3. [楼外楼(孤山路店)](https://www.meituan.com/shop/456) 人均 ¥180，招牌西湖醋鱼\n"
)

BUSY_ANSWER = "小团忙不过来啦，系统繁忙，请稍后再试，我们会第一时间回复您的。"


class _Timeout(Exception):
    pass


class FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class Gateway:
    """假美团网关：按 mode 决定这一轮返回什么，并把每次请求记进 calls。"""

    exceptions = SimpleNamespace(Timeout=_Timeout, RequestException=Exception)

    def __init__(self, answer=ANSWER):
        self.answer = answer
        self.mode = "ok"
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "body": json, "timeout": timeout,
                           "auth": (headers or {}).get("Authorization")})
        mode = self.mode
        if mode == "timeout":
            raise _Timeout("read timed out")
        if mode == "boom":
            raise RuntimeError("connection reset by peer")
        if mode == "http500":
            return FakeResp(500, {"code": 1, "msg": "internal error"})
        if mode == "http403":
            return FakeResp(403, {"code": 1, "msg": "invalid token"})
        if mode == "busy":
            return FakeResp(200, {"result": "SUCCESS", "code": 0, "data": BUSY_ANSWER})
        if mode == "empty":
            return FakeResp(200, {"result": "SUCCESS", "code": 0, "data": ""})
        return FakeResp(200, {"result": "SUCCESS", "code": 0, "data": self.answer})


class Patched:
    """把插件模块里的 requests 换成假网关，退出时还原。"""

    def __init__(self, gw):
        self.gw = gw

    def __enter__(self):
        self._saved = M.requests
        M.requests = self.gw
        return self.gw

    def __exit__(self, *exc):
        M.requests = self._saved
        return False


class Tuned:
    """临时改缓存参数（TTL / stale 上限 / 条数上限），退出时还原。"""

    def __init__(self, **kw):
        self.kw = kw

    def __enter__(self):
        self._saved = {k: getattr(M, k) for k in self.kw}
        for k, v in self.kw.items():
            setattr(M, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            setattr(M, k, v)
        return False


def new_plugin(token="tk-test"):
    return MeituanTravelPlugin({"meituan_token": token} if token else {})


def params(query="杭州西湖景点门票推荐", city="杭州", origin="杭州有什么好玩的"):
    return {"query": query, "city": city, "origin_query": origin}


def booking_of(result, keyword):
    for b in result.get("bookings") or []:
        if keyword in b["title"]:
            return b
    return None


def section(title):
    print(f"\n[{title}]")


def main():
    print("=" * 60)
    print("美团插件结果缓存测试（TTL 命中 + stale-if-error）")
    print("=" * 60)

    section("1] _fmt_age：秒数说成人话")
    check("0/None → 0 秒", M._fmt_age(None) == "0 秒" and M._fmt_age(0) == "0 秒")
    check("45 → 45 秒", M._fmt_age(45) == "45 秒", M._fmt_age(45))
    check("300 → 5 分钟", M._fmt_age(300) == "5 分钟", M._fmt_age(300))
    check("7200 → 2 小时", M._fmt_age(7200) == "2 小时", M._fmt_age(7200))
    check("172800 → 2 天", M._fmt_age(172800) == "2 天", M._fmt_age(172800))

    section("2] 首次成功写缓存，TTL 内不再打接口（origin_query 不进 key）")
    p = new_plugin()
    with Patched(Gateway()) as gw:
        r1 = p.execute(params(origin="西湖门票多少钱"))
        check("首次返回 ok", r1.get("status") == "ok", r1)
        check("首次不是缓存结果", not r1.get("cached"), r1)
        check("首次打到接口 1 次", len(gw.calls) == 1, len(gw.calls))
        check("缓存里落了 1 条", p.cache_size() == 1, p.cache_size())
        check("answer 是接口原文", r1.get("answer") == ANSWER)

        r2 = p.execute(params(origin="杭州有什么好吃的"))
        check("第二次没有再打接口", len(gw.calls) == 1, len(gw.calls))
        check("第二次标 cached", r2.get("cached") is True, r2)
        check("第二次不是 stale", r2.get("stale") is False, r2)
        check("带 age_seconds", isinstance(r2.get("age_seconds"), int) and r2["age_seconds"] >= 0,
              r2.get("age_seconds"))
        check("缓存命中仍是 ok + 原文", r2.get("status") == "ok" and r2.get("answer") == ANSWER)
        check("预订卡片照旧抽出", len(r2.get("bookings") or []) >= 3, r2.get("bookings"))

        b1 = booking_of(r1, "楼外楼")
        b2 = booking_of(r2, "楼外楼")
        check("无业态词标题按 origin_query 判意图（门票→ticket）",
              b1 and b1["type"] == "ticket", b1)
        check("同一份缓存换 origin_query 重算卡片（美食→item）",
              b2 and b2["type"] == "item", b2)
        check("价格仍从链接行提取", b2 and b2.get("price") == "180", b2 and b2.get("price"))

        p.execute(params(query="杭州酒店住宿推荐", origin="杭州住哪好"))
        check("不同 query 是不同缓存条目", p.cache_size() == 2, p.cache_size())
        check("不同 query 会重新打接口", len(gw.calls) == 2, len(gw.calls))

    check("city 参与 key", p._cache_key("杭州", "q", "default") != p._cache_key("苏州", "q", "default"))
    check("channel 参与 key", p._cache_key("杭州", "q", "default") != p._cache_key("杭州", "q", "vip"))
    check("origin_query 不参与 key（同 query 同 key）",
          p._cache_key("杭州", "q", "default") == p._cache_key("杭州", "q", "default"))

    section("3] TTL 过期后重新打接口，且不把过期结果标成 cached")
    p3 = new_plugin()
    with Patched(Gateway()) as gw3, Tuned(CACHE_TTL=0):
        a = p3.execute(params())
        b = p3.execute(params())
        check("TTL=0 时两次都打接口", len(gw3.calls) == 2, len(gw3.calls))
        check("过期命中不算 cached", not b.get("cached"), b)
        check("结果仍是 ok", a.get("status") == "ok" and b.get("status") == "ok")

    section("4] stale-if-error：接口挂了回退旧数据，但标明是旧数据")
    fail_modes = {
        "timeout": "超时",
        "boom": "请求失败",
        "http500": "HTTP 500",
        "http403": "403",
        "busy": "繁忙",
        "empty": "未返回有效结果",
    }
    for mode, hint in fail_modes.items():
        pm = new_plugin()
        # CACHE_TTL=0：让第二次调用必须真打接口（否则 TTL 内直接命中缓存，测不到回退分支）
        with Patched(Gateway()) as gwm, Tuned(CACHE_TTL=0):
            ok = pm.execute(params())
            before = len(gwm.calls)
            gwm.mode = mode
            r = pm.execute(params())
            check(f"{mode}: 回退后仍是 ok", r.get("status") == "ok", r)
            check(f"{mode}: 标 stale", r.get("stale") is True, r)
            check(f"{mode}: 带 age_seconds", isinstance(r.get("age_seconds"), int), r)
            check(f"{mode}: 记下降级原因", hint in str(r.get("degraded_reason", "")),
                  r.get("degraded_reason"))
            check(f"{mode}: 回退的是接口原文", r.get("answer") == ANSWER)
            check(f"{mode}: 卡片仍可用", len(r.get("bookings") or []) >= 3, r.get("bookings"))
            check(f"{mode}: 确实尝试过打接口", len(gwm.calls) == before + 1, len(gwm.calls))
            text = pm.format_for_llm(r)
            check(f"{mode}: 提示词带 ⚠️ 与「仅供参考」",
                  "⚠️" in text and "仅供参考" in text, text[:120])
            check(f"{mode}: 提示词不再自称「实时数据」", "美团实时数据" not in text, text[:80])
            check(f"{mode}: 提示词标明数据时间", "前缓存" in text, text[:80])

    section("5] 没有缓存 / 超出 stale 上限：照原样报错，不拿旧数据冒充")
    p5 = new_plugin()
    with Patched(Gateway()) as gw5:
        gw5.mode = "busy"
        r = p5.execute(params())
        check("冷启动遇繁忙 → status busy", r.get("status") == "busy", r)
        check("冷启动遇繁忙 → 保留原文案", "繁忙" in r.get("message", ""), r.get("message"))
        check("冷启动不会伪造 stale 字段", "stale" not in r, r)
        gw5.mode = "timeout"
        r = p5.execute(params())
        check("冷启动遇超时 → status error", r.get("status") == "error", r)
        check("冷启动遇超时 → 文案可读", "超时" in r.get("message", ""), r.get("message"))

    p5b = new_plugin()
    with Patched(Gateway()) as gw5b, Tuned(CACHE_TTL=0, CACHE_STALE_MAX=-1):
        p5b.execute(params())
        gw5b.mode = "http500"
        r = p5b.execute(params())
        check("超上限不回退 → status error", r.get("status") == "error", r)
        check("超上限不回退 → 透出 HTTP 500", "500" in r.get("message", ""), r.get("message"))

    section("6] 缓存条数有上限（不会无界吃内存）")
    p6 = new_plugin()
    with Patched(Gateway()) as gw6, Tuned(CACHE_MAX_ENTRIES=2):
        for i in range(3):
            p6.execute(params(query=f"杭州查询{i}"))
        check("条数不超过上限", p6.cache_size() <= 2, p6.cache_size())
        check("三次都真打了接口", len(gw6.calls) == 3, len(gw6.calls))
        n = len(gw6.calls)
        p6.execute(params(query="杭州查询2"))
        check("最新一条仍在缓存里（命中不打接口）", len(gw6.calls) == n, len(gw6.calls))

    section("7] 并发（waitress 8 线程）下缓存不串数据、不炸")
    p7 = new_plugin()
    errors = []
    results = []
    lock = threading.Lock()

    def worker(idx):
        try:
            for j in range(5):
                q = f"杭州并发查询{idx % 4}"
                r = p7.execute(params(query=q, origin=f"用户{idx}问{j}"))
                with lock:
                    results.append((q, r))
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(repr(exc))

    with Patched(Gateway()):
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    check("并发无异常", not errors, errors[:2])
    check("并发结果全部 ok", all(r.get("status") == "ok" for _, r in results), len(results))
    check("并发 40 次调用都有结果", len(results) == 40, len(results))
    check("缓存条数=不同 query 数(4)", p7.cache_size() == 4, p7.cache_size())
    check("同 query 拿到的正文一致",
          len({r.get("answer") for q, r in results if q == "杭州并发查询0"}) == 1)

    section("8] format_for_llm：三种新鲜度的措辞各不相同")
    p8 = new_plugin()
    with Patched(Gateway()) as gw8:
        live = p8.execute(params())
        cached = p8.execute(params())
        gw8.mode = "timeout"
        with Tuned(CACHE_TTL=0):  # 强制真打接口，才能走到回退分支
            stale = p8.execute(params())
    t_live = p8.format_for_llm(live)
    t_cached = p8.format_for_llm(cached)
    t_stale = p8.format_for_llm(stale)
    check("实时：标题是「美团实时数据」", "美团实时数据" in t_live, t_live[:60])
    check("实时：不提缓存", "命中缓存" not in t_live, t_live[:200])
    check("缓存命中：标明抓取时间", "命中缓存" in t_cached and "抓取于" in t_cached, t_cached[-200:])
    check("缓存命中：仍按实时数据引用", "美团实时数据" in t_cached)
    check("回退：标题带缓存时间", "前缓存" in t_stale.split("\n")[0], t_stale.split("\n")[0])
    check("回退：要求先告知用户", "必须先说明" in t_stale, t_stale[-260:])
    check("三种文案都不输出预订链接",
          all("https://" not in t for t in (t_live, t_cached, t_stale)))
    check("三种文案都保留正文商家名", all("楼外楼" in t for t in (t_live, t_cached, t_stale)))
    check("失败结果仍走原查询失败文案",
          p8.format_for_llm({"status": "error", "message": "x"}) == "[美团酒旅] 查询失败: x")

    section("9] 配置/入参校验不受缓存影响")
    with Patched(Gateway()) as gw9:
        check("未配 Token → not_configured",
              new_plugin(token="").execute(params()).get("status") == "not_configured")
        check("空 query → error", p.execute({"query": "  ", "city": "杭州"}).get("status") == "error")
        check("上述两种情况不打接口也不写缓存", len(gw9.calls) == 0, len(gw9.calls))

    total = _PASS + _FAIL
    print("\n" + "=" * 60)
    print(f"结果：{_PASS} passed, {_FAIL} failed（共 {total}）")
    print("=" * 60)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
