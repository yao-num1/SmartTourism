# -*- coding: utf-8 -*-
"""
流式生成中断测试：「停止生成」要真停下、真释放、真落库
=====================================================
覆盖：
  1. 取消注册表语义：登记/命中/未命中/注销/TTL 淘汰，计数不越界；
  2. POST /api/chat/stop：命中活跃流与「已经结束了」都回 200（不把没得停报成失败）；
  3. 生成中途停止：SSE 依次给出 stream_id → 正文 → stopped → [DONE]；
     上游流被 close()（否则 waitress 线程被没人看的回答占住）；
     已生成的那一半带 truncated=1 落库，未生成的部分不出现；
  4. 采集阶段就停止：一个字都没出来 → 不落半截问答，也不谎报「信息不足」；
  5. 不停止的回归：正常流式不受影响，truncated=0，没有 stopped 事件。

用临时库（SMARTTOURISM_DB），不调用真实大模型与第三方接口。

运行：  python server/test_stream_stop.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

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
_TMPDB = os.path.join(tempfile.mkdtemp(prefix="st_stream_stop_"), "test.db")
os.environ["SMARTTOURISM_DB"] = _TMPDB
os.environ.setdefault("SMARTTOURISM_MEMORY", "0")
os.environ.setdefault("SMARTTOURISM_LLM_CACHE", "0")

import app as A  # noqa: E402
import database as db  # noqa: E402

_PASS = 0
_FAIL = 0
CHUNKS = ("第一段内容，", "第二段内容，", "第三段不该出现")


class Stream:
    """上游流式响应替身：只实现 generate() 用到的 encoding / iter_lines / close。"""

    encoding = "utf-8"

    def __init__(self, chunks=CHUNKS, cancel_after=None, cancel_event=None):
        self.chunks = chunks
        self.cancel_after = cancel_after      # 读出第几条 SSE 行后置位取消标志
        self.cancel_event = cancel_event
        self.closed = False
        self.lines_read = 0

    def iter_lines(self, decode_unicode=True):
        for c in self.chunks:
            yield "data: " + json.dumps({"choices": [{"delta": {"content": c}}]},
                                        ensure_ascii=False)
            self.lines_read += 1
            if self.cancel_event is not None and self.lines_read == self.cancel_after:
                self.cancel_event.set()
        yield "data: [DONE]"

    def close(self):
        self.closed = True


class Stubbed:
    """把 /api/chat 用到的外部依赖全换成替身；cancel_at 指定「读到第几条就停止」。"""

    def __init__(self, cancel_at=None, pre_stop=False):
        self.cancel_at = cancel_at
        self.pre_stop = pre_stop
        self.stream = None
        self.captured_event = None

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
            "_stream_register": A._stream_register,
            "logger_disabled": A.app.logger.disabled,
        }
        A.API_CONFIG = cfg

        def _fake_call_llm(messages, stream=False, skill_name="", tools=None, tool_choice=None):
            if stream:
                return {"url": "http://127.0.0.1:1/v1/chat/completions",
                        "headers": {"Authorization": "Bearer TEST-ONLY-FAKE-KEY"},
                        "payload": {"model": "test-model", "messages": messages, "stream": True}}
            # 非流式用于「预生成思考」；返回空内容，让断言只看到真正的答复
            return {"choices": [{"message": {"content": ""}}]}

        def _register(sid):
            ev = self.saved["_stream_register"](sid)
            self.captured_event = ev
            if self.pre_stop:
                ev.set()   # 模拟用户在数据采集阶段就点了停止
            return ev

        def _send(url, headers, payload, stream=False, timeout=120):
            # 取消标志用「这一路真正登记到的 Event」，置位它等于前端点了停止
            self.stream = Stream(CHUNKS, cancel_after=self.cancel_at,
                                 cancel_event=(self.captured_event if self.cancel_at else None))
            return self.stream, None

        A.call_llm = _fake_call_llm
        A._llm_send = _send
        A._stream_register = _register
        A.get_plugin_data = lambda *a, **kw: ""
        A.get_rag_context = lambda *a, **kw: ""
        A.retrieve_knowledge = lambda *a, **kw: ("", [])
        A._extract_locations_from_text = lambda *a, **kw: []
        A.app.logger.disabled = True   # 不把测试噪声写进 data/logs/app.log
        return self

    def __exit__(self, *exc):
        for k in ("API_CONFIG", "call_llm", "_llm_send", "get_plugin_data", "get_rag_context",
                  "retrieve_knowledge", "_extract_locations_from_text", "_stream_register"):
            setattr(A, k, self.saved[k])
        A.app.logger.disabled = self.saved["logger_disabled"]
        return False


def check(name, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}{(' -> ' + str(extra))[:300] if extra else ''}")


def parse_sse(raw):
    """把 SSE 原文拆成事件列表，并保留顺序（停止流程的先后是有意义的）。"""
    out = []
    for line in raw.split("\n"):
        if not line.startswith("data: "):
            continue
        s = line[6:].strip()
        if s == "[DONE]":
            out.append({"_type": "[DONE]"})
            continue
        try:
            d = json.loads(s)
        except ValueError:
            continue
        if "content" in d:
            d["_type"] = "content"
        elif "done" in d:
            d["_type"] = "done"
        else:
            d["_type"] = d.get("type", "?")
        out.append(d)
    return out


def types(events):
    return [e.get("_type") for e in events]


def chat(conv, stub, stream=True):
    with stub:
        resp = A.app.test_client().post("/api/chat", json={
            "message": "杭州3天怎么玩", "skill": "", "conversation_id": conv, "stream": stream})
        return resp.status_code, resp.get_data(as_text=True), stub


def agent_rows(conv):
    return [m for m in db.get_session_messages(conv) if m["role"] == "agent"]


def user_rows(conv):
    return [m for m in db.get_session_messages(conv) if m["role"] == "user"]


# ==================== 用例 ====================

def test_registry_unit():
    print("\n[1] 取消注册表：登记 / 命中 / 未命中 / 注销 / TTL 淘汰")
    check("初始没有活跃流", A.stream_stats()["active"] == 0, A.stream_stats())

    ev = A._stream_register("unit-a")
    check("登记后计为活跃", A.stream_stats()["active"] == 1, A.stream_stats())
    check("新登记的流未被取消", A._stream_cancelled(ev) is False)
    check("None 事件视为未取消", A._stream_cancelled(None) is False)
    check("停止不存在的流返回 False", A._stream_stop("unit-never") is False)
    check("停止已登记的流返回 True", A._stream_stop("unit-a") is True)
    check("置位后取消生效", A._stream_cancelled(ev) is True)
    A._stream_unregister("unit-a")
    check("注销后计数回落", A.stream_stats()["active"] == 0, A.stream_stats())
    check("注销后再停止返回 False", A._stream_stop("unit-a") is False)
    check("重复注销不报错", (A._stream_unregister("unit-a") or True))

    # TTL 淘汰：残留条目（异常没走到 finally）不能永远占着内存
    A._stream_events["unit-old"] = (A.threading.Event(), time.time() - A.STREAM_TTL_S - 10)
    A._stream_register("unit-b")
    check("超时的残留流被清掉", "unit-old" not in A._stream_events, list(A._stream_events))
    A._stream_unregister("unit-b")
    check("注册表清空", not A._stream_events, A._stream_events)


def test_stop_endpoint():
    print("\n[2] POST /api/chat/stop：没得停也回 200，不报 404")
    c = A.app.test_client()
    r = c.post("/api/chat/stop", json={"request_id": "no-such-stream"})
    check("未知流仍是 200", r.status_code == 200, r.status_code)
    body = r.get_json()
    check("found=False 如实说明", body["success"] is True and body["found"] is False, body)

    ev = A._stream_register("api-stop")
    r2 = c.post("/api/chat/stop", json={"request_id": "api-stop"})
    check("命中活跃流 found=True", r2.get_json()["found"] is True, r2.get_json())
    check("事件确被置位", A._stream_cancelled(ev) is True)
    A._stream_unregister("api-stop")

    r3 = c.post("/api/chat/stop", json={})
    check("缺 request_id 不炸", r3.status_code == 200 and r3.get_json()["found"] is False,
          r3.get_json())


def test_stop_mid_generation():
    print("\n[3] 生成中途停止：真停、真关流、半截内容如实落库")
    conv = "conv-stop-mid"
    before = A.stream_stats()["cancelled"]
    code, raw, stub = chat(conv, Stubbed(cancel_at=2))
    events = parse_sse(raw)
    seq = types(events)

    check("状态码 200", code == 200, code)
    check("第一条事件就是 stream_id", seq[0] == "stream_id", seq[:3])
    sid = events[0]["data"]["request_id"]
    check("stream_id 非空可停止", bool(sid), sid)
    check("有 stopped 事件", "stopped" in seq, seq)
    check("stopped 在 [DONE] 之前", seq.index("stopped") < seq.index("[DONE]"), seq)
    check("[DONE] 照发", "[DONE]" in seq, seq)

    body = "".join(e["content"] for e in events if e.get("_type") == "content")
    check("已生成的前两段落库前已下发", "第一段内容" in body and "第二段内容" in body, body)
    check("未生成的第三段没有出现", "第三段不该出现" not in body, body)
    stopped = [e for e in events if e.get("_type") == "stopped"][0]
    check("stopped 说明内容不完整", "不完整" in stopped["data"]["message"], stopped)
    check("stopped 的字数与已下发内容一致",
          0 < stopped["data"]["chars"] <= len(body) + 4, (stopped, body))
    done_ev = [e for e in events if e.get("_type") == "done"][0]
    check("done 事件标出 cancelled", done_ev.get("cancelled") is True, done_ev)

    check("上游流被 close（线程与连接释放）", stub.stream.closed is True)
    check("读取行数停在取消处", stub.stream.lines_read == 2, stub.stream.lines_read)

    a_rows = agent_rows(conv)
    check("半截答复有落库", len(a_rows) == 1, a_rows)
    if a_rows:
        check("落库内容就是已下发的那一半", a_rows[0]["content"] == body,
              (a_rows[0]["content"], body))
        check("truncated=1 如实标记", int(a_rows[0]["truncated"]) == 1, a_rows[0])
    check("用户提问照常落库", len(user_rows(conv)) == 1, user_rows(conv))
    check("中断计数 +1", A.stream_stats()["cancelled"] == before + 1, A.stream_stats())
    check("结束后注册表不残留", not A._stream_events, A._stream_events)
    check("中断轮不产生地点卡片", "locations" not in seq, seq)


def test_stop_during_collecting():
    print("\n[4] 采集阶段就停止：没生成一个字就不落半截问答")
    conv = "conv-stop-collect"
    code, raw, stub = chat(conv, Stubbed(pre_stop=True))
    events = parse_sse(raw)
    seq = types(events)

    check("状态码 200", code == 200, code)
    check("仍然先给 stream_id", seq[0] == "stream_id", seq[:2])
    check("给出 stopped 事件", "stopped" in seq, seq)
    stopped = [e for e in events if e.get("_type") == "stopped"][0]
    check("chars=0 说明什么都没生成", stopped["data"]["chars"] == 0, stopped)
    check("标出停在采集阶段", stopped["data"].get("stage") == "collecting", stopped)
    check("没有正文事件", "content" not in seq, seq)
    check("[DONE] 照发（前端好收尾）", "[DONE]" in seq, seq)
    check("没有连接上游流", stub.stream is None)
    check("该会话没有任何消息落库", db.get_session_messages(conv) == [],
          db.get_session_messages(conv))
    check("缓存里是空历史而非半截问答", A.conversations.get(conv) == [],
          A.conversations.get(conv))


def test_normal_stream_regression():
    print("\n[5] 不停止的回归：正常流式不受影响")
    conv = "conv-normal"
    before = A.stream_stats()["cancelled"]
    code, raw, stub = chat(conv, Stubbed())
    events = parse_sse(raw)
    seq = types(events)

    check("状态码 200", code == 200, code)
    check("没有 stopped 事件", "stopped" not in seq, seq)
    check("没有 error 事件", "error" not in seq, seq)
    body = "".join(e["content"] for e in events if e.get("_type") == "content")
    check("完整答复全部下发", all(c in body for c in CHUNKS), body)
    check("[DONE] 存在", "[DONE]" in seq, seq)
    check("上游流照常关闭", stub.stream.closed is True)
    a_rows = agent_rows(conv)
    check("答复完整落库", len(a_rows) == 1, a_rows)
    if a_rows:
        check("truncated=0（完整答复）", int(a_rows[0]["truncated"]) == 0, a_rows[0])
    check("中断计数没被误加", A.stream_stats()["cancelled"] == before, A.stream_stats())
    check("注册表已清", not A._stream_events, A._stream_events)


def test_truncated_column_and_ops():
    print("\n[6] messages.truncated 列 + 运维面板可见")
    cols = [r[1] for r in db.get_connection().execute("PRAGMA table_info(messages)").fetchall()]
    check("messages 表有 truncated 列", "truncated" in cols, cols)

    # 幂等：老库重复初始化不该因为列已存在而炸
    try:
        db.init_db()
        check("init_db 重复运行不报错", True)
    except Exception as e:  # noqa: BLE001
        check("init_db 重复运行不报错", False, e)

    db.create_session(user_id="default", session_id="conv-col")
    m = db.save_message("conv-col", "agent", "半截答复", message_id="mid-col", truncated=True)
    check("save_message 接受 message_id", m["message_id"] == "mid-col", m)
    check("save_message 写入 truncated", int(m["truncated"]) == 1, m)
    back = db.get_session_messages("conv-col")
    check("读回带 truncated", int(back[0]["truncated"]) == 1, back[0])

    r = A.app.test_client().get("/api/admin/ops")
    check("本机访问 /api/admin/ops 返回 chat_streams",
          "chat_streams" in (r.get_json() or {}), list((r.get_json() or {}).keys())[:12])
    cs = (r.get_json() or {}).get("chat_streams", {})
    check("chat_streams 有三个计数",
          {"cancelled", "client_disconnected", "active"} <= set(cs), cs)
    html = A.render_admin_html()
    check("面板有流式生成行", "chat_streams" in html and "客户端断连" in html)


def test_message_ordering_by_rowid():
    print("\n[7] 消息按写入顺序返回（timestamp 只到秒，同秒会乱序）")
    conv = "conv-order"
    db.create_session(user_id="default", session_id=conv)
    for i, (role, content) in enumerate([("user", "问1"), ("agent", "答1"),
                                         ("user", "问2"), ("agent", "答2")]):
        db.save_message(conv, role, content, message_id=f"m{i}")
    rows = db.get_session_messages(conv)
    check("顺序就是写入顺序", [r["content"] for r in rows] == ["问1", "答1", "问2", "答2"],
          [r["content"] for r in rows])
    check("get_last_user_message 取到最后一条提问",
          db.get_last_user_message(conv) == "问2", db.get_last_user_message(conv))

    deleted = db.delete_last_turn(conv)
    check("delete_last_turn 删掉一轮（2 条）", deleted == 2, deleted)
    check("删的是最后一问一答",
          [r["content"] for r in db.get_session_messages(conv)] == ["问1", "答1"],
          [r["content"] for r in db.get_session_messages(conv)])
    check("空会话删不出错", db.delete_last_turn("conv-empty-xxx") == 0)
    check("无提问时 last_user 返回 None", db.get_last_user_message("conv-empty-xxx") is None)


def main():
    print("=" * 60)
    print("流式生成中断（停止生成 / 断连释放）测试")
    print("=" * 60)
    test_registry_unit()
    test_stop_endpoint()
    test_stop_mid_generation()
    test_stop_during_collecting()
    test_normal_stream_regression()
    test_truncated_column_and_ops()
    test_message_ordering_by_rowid()
    total = _PASS + _FAIL
    print("\n" + "=" * 60)
    print(f"结果：{_PASS} passed, {_FAIL} failed（共 {total}）")
    print("=" * 60)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
