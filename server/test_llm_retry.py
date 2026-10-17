# -*- coding: utf-8 -*-
"""
LLM 发送层容错单元测试（重试 + 备用通道）
==========================================
覆盖 _llm_send / _call_llm_raw 的降级行为，全程离线（requests 被替身接管）：
  1. 默认参数与「只重试瞬态故障」的错误分类（401/402 不等 120 秒）；
  2. 超时后第二次成功、持续失败时的退避序列（指数）；
  3. 备用通道：仅主通道耗尽后才切，切换时改 model / Authorization，且不改坏主通道入参；
  4. 未配置备用通道时行为与改动前一致（只打主通道）；
  5. 非 2xx 的响应体被显式 close；200 但正文不是 JSON 时按既有错误契约返回。

用临时库（SMARTTOURISM_DB），不碰 data/smarttourism.db。

运行：  python server/test_llm_retry.py
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

# DB_PATH 在 database 模块加载时求值，必须先设好再 import app
_TMPDB = os.path.join(tempfile.mkdtemp(prefix="st_llmretry_test_"), "test.db")
os.environ["SMARTTOURISM_DB"] = _TMPDB
os.environ.setdefault("SMARTTOURISM_MEMORY", "0")
os.environ.setdefault("SMARTTOURISM_LLM_CACHE", "0")

import requests  # noqa: E402
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


# ==================== requests 替身 ====================

class FakeResp:
    """够用的 Response 替身：只实现 _llm_send / call_llm 用到的那几个方法。"""

    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self.text = text
        self._body = body if body is not None else {
            "choices": [{"message": {"content": "ok"}}]}
        self.closed = 0
        self.encoding = "utf-8"

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.exceptions.HTTPError(f"HTTP {self.status_code}")
            err.response = self
            raise err

    def json(self):
        if isinstance(self._body, ValueError):
            raise self._body
        return self._body

    def close(self):
        self.closed += 1


class ShimRequests:
    """替换 app.requests：Request(...).prepare() 记录入参，Session().send() 按脚本给结果。"""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []          # 每次 send 的 (url, payload, headers, stream, timeout)
        self.exceptions = requests.exceptions

    def Request(self, method, url, headers=None, json=None):
        outer = self

        class _Req:
            def __init__(self, *a, **kw):
                pass

            def prepare(self):
                info = {"url": url, "headers": dict(headers or {}), "payload": json}

                class _Prepared:
                    pass

                p = _Prepared()
                p._info = info
                p._outer = outer
                return p

        return _Req(method, url, headers=headers, json=json)

    class _Session:
        def send(self, prep, stream=False, timeout=None):
            outer = prep._outer
            info = prep._info
            outer.calls.append((info["url"], info["payload"], info["headers"], stream, timeout))
            if not outer.outcomes:
                raise AssertionError("替身脚本已用尽，却还在发请求")
            kind, val = outer.outcomes.pop(0)
            if kind == "raise":
                raise val
            return val

    def Session(self):
        return self._Session()


class TimeShim:
    def __init__(self):
        self.sleeps = []

    def sleep(self, s):
        self.sleeps.append(round(s, 4))


MAIN = "https://main.example.com/v1/chat/completions"
FB = "https://fb.example.com/v1/chat/completions"


def run_send(outcomes, *, retry=1, wait=0.0, fallback=None):
    """在受控配置下调用一次 _llm_send，返回 (resp, err, shim, times)。"""
    shim = ShimRequests(outcomes)
    clock = TimeShim()
    old = (A.requests, A.time, A.LLM_RETRY, A.LLM_RETRY_WAIT, A._LLM_FALLBACK, A.app.logger.disabled)
    fb = None
    if fallback is not None:
        # 走生产同一条拼装路径：只给 base_url，函数负责补 /chat/completions
        fb = A._build_llm_fallback({
            "SMARTTOURISM_LLM_FALLBACK_BASE_URL": fallback["url"],
            "SMARTTOURISM_LLM_FALLBACK_KEY": fallback.get("key", ""),
            "SMARTTOURISM_LLM_FALLBACK_MODEL": fallback.get("model", ""),
        })
    try:
        A.requests = shim
        A.time = clock
        A.LLM_RETRY = retry
        A.LLM_RETRY_WAIT = wait
        A._LLM_FALLBACK = fb
        A.app.logger.disabled = True
        resp, err = A._llm_send(MAIN, {"Authorization": "Bearer MAIN-KEY"},
                                {"model": "main-model", "messages": [{"role": "user", "content": "hi"}]},
                                stream=False, timeout=5)
    finally:
        A.requests, A.time, A.LLM_RETRY, A.LLM_RETRY_WAIT, A._LLM_FALLBACK = old[:5]
        A.app.logger.disabled = old[5]
    return resp, err, shim, clock


def run_raw(outcomes, *, retry=1, wait=0.0, stream=False):
    """走 _call_llm_raw（覆盖 config → payload → _llm_send → json 解析）。"""
    shim = ShimRequests(outcomes)
    clock = TimeShim()
    cfg = {"provider": "deepseek", "base_url": "https://main.example.com/v1", "api_key": "MAIN-KEY",
           "model": "main-model", "temperature": 0.3, "max_tokens": 1024}
    old = (A.requests, A.time, A.LLM_RETRY, A.LLM_RETRY_WAIT, A._LLM_FALLBACK,
           A.API_CONFIG, A.app.logger.disabled)
    try:
        A.requests = shim
        A.time = clock
        A.LLM_RETRY = retry
        A.LLM_RETRY_WAIT = wait
        A._LLM_FALLBACK = None
        A.API_CONFIG = cfg
        A.app.logger.disabled = True
        return A._call_llm_raw([{"role": "user", "content": "hi"}], stream=stream), shim, clock
    finally:
        (A.requests, A.time, A.LLM_RETRY, A.LLM_RETRY_WAIT, A._LLM_FALLBACK,
         A.API_CONFIG, A.app.logger.disabled) = old


# ==================== 用例 ====================

def test_defaults():
    print("\n[1] 默认配置：开 1 次重试，只重试瞬态故障")
    check("默认 LLM_RETRY=1", A.LLM_RETRY == 1, A.LLM_RETRY)
    check("默认退避 1.5s", abs(A.LLM_RETRY_WAIT - 1.5) < 1e-6, A.LLM_RETRY_WAIT)
    check("可重试集合为 timeout/network/busy/server",
          A._RETRYABLE_LLM_CODES == {"timeout", "network", "busy", "server"},
          A._RETRYABLE_LLM_CODES)
    check("未配置环境变量时无备用通道", A._LLM_FALLBACK is None, A._LLM_FALLBACK)


def test_retry_then_success():
    print("\n[2] 超时后重试成功：返回响应，共发 2 次")
    ok = FakeResp(200)
    resp, err, shim, clock = run_send([("raise", requests.exceptions.Timeout("read timed out")),
                                       ("resp", ok)])
    check("重试后拿到响应", resp is ok and err is None, err)
    check("共发 2 次", len(shim.calls) == 2, len(shim.calls))
    check("失败后睡了 1 次", clock.sleeps == [0.0], clock.sleeps)
    check("两次都打主通道", {c[0] for c in shim.calls} == {MAIN}, [c[0] for c in shim.calls])


def test_non_retryable():
    print("\n[3] 鉴权/额度类不重试（重试只是白等）")
    for status, code in ((401, "auth"), (402, "quota")):
        resp, err, shim, clock = run_send([("resp", FakeResp(status, text="denied"))])
        check(f"{status} 不重试", len(shim.calls) == 1, len(shim.calls))
        check(f"{status} 错误码 {code}", err and err["error_code"] == code, err)
        check(f"{status} 返回中文提示", err and "AI 服务" in err["error"], err)
    # 400 之类不可分类的 HTTP 错误
    resp, err, shim, _ = run_send([("resp", FakeResp(400, text="bad request"))])
    check("400 归为 unknown 且不重试", len(shim.calls) == 1 and err["error_code"] == "unknown", err)


def test_backoff_and_exhaust():
    print("\n[4] 持续失败：指数退避 + 用尽次数后返回错误结构")
    e = lambda: requests.exceptions.Timeout("read timed out")  # noqa: E731
    resp, err, shim, clock = run_send([("raise", e()), ("raise", e()), ("raise", e())],
                                      retry=2, wait=0.5)
    check("共发 3 次（1+2 重试）", len(shim.calls) == 3, len(shim.calls))
    check("退避序列 0.5 → 1.0", clock.sleeps == [0.5, 1.0], clock.sleeps)
    check("失败返回 (None, err)", resp is None and err is not None, resp)
    check("错误结构含 error/detail/error_code",
          set(err) >= {"error", "detail", "error_code"}, err)
    check("超时错误码 timeout", err["error_code"] == "timeout", err)

    resp, err, shim, _ = run_send([("raise", requests.exceptions.ConnectionError("refused"))] * 2,
                                  retry=1, wait=0.0)
    check("连不上归为 network", err["error_code"] == "network", err)
    check("连不上 detail 不含主机名等底层细节", err["detail"] == "connection_error", err["detail"])


def test_server_error_retryable():
    print("\n[5] 5xx / 429 属瞬态，会重试")
    for status, code in ((503, "server"), (429, "busy")):
        ok = FakeResp(200)
        resp, err, shim, _ = run_send([("resp", FakeResp(status, text="upstream down")),
                                       ("resp", ok)])
        check(f"{status} 第二次成功", resp is ok and err is None, err)
        check(f"{status} 共发 2 次", len(shim.calls) == 2, len(shim.calls))


def test_fallback_switch():
    print("\n[6] 备用通道：主通道耗尽后切换，改 model 与密钥")
    ok = FakeResp(200)
    resp, err, shim, _ = run_send(
        [("resp", FakeResp(500, text="boom")), ("resp", FakeResp(500, text="boom")),
         ("resp", ok)],
        retry=1, wait=0.0,
        fallback={"url": "https://fb.example.com/v1", "key": "FB-KEY", "model": "fb-model"})
    check("主通道重试耗尽后才切", len(shim.calls) == 3, len(shim.calls))
    check("前两次的 url 是主通道", [c[0] for c in shim.calls][:2] == [MAIN, MAIN],
          [c[0] for c in shim.calls])
    check("第 3 次打到备用通道", shim.calls[2][0] == FB, shim.calls[2][0])
    check("备用通道用备用 model", shim.calls[2][1]["model"] == "fb-model", shim.calls[2][1])
    check("备用通道用备用密钥", shim.calls[2][2]["Authorization"] == "Bearer FB-KEY",
          shim.calls[2][2])
    check("主通道入参未被改写（不共享字典）",
          shim.calls[0][1]["model"] == "main-model"
          and shim.calls[0][2]["Authorization"] == "Bearer MAIN-KEY",
          (shim.calls[0][1], shim.calls[0][2]))
    check("切换后返回成功响应", resp is ok and err is None, err)


def test_fallback_absent_and_all_failed():
    print("\n[7] 无备用通道只打主通道；备用也失败则返回最后一次的错误")
    resp, err, shim, _ = run_send([("resp", FakeResp(500, text="a")), ("resp", FakeResp(500, text="b"))],
                                  retry=1, wait=0.0)
    check("无备用时只发 2 次", len(shim.calls) == 2, len(shim.calls))
    check("无备用时全打主通道", {c[0] for c in shim.calls} == {MAIN}, [c[0] for c in shim.calls])
    check("无备用返回 server 错误", err["error_code"] == "server", err)

    resp, err, shim, clock = run_send(
        [("resp", FakeResp(500, text="a")), ("resp", FakeResp(500, text="b")),
         ("resp", FakeResp(503, text="c")), ("resp", FakeResp(503, text="d"))],
        retry=1, wait=0.0,
        fallback={"url": "https://fb.example.com/v1", "key": "FB-KEY", "model": "fb-model"})
    check("两通道各 2 次共 4 次", len(shim.calls) == 4, len(shim.calls))
    check("备用通道失败不再重新退避", clock.sleeps == [0.0, 0.0], clock.sleeps)
    check("两通道都挂时仍返回错误字典", resp is None and err["error_code"] == "server", err)

    # 备用只配 url：model 与密钥沿用主通道
    ok = FakeResp(200)
    resp, err, shim, _ = run_send([("resp", FakeResp(500, text="a")), ("resp", ok)],
                                  retry=0, wait=0.0, fallback={"url": "https://fb.example.com/v1"})
    check("只配 url 时沿用主 model", shim.calls[1][1]["model"] == "main-model", shim.calls[1][1])
    check("只配 url 时沿用主密钥", shim.calls[1][2]["Authorization"] == "Bearer MAIN-KEY",
          shim.calls[1][2])


def test_error_dict_shape():
    print("\n[8] 错误字典与 call_llm 既有契约同构（前端不需要认识新字段）")
    resp, err, shim, _ = run_send([("resp", FakeResp(500, text="x" * 400))], retry=0, wait=0.0)
    check("detail 取响应正文并截断", err["detail"] == "x" * 200, len(err["detail"]))
    check("error 为可读中文", err["error"].endswith("。") and "AI 服务" in err["error"], err["error"])
    r = requests.exceptions.HTTPError("boom")
    d = A._llm_error_dict(r)
    check("无 response 的 HTTPError 不崩", d["error_code"] == "unknown", d)


def test_close_on_failure():
    print("\n[9] 非 2xx 的响应体被显式关闭")
    dead = FakeResp(500, text="boom")
    resp, err, shim, _ = run_send([("resp", dead)], retry=0, wait=0.0)
    check("失败响应 close 调用过", dead.closed >= 1, dead.closed)
    check("未返回该响应", resp is None, resp)


def test_call_llm_raw_paths():
    print("\n[10] _call_llm_raw：非流式解析 JSON / 坏 JSON 降级 / 流式仍只回传连接参数")
    ok = FakeResp(200, body={"choices": [{"message": {"content": "答复"}}]})
    res, shim, _ = run_raw([("resp", ok)])
    check("非流式返回模型 JSON", res.get("choices", [{}])[0]["message"]["content"] == "答复", res)

    res, shim, _ = run_raw([("resp", FakeResp(200, body=ValueError("Expecting value: line 1")))],
                           retry=0)
    check("200 但正文非 JSON 时按错误契约返回",
          res.get("error_code") == "unknown" and "无法解析" in res.get("error", ""), res)
    check("坏 JSON 不冒泡异常", "error" in res, res)

    res, shim, _ = run_raw([("resp", FakeResp(500, text="down")),
                            ("resp", FakeResp(500, text="down"))], retry=1, wait=0.0)
    check("非流式失败返回错误字典", "error" in res and res.get("error_code") == "server", res)

    res, shim, _ = run_raw([], stream=True)
    check("流式不发请求，只回传 url/headers/payload",
          len(shim.calls) == 0 and res.get("url") == MAIN and "payload" in res, res)


def test_fallback_config_from_env():
    print("\n[11] 备用通道只由环境变量配置（仓库内不含明文密钥）")
    check("未配置 base_url 时无备用通道", A._build_llm_fallback({}) is None)
    check("空白 base_url 视为未配置",
          A._build_llm_fallback({"SMARTTOURISM_LLM_FALLBACK_BASE_URL": "   "}) is None)
    fb = A._build_llm_fallback({"SMARTTOURISM_LLM_FALLBACK_BASE_URL": "https://fb.example.com/v1/",
                                "SMARTTOURISM_LLM_FALLBACK_MODEL": " other-model ",
                                "SMARTTOURISM_LLM_FALLBACK_KEY": " FB-KEY "})
    check("自动补 /chat/completions", fb["base_url"] == "https://fb.example.com/v1/chat/completions",
          fb["base_url"])
    check("model 去空白", fb["model"] == "other-model", fb["model"])
    check("key 去空白", fb["api_key"] == "FB-KEY", fb["api_key"])
    check("可重试路径的备用 url 与主 url 不同", fb["base_url"] != MAIN, fb["base_url"])


def main():
    print("=" * 60)
    print("LLM 重试 / 备用通道测试")
    print("=" * 60)
    test_defaults()
    test_retry_then_success()
    test_non_retryable()
    test_backoff_and_exhaust()
    test_server_error_retryable()
    test_fallback_switch()
    test_fallback_absent_and_all_failed()
    test_error_dict_shape()
    test_close_on_failure()
    test_call_llm_raw_paths()
    test_fallback_config_from_env()
    total = _PASS + _FAIL
    print("\n" + "=" * 60)
    print(f"结果：{_PASS} passed, {_FAIL} failed（共 {total}）")
    print("=" * 60)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
