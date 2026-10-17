# -*- coding: utf-8 -*-
"""
对话热路径降级测试：DB / 日志写失败不能吞掉答复
=================================================
真实跑 /api/chat（非流式 + 流式），把会话库的相关 DAO 全部打成抛异常：
  1. 答复照常返回（200 + reply / SSE content + [DONE]），不 500、不空内容；
  2. 失败不是静默的：/api/admin/ops 的「最近错误」里能看到 db_* 明细；
  3. _db_call 成功透传、失败返回 default、无请求上下文时也能用；
  4. _load_conversation_history 在 DB 读失败时退化为空历史（而不是连缓存一起丢）；
  5. 启动期降级（数据库不可用 / 文件日志不可用）在 validate_config 里可见；
  6. 事后反幻觉校验自己坏掉时同样只降级、不吞答复，且不再静默 pass。

用临时库（SMARTTOURISM_DB），不碰 data/smarttourism.db；不调用真实大模型与第三方接口。

运行：  python server/test_hotpath_degrade.py
"""

from __future__ import annotations

import json
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
_TMPDB = os.path.join(tempfile.mkdtemp(prefix="st_hotpath_test_"), "test.db")
os.environ["SMARTTOURISM_DB"] = _TMPDB
os.environ.setdefault("SMARTTOURISM_MEMORY", "0")
os.environ.setdefault("SMARTTOURISM_LLM_CACHE", "0")

import app as A  # noqa: E402
import database as db  # noqa: E402

_PASS = 0
_FAIL = 0
REPLY = "好的，杭州 3 天建议如下。"
_CHUNKS = ("好的，", "杭州 3 天", "建议如下。")


class FakeStream:
    """流式响应替身：只实现 generate() 用到的 iter_lines / encoding。"""

    encoding = "utf-8"

    def iter_lines(self, decode_unicode=True):
        for c in _CHUNKS:
            yield "data: " + json.dumps({"choices": [{"delta": {"content": c}}]},
                                       ensure_ascii=False)
        yield "data: [DONE]"

    def close(self):
        pass


def check(name, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}{(' -> ' + str(extra))[:300] if extra else ''}")


# ==================== 打桩：假模型 + 不打外网 + 坏 DAO ====================

_BROKEN = ("get_session", "create_session", "update_session_active",
           "get_session_messages", "save_message")


def _boom(name):
    def _f(*a, **kw):
        raise RuntimeError(f"{name} 炸了（模拟磁盘满/库被独占）")
    return _f


class BreakDB:
    """把对话热路径用到的 DAO 全部换成抛异常，退出时还原。"""

    def __init__(self, names=_BROKEN):
        self.names = names
        self.saved = {}

    def __enter__(self):
        for n in self.names:
            self.saved[n] = getattr(db, n)
            setattr(db, n, _boom(n))
        return self

    def __exit__(self, *exc):
        for n, fn in self.saved.items():
            setattr(db, n, fn)
        return False


class Stubbed:
    """替换 API_CONFIG / call_llm / 插件与 RAG 取数，让 /api/chat 只走本地逻辑。"""

    def __enter__(self):
        cfg = dict(A.API_CONFIG)
        cfg["api_key"] = "TEST-ONLY-FAKE-KEY"
        cfg["base_url"] = "http://127.0.0.1:1/v1"
        self.saved = {
            "API_CONFIG": A.API_CONFIG,
            "call_llm": A.call_llm,
            "_llm_send": A._llm_send,
            "get_plugin_data": A.get_plugin_data,
            "get_rag_context": A.get_rag_context,
            "retrieve_knowledge": A.retrieve_knowledge,
            "_extract_locations_from_text": A._extract_locations_from_text,
            "logger_disabled": A.app.logger.disabled,
        }
        A.API_CONFIG = cfg

        def _fake_call_llm(messages, stream=False, skill_name="", tools=None, tool_choice=None):
            # 流式路径先拿「连接参数」再由 _llm_send 建连，两处都要给替身
            if stream:
                return {"url": "http://127.0.0.1:1/v1/chat/completions",
                        "headers": {"Authorization": "Bearer TEST-ONLY-FAKE-KEY"},
                        "payload": {"model": "test-model", "messages": messages, "stream": True}}
            return {"choices": [{"message": {"content": REPLY}}]}

        A.call_llm = _fake_call_llm
        A._llm_send = lambda url, headers, payload, stream=False, timeout=120: (FakeStream(), None)
        A.get_plugin_data = lambda *a, **kw: ""
        A.get_rag_context = lambda *a, **kw: ""
        A.retrieve_knowledge = lambda *a, **kw: ("", [])
        A._extract_locations_from_text = lambda *a, **kw: []
        A.app.logger.disabled = True   # 不把测试噪声写进 data/logs/app.log
        return self

    def __exit__(self, *exc):
        for k in ("API_CONFIG", "call_llm", "_llm_send", "get_plugin_data", "get_rag_context",
                  "retrieve_knowledge", "_extract_locations_from_text"):
            setattr(A, k, self.saved[k])
        A.app.logger.disabled = self.saved["logger_disabled"]
        return False


