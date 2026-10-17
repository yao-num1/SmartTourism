# -*- coding: utf-8 -*-
"""
重新生成 / 编辑重发测试：撤回必须只撤回「这一轮」
==================================================
两个按钮（🔄 重新生成 / ✎ 编辑重发）看起来简单，真正危险的是删：
库里最后一条如果不是用户要撤的那一轮，删下去吃掉的是更早一轮的好答复。
所以规则是 **先比对再删**，比不上的就不删，当新问题回答。

覆盖：
  1. delete_last_turn 的形状判断：一问一答删 2 条；末尾只有一条提问删 1 条；
     末尾只有答复删 1 条；空会话删 0 条；剩余顺序不变；
  2. regenerate：命中最后一条提问 → 旧问答被替换而不是叠成两条；
  3. regenerate：对不上 → 一律不删（上一轮原样保留），当新问题回答；
  4. 库里没历史时 regenerate → 400 nothing_to_regenerate（不谎称重发成功）；
  5. edit_message：带 edit_of 才撤回；改写后的提问落库；
  6. 撤回后内存缓存同步（否则下一句把「已撤掉的答复」当历史喂给模型）；
  7. 前端接线：按钮、payload 字段、本地错误气泡不给重新生成入口。

用临时库（SMARTTOURISM_DB），不调用真实大模型与第三方接口。

运行：  python server/test_regenerate.py
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

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="st_regenerate_"), "test.db")
os.environ["SMARTTOURISM_DB"] = _TMPDB
os.environ.setdefault("SMARTTOURISM_MEMORY", "0")
os.environ.setdefault("SMARTTOURISM_LLM_CACHE", "0")

import app as A  # noqa: E402
import database as db  # noqa: E402

_PASS = 0
_FAIL = 0
DASHBOARD = os.path.join(_ROOT, "dashboard", "index.html")


def check(name, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}{(' -> ' + str(extra))[:300] if extra else ''}")


class StubbedLLM:
    def __enter__(self):
        self.saved = {k: getattr(A, k) for k in (
            "API_CONFIG", "call_llm", "get_plugin_data", "get_rag_context",
            "retrieve_knowledge", "_extract_locations_from_text")}
        self.logger_disabled = A.app.logger.disabled
        A.API_CONFIG = {"base_url": "http://127.0.0.1:1/v1", "api_key": "TEST-ONLY-FAKE-KEY",
                        "model": "test-model"}
        A.call_llm = lambda messages, stream=False, skill_name="", tools=None, tool_choice=None: {
            "choices": [{"message": {"content": "重发生成的新答复"}}]}
        A.get_plugin_data = lambda *a, **kw: ""
        A.get_rag_context = lambda *a, **kw: ""
        A.retrieve_knowledge = lambda *a, **kw: ("", [])
        A._extract_locations_from_text = lambda *a, **kw: []
        A.app.logger.disabled = True
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(A, k, v)
        A.app.logger.disabled = self.logger_disabled
        return False


def chat(conv, message, **extra):
    body = {"message": message, "skill": "", "conversation_id": conv, "stream": False}
    body.update(extra)
    with StubbedLLM():
        resp = A.app.test_client().post("/api/chat", json=body)
    return resp.status_code, resp.get_json()


def rows(conv):
    return [(m["role"], m["content"]) for m in db.get_session_messages(conv)]


def mk_session(sid, user_id="default"):
    if not db.get_session(sid):
        db.create_session(user_id=user_id, session_id=sid)


# ==================== 用例 ====================

def test_delete_last_turn_shapes():
    print("\n[1] delete_last_turn：按末尾形状决定删几条")
    mk_session("rt-pair")
    for role, content in [("user", "问1"), ("agent", "答1"), ("user", "问2"), ("agent", "答2")]:
        db.save_message("rt-pair", role, content)
    check("一问一答删 2 条", db.delete_last_turn("rt-pair") == 2)
    check("删的是最后那一轮", rows("rt-pair") == [("user", "问1"), ("agent", "答1")], rows("rt-pair"))

    mk_session("rt-lone-user")
    db.save_message("rt-lone-user", "user", "上一问问")
    db.save_message("rt-lone-user", "agent", "上一答")
    db.save_message("rt-lone-user", "user", "只有提问没有答复")
    check("末尾只有提问只删 1 条", db.delete_last_turn("rt-lone-user") == 1)
    check("上一轮不受牵连", rows("rt-lone-user") == [("user", "上一问问"), ("agent", "上一答")],
          rows("rt-lone-user"))

    mk_session("rt-lone-agent")
    db.save_message("rt-lone-agent", "user", "更早的提问")
    db.save_message("rt-lone-agent", "agent", "更早的答复")
    db.save_message("rt-lone-agent", "agent", "连续两条答复的最后一条")
    check("末尾是答复但前一条也是答复 → 只删 1 条", db.delete_last_turn("rt-lone-agent") == 1)
    check("更早的问答完整保留", rows("rt-lone-agent") == [("user", "更早的提问"), ("agent", "更早的答复")],
          rows("rt-lone-agent"))

    check("空会话删出 0", db.delete_last_turn("rt-nope") == 0)


def test_regenerate_match():
    print("\n[2] regenerate 命中最后一条提问：替换而不是叠加")
    conv = "rt-api-match"
    st, _ = chat(conv, "杭州3天怎么玩")
    check("首轮成功", st == 200)
    check("首轮 2 条", len(rows(conv)) == 2, rows(conv))

    st, d = chat(conv, "杭州3天怎么玩", regenerate=True)
    check("重新生成成功", st == 200, d)
    check("答复被替换（还是 2 条）", len(rows(conv)) == 2, rows(conv))
    check("库里是新生成的答复", ("agent", "重发生成的新答复") in rows(conv), rows(conv))
    check("提问没被叠成两条", sum(1 for r in rows(conv) if r[0] == "user") == 1, rows(conv))

    st, _ = chat(conv, "第二天去哪吃饭")
    check("后续提问正常追加", st == 200 and len(rows(conv)) == 4, rows(conv))
    st, _ = chat(conv, "第二天去哪吃饭", regenerate=True)
    check("对最后一轮重新生成", st == 200 and len(rows(conv)) == 4, rows(conv))
    check("更早那条提问还在", ("user", "杭州3天怎么玩") in rows(conv), rows(conv))


def test_regenerate_mismatch():
    print("\n[3] regenerate 对不上：一律不删，当新问题回答")
    conv = "rt-api-mismatch"
    chat(conv, "宁波半天玩什么")
    before = rows(conv)
    st, d = chat(conv, "一句库里没有的话", regenerate=True)
    check("请求仍然成功", st == 200, d)
    check("旧的一轮原样保留", before == rows(conv)[:2], rows(conv))
    check("新问题被当成一轮存下", ("user", "一句库里没有的话") in rows(conv), rows(conv))


def test_regenerate_nothing_to_regenerate():
    print("\n[4] 库里没历史时 regenerate → 400，不谎称重发成功")
    st, d = chat("rt-api-empty", "一个没人答过的问题", regenerate=True)
    check("返回 400", st == 400, st)
    check("带可读错误", d.get("code") == "nothing_to_regenerate" and d.get("error"), d)
    check("没有落库", db.get_session("rt-api-empty") is None or rows("rt-api-empty") == [],
          rows("rt-api-empty"))


def test_edit_resend():
    print("\n[5] 编辑重发：edit_of 比对通过才撤回，改写后的提问落库")
    conv = "rt-api-edit"
    chat(conv, "帮我推荐几家西湖附近的酒店")
    st, d = chat(conv, "帮我改写后的提问", edit_message="帮我改写后的提问",
                 edit_of="帮我推荐几家西湖附近的酒店")
    check("编辑重发成功", st == 200, d)
    check("旧的提问被换掉", ("user", "帮我推荐几家西湖附近的酒店") not in rows(conv), rows(conv))
    check("新提问与答复各一条", rows(conv) == [("user", "帮我改写后的提问"),
                                            ("agent", "重发生成的新答复")], rows(conv))

    # edit_of 对不上：只加新的一轮，绝不吃掉别人的那一轮
    chat(conv, "第二轮提问")
    before = rows(conv)
    st, _ = chat(conv, "改写后的提问", edit_message="改写后的提问", edit_of="库里根本不存在的原话")
    check("对不上时不删", st == 200 and before == rows(conv)[:4], rows(conv))
    check("对不上时按新一轮存", len(rows(conv)) == 6, rows(conv))

    # 定位后缀不能进库：库里存的是清洗过的提问，比对与上下文回填都按这个口径
    conv2 = "rt-api-edit-gps"
    chat(conv2, "附近有什么好玩的（120.6993, 27.9938）")
    check("普通提问剥掉坐标后缀", ("user", "附近有什么好玩的") in rows(conv2), rows(conv2))
    st, _ = chat(conv2, "改成亲子一点（120.6993, 27.9938）",
                 edit_message="改成亲子一点（120.6993, 27.9938）", edit_of="附近有什么好玩的")
    check("编辑重发也剥掉坐标后缀", ("user", "改成亲子一点") in rows(conv2), rows(conv2))
    check("编辑重发没叠轮", len(rows(conv2)) == 2, rows(conv2))


def test_cache_consistency():
    print("\n[6] 撤回后内存缓存同步")
    conv = "rt-api-cache"
    chat(conv, "缓存测试提问")
    cached = A.conversations.get(conv) or []
    check("缓存里有这一轮", len(cached) == 2, cached)
    chat(conv, "缓存测试提问", regenerate=True)
    cached = A.conversations.get(conv) or []
    check("缓存没有叠成两轮", len(cached) == 2, cached)
    check("缓存里是重发后的答复",
          any(c["content"] == "重发生成的新答复" for c in cached), cached)


def test_dashboard_wiring():
    print("\n[7] 前端接线")
    with open(DASHBOARD, encoding="utf-8") as f:
        html = f.read()
    check("有重新生成按钮", "regenerateLastTurn()" in html and "🔄 重新生成" in html)
    check("有编辑重发按钮", "startEditTurn(" in html and "✎ 编辑重发" in html)
    check("编辑就地改写用 textarea", 'id="edit-input"' in html)
    check("Ctrl+Enter 重发", "confirmEditTurn()" in html and "ctrlKey" in html.replace("event.ctrlKey", "ctrlKey"))
    check("payload 带 regenerate", "payload.regenerate = true" in html)
    check("payload 带 edit_message 与 edit_of",
          "payload.edit_message" in html and "payload.edit_of" in html)
    check("regenerate/edit 不再重复 push 提问气泡",
          "if (!editMode && !regenMode) {" in html)
    check("本地错误气泡标了 error", "error: true" in html and "!m.error" in html)
    check("编辑只挂在最后一条提问", "idx === lastUserIdx && lastAgentIdx > idx" in html)
    check("生成中不给操作入口", "!isStreaming && editingIdx < 0 && conversationId" in html)
    check("撤回会同步截断本地消息", "messages.splice(idx + 1" in html and "messages.splice(agentIdx, 1)" in html)


def main():
    db.init_db()
    A.app.logger.disabled = True
    try:
        for fn in (test_delete_last_turn_shapes, test_regenerate_match, test_regenerate_mismatch,
                   test_regenerate_nothing_to_regenerate, test_edit_resend,
                   test_cache_consistency, test_dashboard_wiring):
            fn()
    finally:
        A.app.logger.disabled = False
    print(f"\n{'=' * 48}\n重新生成/编辑重发: {_PASS} passed / {_FAIL} failed\n{'=' * 48}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
