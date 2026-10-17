# -*- coding: utf-8 -*-
"""
知识库出处可追溯测试：检索真发生在主路径，出处要真发得出来
==========================================================
覆盖：
  1. retrieve_knowledge 的降级口径——没输入 / 检索器不可用 / 无结果 / 检索抛异常，
     四种情况都必须同时返回「空上下文 + 空出处」，不能凭空造一个来源；
  2. 正常检索：出处条数、字段（name/city/type/source/score/snippet）、空白折叠；
     get_rag_context 作为薄包装与之一致；
  3. /api/chat 流式：preparation 与 sources 事件按序下发，非流式 JSON 带 sources；
  4. sources_json 落库与读回（历史回看还能追溯依据）；
  5. 只配置了高德 Key、知识库无命中时：preparation 如实写 rag_sources=0 且不发 sources。

不调用真实大模型，不联网下载模型（假检索器）。

运行：  python server/test_rag_sources.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types

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

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="st_rag_sources_"), "test.db")
os.environ["SMARTTOURISM_DB"] = _TMPDB
os.environ.setdefault("SMARTTOURISM_MEMORY", "0")
os.environ.setdefault("SMARTTOURISM_LLM_CACHE", "0")

import app as A  # noqa: E402
import database as db  # noqa: E402
import anti_hallucination as AH  # noqa: E402

_PASS = 0
_FAIL = 0
REPLY = "杭州西湖适合傍晚去。"
SOURCES = [
    {"index": 1, "name": "西湖景点", "city": "杭州", "type": "poi",
     "source": "本地知识库", "score": 0.81, "snippet": "西湖  三面云山"},
]


class Doc:
    def __init__(self, text, score, metadata):
        self.text, self.score, self.metadata = text, score, metadata


class FakeRetriever:
    """假混合检索器：结果可控，避免测试碰真模型与联网下载。"""

    embedder = types.SimpleNamespace(name="fake")

    def __init__(self, docs=None, boom=False):
        self._docs, self._boom = (docs or []), boom
        self.calls = []

    def retrieve(self, query, top_k=3, **kw):
        self.calls.append(query)
        if self._boom:
            raise RuntimeError("检索器坏了")
        return self._docs[:top_k]


class Isolated:
    """把 anti_hallucination 的日志接走，测试噪声不进 data/logs。"""

    def __enter__(self):
        self.saved = list(AH.logger.handlers)
        AH.logger.handlers = [AH.logging.NullHandler()]
        AH.logger.propagate = False
        return self

    def __exit__(self, *exc):
        AH.logger.handlers = self.saved
        AH.logger.propagate = True
        return False


class PatchRAG:
    """把懒加载的检索器换成假对象（含「构建失败」和「检索抛异常」两种坏法）。"""

    def __init__(self, retriever="ok", docs=None):
        self.retriever, self.docs = retriever, docs

    def __enter__(self):
        self.saved = (AH._HYBRID_RETRIEVER, AH._HYBRID_BUILD_FAILED)
        AH._HYBRID_BUILD_FAILED = False
        if self.retriever == "unavailable":
            AH._HYBRID_RETRIEVER = None
            AH._HYBRID_BUILD_FAILED = True
        elif self.retriever == "boom":
            AH._HYBRID_RETRIEVER = FakeRetriever(boom=True)
        else:
            AH._HYBRID_RETRIEVER = FakeRetriever(self.docs or [])
        return self

    def __exit__(self, *exc):
        AH._HYBRID_RETRIEVER, AH._HYBRID_BUILD_FAILED = self.saved
        return False


def check(name, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}{(' -> ' + str(extra))[:300] if extra else ''}")


class Stream:
    encoding = "utf-8"

    def iter_lines(self, decode_unicode=True):
        yield "data: " + json.dumps({"choices": [{"delta": {"content": REPLY}}]},
                                    ensure_ascii=False)
        yield "data: [DONE]"

    def close(self):
        pass


class Stubbed:
    """把 /api/chat 的外部依赖换成替身；rag 走 PatchRAG 控制。"""

    def __init__(self, rag=("", [])):
        self.rag = rag
        self.sources_sent = None

    def __enter__(self):
        cfg = dict(A.API_CONFIG)
        cfg["api_key"] = "TEST-ONLY-FAKE-KEY"
        self.saved = {
            "API_CONFIG": A.API_CONFIG, "call_llm": A.call_llm, "_llm_send": A._llm_send,
            "get_plugin_data": A.get_plugin_data, "retrieve_knowledge": A.retrieve_knowledge,
            "_extract_locations_from_text": A._extract_locations_from_text,
            "logger_disabled": A.app.logger.disabled,
        }
        A.API_CONFIG = cfg

        def _call_llm(messages, stream=False, skill_name="", tools=None, tool_choice=None):
            if stream:
                return {"url": "http://127.0.0.1:1/v1/chat/completions",
                        "headers": {}, "payload": {"messages": messages, "stream": True}}
            return {"choices": [{"message": {"content": ""}}]}

        def _retrieve(user_input, top_k=3):
            self.sources_sent = self.rag[1]
            return self.rag

        A.call_llm = _call_llm
        A._llm_send = lambda *a, **kw: (Stream(), None)
        A.get_plugin_data = lambda *a, **kw: ""
        A.retrieve_knowledge = _retrieve
        A._extract_locations_from_text = lambda *a, **kw: []
        A.app.logger.disabled = True
        return self

    def __exit__(self, *exc):
        for k in ("API_CONFIG", "call_llm", "_llm_send", "get_plugin_data",
                  "retrieve_knowledge", "_extract_locations_from_text"):
            setattr(A, k, self.saved[k])
        A.app.logger.disabled = self.saved["logger_disabled"]
        return False


def parse(raw):
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
        d["_type"] = "content" if "content" in d else ("done" if "done" in d else d.get("type", "?"))
        out.append(d)
    return out


# ==================== 用例 ====================

def test_retrieve_unit():
    print("\n[1] retrieve_knowledge：四种「没有依据」都不许编出处")
    with Isolated():
        check("空输入 → 空上下文空出处", AH.retrieve_knowledge("   ") == ("", []))
        check("空输入时 get_rag_context 也为空", AH.get_rag_context("") == "")
        with PatchRAG("unavailable"):
            check("检索器不可用 → (\"\", [])", AH.retrieve_knowledge("杭州西湖") == ("", []))
        with PatchRAG("ok", []):
            check("无命中 → (\"\", [])", AH.retrieve_knowledge("冷门问题") == ("", []))
        with PatchRAG("boom"):
            ctx, src = AH.retrieve_knowledge("杭州西湖")
            check("检索抛异常 → 降级为空且不抛出", ctx == "" and src == [])

        docs = [Doc("西湖  三面云山一面城\n\n苏堤春晓", 0.81,
                    {"name": "西湖景点", "city": "杭州", "type": "poi", "source": "本地知识库"}),
                Doc("灵隐寺门票信息", 0.42, {"source": "本地知识库"})]
        with PatchRAG("ok", docs):
            ctx, src = AH.retrieve_knowledge("杭州西湖怎么玩", top_k=3)
            check("命中 2 条出处", len(src) == 2, src)
            check("上下文字符串照旧注入提示词", "参考资料 1" in ctx and "三面云山" in ctx, ctx[:120])
            check("get_rag_context 与包装前一致", AH.get_rag_context("杭州西湖怎么玩") == ctx)
            first = src[0]
            check("出处带 name", first["name"] == "西湖景点", first)
            check("出处带 city/type", first["city"] == "杭州" and first["type"] == "poi", first)
            check("出处带 source", first["source"] == "本地知识库", first)
            check("score 已四舍五入", first["score"] == 0.81, first)
            check("snippet 折叠换行与多余空白",
                  first["snippet"] == "西湖 三面云山一面城 苏堤春晓", first["snippet"])
            check("snippet 不超过 120 字", len(first["snippet"]) <= 120, first["snippet"])
            check("没有 name 时回退到 source", src[1]["name"] == "本地知识库", src[1])
            check("序号从 1 开始", [x["index"] for x in src] == [1, 2], src)


def test_stream_sources():
    print("\n[2] 流式：preparation / sources 事件在正文之前就下发")
    conv = "conv-rag-stream"
    stub = Stubbed((" 参考上下文 ", SOURCES))
    with PatchRAG("ok", []), stub:
        resp = A.app.test_client().post("/api/chat", json={
            "message": "杭州西湖怎么玩", "conversation_id": conv, "stream": True})
        events = parse(resp.get_data(as_text=True))
    types_ = [e["_type"] for e in events]
    check("状态码 200", resp.status_code == 200, resp.status_code)
    check("有 preparation 事件", "preparation" in types_, types_)
    check("有 sources 事件", "sources" in types_, types_)
    check("sources 早于正文（用户能在生成时看到依据）",
          types_.index("sources") < types_.index("content"), types_)
    prep = [e for e in events if e["_type"] == "preparation"][0]
    check("preparation 带命中条数", prep["data"]["rag_sources"] == 1, prep)
    check("preparation 带采集耗时", isinstance(prep["data"]["collect_ms"], int), prep)
    check("preparation 如实说明无实时数据", prep["data"]["realtime"] is False, prep)
    src = [e for e in events if e["_type"] == "sources"][0]
    check("sources 内容就是检索到的条目", src["data"] == SOURCES, src)

    rows = [m for m in db.get_session_messages(conv) if m["role"] == "agent"]
    check("答复落库一条", len(rows) == 1, rows)
    if rows:
        stored = json.loads(rows[0]["sources_json"] or "[]")
        check("出处随答复落库（历史可追溯）", stored == SOURCES, rows[0].get("sources_json"))


def test_no_sources_honest():
    print("\n[3] 知识库没命中：不发 sources 事件，也不落空出处")
    conv = "conv-rag-none"
    stub = Stubbed(("", []))
    with PatchRAG("unavailable"), stub:
        resp = A.app.test_client().post("/api/chat", json={
            "message": "随便聊聊", "conversation_id": conv, "stream": True})
        events = parse(resp.get_data(as_text=True))
    types_ = [e["_type"] for e in events]
    check("答复照常生成", "content" in types_ and "[DONE]" in types_, types_)
    check("不发 sources 事件", "sources" not in types_, types_)
    prep = [e for e in events if e["_type"] == "preparation"][0]
    check("preparation 如实写 0 条命中", prep["data"]["rag_sources"] == 0, prep)
    rows = [m for m in db.get_session_messages(conv) if m["role"] == "agent"]
    check("落库 sources_json 为 NULL", rows and rows[0]["sources_json"] is None,
          rows[0].get("sources_json") if rows else None)


def test_nonstream_sources():
    print("\n[4] 非流式：JSON 里带 sources 与 collect_ms")
    conv = "conv-rag-nonstream"
    with PatchRAG("ok", []), Stubbed((" 参考上下文 ", SOURCES)):
        resp = A.app.test_client().post("/api/chat", json={
            "message": "杭州西湖怎么玩", "conversation_id": conv, "stream": False})
    body = resp.get_json()
    check("非流式仍 200", resp.status_code == 200 and body["success"], body)
    check("JSON 带 sources", body.get("sources") == SOURCES, body.get("sources"))
    check("JSON 带 collect_ms", isinstance(body.get("collect_ms"), int), body.get("collect_ms"))


def test_db_column():
    print("\n[5] messages.sources_json 列与迁移幂等")
    cols = [r[1] for r in db.get_connection().execute("PRAGMA table_info(messages)").fetchall()]
    check("messages 表有 sources_json 列", "sources_json" in cols, cols)
    try:
        db.init_db()
        check("重复 init_db 不报错（列已存在被吞掉）", True)
    except Exception as e:  # noqa: BLE001
        check("重复 init_db 不报错（列已存在被吞掉）", False, e)
    db.create_session(user_id="default", session_id="conv-col-src")
    m = db.save_message("conv-col-src", "agent", "答复", message_id="mid-src",
                        sources_json=json.dumps(SOURCES, ensure_ascii=False))
    check("写入 sources_json", m["sources_json"], m)
    back = db.get_session_messages("conv-col-src")[0]
    check("读回 sources_json 一致", json.loads(back["sources_json"]) == SOURCES, back)
    m2 = db.save_message("conv-col-src", "agent", "答复2", message_id="mid-src2")
    check("不传时为 NULL（不是空字符串）", m2["sources_json"] is None, m2)


def main():
    print("=" * 60)
    print("知识库出处可追溯（RAG citations）测试")
    print("=" * 60)
    test_retrieve_unit()
    test_stream_sources()
    test_no_sources_honest()
    test_nonstream_sources()
    test_db_column()
    total = _PASS + _FAIL
    print("\n" + "=" * 60)
    print(f"结果：{_PASS} passed, {_FAIL} failed（共 {total}）")
    print("=" * 60)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