def ops_db_errors():
    snap = A.OPS.snapshot()
    return [e for e in snap["recent_errors"] if "db_" in e.get("message", "")]


def post_chat(conv, stream):
    with Stubbed():
        resp = A.app.test_client().post("/api/chat", json={
            "message": "杭州3天怎么玩", "skill": "", "conversation_id": conv, "stream": stream})
        return resp.status_code, resp.get_data(as_text=True)


def parse_sse(raw):
    events, err = [], None
    for line in raw.split("\n"):
        if not line.startswith("data: "):
            continue
        s = line[6:].strip()
        if s == "[DONE]":
            events.append({"done": True})
            continue
        try:
            d = json.loads(s)
        except ValueError:
            continue
        events.append(d)
        if "error" in d:
            err = d
    return events, err


# ==================== 用例 ====================

def test_db_call_unit():
    print("\n[1] _db_call：成功透传 / 失败给 default / 无请求上下文也能用")
    check("成功时原样返回", A._db_call("ok", lambda x: x * 2, 21) == 42)
    old = A.app.logger.disabled
    A.app.logger.disabled = True
    try:
        def _bad():
            raise RuntimeError("boom")

        check("失败返回 default", A._db_call("bad", _bad, default=[]) == [])
        check("失败不抛异常", True)
        before = len(ops_db_errors())
        A._db_call("unit_probe", _bad)
        check("失败写进运维错误明细", len(ops_db_errors()) == before + 1, ops_db_errors()[-1:])
        check("错误明细带 exc_type", ops_db_errors()[0].get("exc_type") == "RuntimeError",
              ops_db_errors()[0])
        path, rid = A._db_ctx()
        check("无请求上下文时 path=startup", path == "startup", path)
    finally:
        A.app.logger.disabled = old


def test_load_history_degrade():
    print("\n[2] DB 读失败时 _load_conversation_history 退化为空历史，不连缓存一起丢")
    A._conv_cache_drop("hot-cached")
    A._conv_cache_put("hot-cached", [{"role": "user", "content": "旧轮次"}])
    with BreakDB(("get_session_messages",)):
        got = A._load_conversation_history("hot-cached")
        check("已有缓存仍可用", got == [{"role": "user", "content": "旧轮次"}], got)
        A._conv_cache_drop("hot-fresh")
        old = A.app.logger.disabled
        A.app.logger.disabled = True
        try:
            got2 = A._load_conversation_history("hot-fresh")
        finally:
            A.app.logger.disabled = old
        check("冷会话退化为空列表而不是抛异常", got2 == [], got2)
    A._conv_cache_drop("hot-cached")
    A._conv_cache_drop("hot-fresh")


def test_nonstream_chat_survives_db_failure():
    print("\n[3] 非流式 /api/chat：DAO 全炸也要把答复交给用户")
    with BreakDB():
        code, raw = post_chat("hot-nonstream", stream=False)
    check("HTTP 200（不是 500）", code == 200, code)
    try:
        body = json.loads(raw)
    except ValueError:
        body = {}
    check("success=true", body.get("success") is True, raw[:200])
    check("reply 是模型答复", REPLY in (body.get("reply") or ""), body.get("reply"))
    check("conversation_id 回传", body.get("conversation_id") == "hot-nonstream", body)


def test_stream_chat_survives_db_failure():
    print("\n[4] 流式 /api/chat：DAO 全炸也要流完并补 [DONE]")
    errs_before = len(ops_db_errors())
    with BreakDB():
        code, raw = post_chat("hot-stream", stream=True)
    check("HTTP 200", code == 200, code)
    events, err = parse_sse(raw)
    text = "".join(e.get("content", "") for e in events if "content" in e)
    check("没有 error 事件", err is None, err)
    check("正文完整流出", REPLY in text, text[:120])
    check("补了 [DONE]", any(e.get("done") for e in events))
    check("下发了 context 占用事件", any(e.get("type") == "context" for e in events))
    check("落库失败被记进运维错误明细（不再静默）",
          len(ops_db_errors()) > errs_before, ops_db_errors()[-2:])


def test_degraded_startup_visible():
    print("\n[5] 启动期降级在 /api/health 的配置校验里看得见")
    res = A.validate_config()
    names = {c["name"]: c for c in res["checks"]}
    check("健康时 database=ok", names.get("database", {}).get("level") == "ok", names.get("database"))
    check("健康时 log_file=ok", names.get("log_file", {}).get("level") == "ok", names.get("log_file"))

    old_db, old_log = A.DB_READY, A.LOG_FILE_OK
    try:
        A.DB_READY, A.DB_ERROR = False, "disk I/O error"
        A.LOG_FILE_OK = False
        res = A.validate_config()
        names = {c["name"]: c for c in res["checks"]}
        check("DB 挂掉 → warn 而非 error（对话仍可用）",
              names["database"]["level"] == "warn" and names["database"]["ok"] is False,
              names["database"])
        check("DB 降级原因写进 message", "disk I/O error" in names["database"]["message"],
              names["database"]["message"])
        check("日志降级 → warn", names["log_file"]["level"] == "warn", names["log_file"])
        check("降级不计入 missing_required（不谎报核心功能不可用）",
              "database" not in res["missing_required"], res["missing_required"])
        check("降级出现在 warnings 列表", any("不再持久化" in w for w in res["warnings"]),
              res["warnings"])
    finally:
        A.DB_READY, A.DB_ERROR, A.LOG_FILE_OK = old_db, "", old_log


def test_log_dir_unwritable_boot():
    print("\n[6] 日志目录不可用时 _add_file_handler 降级为 None（不阻断启动）")
    old = A.LOG_FILE_OK
    try:
        A.LOG_FILE_OK = False
        check("LOG_FILE_OK=False 时不建 handler", A._add_file_handler("probe.log") is None)
        A.LOG_FILE_OK = True
        A.LOG_DIR = os.path.join(_TMPDB, "no-such-dir", "logs")
        h = A._add_file_handler("probe.log")
        check("目录不可写时返回 None 而不是抛异常", h is None)
        check("失败后 LOG_FILE_OK 置 False", A.LOG_FILE_OK is False, A.LOG_FILE_OK)
    finally:
        A.LOG_FILE_OK, A.LOG_DIR = old, os.path.join(_ROOT, "data", "logs")


def ops_validation_errors():
    snap = A.OPS.snapshot()
    return [e for e in snap["recent_errors"] if "validation" in e.get("message", "")]


class BrokenValidation:
    """把事后反幻觉校验打成抛异常：它只是保险丝，不该把已生成、已落库的答复打成 500。"""

    def __enter__(self):
        self.saved = A.validate_response

        def _boom(*a, **kw):
            raise RuntimeError("校验环节炸了（模拟）")

        A.validate_response = _boom
        return self

    def __exit__(self, *exc):
        A.validate_response = self.saved
        return False


def test_validation_failure_does_not_kill_answer():
    print("\n[7] 事后校验本身坏掉：答复照旧交付，但降级留痕（不再 except: pass）")
    before = len(ops_validation_errors())
    with BrokenValidation():
        code, raw = post_chat("val-nonstream", stream=False)
        body = json.loads(raw)
        check("非流式仍然 200（不是 500）", code == 200, code)
        check("非流式答复完整", body.get("reply") == REPLY, body.get("reply"))
        check("非流式 validation 字段为 null（不是崩溃）", body.get("validation") is None,
              body.get("validation"))

        code, raw = post_chat("val-stream", stream=True)
        events, err = parse_sse(raw)
        text = "".join(e.get("content", "") for e in events if "content" in e)
        check("流式仍然 200", code == 200, code)
        check("流式答复完整", text == REPLY, text)
        check("流式没有 error 事件", err is None, err)
        check("流式收到 [DONE]", any(e.get("done") for e in events))
    check("校验失败进了运维错误明细", len(ops_validation_errors()) == before + 2,
          ops_validation_errors()[-1:])
    check("错误明细带 exc_type",
          ops_validation_errors()[-1].get("exc_type") == "RuntimeError", ops_validation_errors()[-1])

    # _run_validation 直接调用：异常吞掉返回 None，部分降级仍返回报告
    old_disabled = A.app.logger.disabled
    A.app.logger.disabled = True
    try:
        with BrokenValidation():
            check("_run_validation 异常时返回 None",
                  A._run_validation("答复", "", "val-direct") is None)

        from anti_hallucination import ValidationReport
        saved = A.validate_response
        try:
            A.validate_response = lambda *a, **kw: ValidationReport(degraded=["consistency"])
            rep = A._run_validation("答复", "", "val-direct")
            check("部分降级仍返回报告", rep is not None and rep.degraded == ["consistency"], rep)
        finally:
            A.validate_response = saved
    finally:
        A.app.logger.disabled = old_disabled
    A._conv_cache_drop("val-nonstream")
    A._conv_cache_drop("val-stream")


def main():
    print("=" * 60)
    print("对话热路径 DB / 日志降级测试")
    print("=" * 60)
    test_db_call_unit()
    test_load_history_degrade()
    test_nonstream_chat_survives_db_failure()
    test_stream_chat_survives_db_failure()
    test_degraded_startup_visible()
    test_log_dir_unwritable_boot()
    test_validation_failure_does_not_kill_answer()
    total = _PASS + _FAIL
    print("\n" + "=" * 60)
    print(f"结果：{_PASS} passed, {_FAIL} failed（共 {total}）")
    print("=" * 60)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
