# -*- coding: utf-8 -*-
"""
多会话管理测试：历史对话必须留得住、认得出、切得回
==================================================
背景（用户提的硬性要求）：点「新建对话」绝不能把上一次的对话内容删掉。
旧实现里 newConversation() 调的是 DELETE /api/conversations/<id>，
它直接 DELETE FROM messages —— 那是真删库，不是「另起一页」。

覆盖：
  1. sessions.title 迁移存在且 init_db 幂等；
  2. 标题生成：首条提问压平换行、截断，空输入不给标题；
  3. 新建会话跑一轮自动补标题，已有标题不被后续消息覆盖；
  4. list_user_sessions 带 title / message_count / preview，并按用户隔离；
  5. 改名与删除的归属校验：别人的会话改不了也删不掉，删除级联清掉消息；
  6. /api/sessions、PUT title、DELETE 三个接口的成功与错误码；
  7. 前端接线：新建对话不再打删除接口，面板与切换/改名/删除函数都在；
  8. 回归：两条会话各自发消息后都还在（新建 ≠ 清空）。

用临时库（SMARTTOURISM_DB），不调用真实大模型与第三方接口。

运行：  python server/test_session_management.py
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
_TMPDB = os.path.join(tempfile.mkdtemp(prefix="st_sessions_"), "test.db")
os.environ["SMARTTOURISM_DB"] = _TMPDB
os.environ.setdefault("SMARTTOURISM_MEMORY", "0")
os.environ.setdefault("SMARTTOURISM_LLM_CACHE", "0")

import app as A  # noqa: E402
import database as db  # noqa: E402

_PASS = 0
_FAIL = 0
REPLY = "杭州三天：第一天西湖苏堤，第二天灵隐寺，第三天河坊街。"
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
    """把 /api/chat 的外部依赖全换成替身，只留 DB 是真的（临时库）。"""

    def __enter__(self):
        self.saved = {k: getattr(A, k) for k in (
            "API_CONFIG", "call_llm", "get_plugin_data", "get_rag_context",
            "retrieve_knowledge", "_extract_locations_from_text")}
        self.logger_disabled = A.app.logger.disabled
        A.API_CONFIG = {"base_url": "http://127.0.0.1:1/v1", "api_key": "TEST-ONLY-FAKE-KEY",
                        "model": "test-model"}
        A.call_llm = lambda messages, stream=False, skill_name="", tools=None, tool_choice=None: {
            "choices": [{"message": {"content": REPLY}}]}
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


def post_chat(conv, message, user_id=None):
    body = {"message": message, "skill": "", "conversation_id": conv, "stream": False}
    if user_id:
        body["user_id"] = user_id
    with StubbedLLM():
        resp = A.app.test_client().post("/api/chat", json=body)
    return resp.status_code, resp.get_json()


def session_row(session_id):
    conn = db.get_connection()
    try:
        row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def msg_count(session_id):
    conn = db.get_connection()
    try:
        return conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?",
                            (session_id,)).fetchone()[0]
    finally:
        conn.close()


# ==================== 用例 ====================

def test_migration():
    print("\n[1] sessions.title：列存在、重复迁移不报错")
    db.init_db()
    db.init_db()
    check("init_db 幂等", True)
    conn = db.get_connection()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    finally:
        conn.close()
    check("sessions 有 title 列", "title" in cols, sorted(cols))
    check("新建会话返回带 title 的行", "title" in (db.create_session(user_id="u-mig") or {}))


def test_title_generation():
    print("\n[2] title_from_message：压平换行 / 截断 / 空输入")
    check("普通提问原样", db.title_from_message("杭州3天怎么玩") == "杭州3天怎么玩")
    check("换行压成空格", db.title_from_message("杭州\n\t3天\r\n怎么玩") == "杭州 3天 怎么玩")
    check("首尾空白去掉", db.title_from_message("  推荐酒店  ") == "推荐酒店")
    long = db.title_from_message("帮" * 40)
    check("超长截到 20 字加省略号", long == "帮" * 20 + "…" and len(long) == 21, long)
    check("空输入不给标题", db.title_from_message("") == "")
    check("None 不给标题", db.title_from_message(None) == "")


def test_auto_title_on_chat():
    print("\n[3] 新建会话自动补标题，已有标题不被后续消息覆盖")
    conv = "conv-auto-title"
    code, _ = post_chat(conv, "我想去杭州玩3天，帮我规划包含景点、美食和住宿的行程")
    check("聊天请求成功", code == 200, code)
    row = session_row(conv)
    check("会话已建", row is not None)
    check("标题取首条提问前 20 字加省略号",
          (row or {}).get("title") == "我想去杭州玩3天，帮我规划包含景点、美食…",
          (row or {}).get("title"))
    check("标题不超 21 字", len((row or {}).get("title") or "") <= 21)

    # 手动改名后再发消息：只_if_empty 的那次写入不能把名字改回去
    check("改名成功", db.rename_session(conv, "default", "杭州之行") is True)
    post_chat(conv, "第二天去哪吃饭")
    check("后续消息不覆盖手动标题", session_row(conv)["title"] == "杭州之行", session_row(conv))

    # 护栏：DB 挂了自动标题只是不生效，不能把答复带崩
    check("不存在的会话补标题返回 0", db.set_session_title("conv-never-x", "x") == 0)
    check("空标题不写", db.set_session_title(conv, "") == 0)


def test_list_sessions():
    print("\n[4] list_user_sessions：标题 / 条数 / 末条摘要 / 按用户隔离")
    a, b = "conv-list-a", "conv-list-b"
    db.create_session(user_id="u-list", session_id=a)
    db.create_session(user_id="u-list", session_id=b)
    db.create_session(user_id="u-other", session_id="conv-list-other")
    db.save_message(a, "user", "第一条提问")
    db.save_message(a, "agent", "第一段答复\n第二行不该出现在摘要里")
    db.save_message(b, "user", "第二条会话的提问")
    db.set_session_title(a, "第一条会话")
    db.set_session_title(b, "杭州3天行程")

    rows = db.list_user_sessions(user_id="u-list")
    check("只列出本人会话", len(rows) == 2, [r["session_id"] for r in rows])
    by_id = {r["session_id"]: r for r in rows}
    check("带 title", by_id[b]["title"] == "杭州3天行程", by_id[b])
    check("自动补的标题只写一次",
          db.set_session_title(a, "第二次写的") == 0 and session_row(a)["title"] == "第一条会话",
          session_row(a))
    check("带 message_count", by_id[a]["message_count"] == 2, by_id[a])
    check("preview 取末条", by_id[a]["preview"].startswith("第一段答复"), by_id[a])
    check("first_message 取首条（老会话没标题时用它）",
          by_id[a]["first_message"] == "第一条提问", by_id[a])
    check("preview 不含换行", "\n" not in by_id[a]["preview"] and "\r" not in by_id[a]["preview"])
    check("他人会话不串", "conv-list-other" not in by_id)
    check("limit 生效", len(db.list_user_sessions(user_id="u-list", limit=1)) == 1)
    check("空用户返回空列表", db.list_user_sessions(user_id="u-none-here") == [])


def test_rename_and_delete():
    print("\n[5] 改名与删除：归属校验 + 删除级联")
    conv = "conv-own"
    db.create_session(user_id="u-mine", session_id=conv)
    db.save_message(conv, "user", "会被一起删掉的问题")
    db.save_message(conv, "agent", "会被一起删掉的答复")

    check("别人改不了", db.rename_session(conv, "u-other", "别人的名字") is False)
    check("别人删不掉", db.delete_session(conv, "u-other") is False)
    check("越权后标题没变", session_row(conv)["title"] is None)
    check("空标题拒绝", db.rename_session(conv, "u-mine", "   ") is False)
    check("本人改名成功", db.rename_session(conv, "u-mine", "  温州出发  ") is True)
    check("改名去掉首尾空白", session_row(conv)["title"] == "温州出发")
    check("超长标题截到 60", db.rename_session(conv, "u-mine", "长" * 80) is True
          and len(session_row(conv)["title"]) == 60)

    check("本人删除成功", db.delete_session(conv, "u-mine") is True)
    check("会话已没了", session_row(conv) is None)
    check("消息级联删除", msg_count(conv) == 0, msg_count(conv))
    check("重复删除返回 False", db.delete_session(conv, "u-mine") is False)
    check("不存在的会话改名 False", db.rename_session("conv-never-y", "u-mine", "x") is False)


def test_apis():
    print("\n[6] 接口：GET /api/sessions、PUT title、DELETE")
    c = A.app.test_client()
    uid = "u-api"
    conv = "conv-api"
    db.create_session(user_id=uid, session_id=conv)
    db.save_message(conv, "user", "接口测试提问")

    data = c.get("/api/sessions?user_id=" + uid).get_json()
    check("列表 success", data.get("success") is True)
    item = {s["session_id"]: s for s in data["sessions"]}.get(conv)
    check("列表含该会话", item is not None, data)
    check("列表含 title/count/preview",
          {"title", "message_count", "preview"} <= set(item or {}), item)
    check("列表按 user_id 隔离", "conv-list-a" not in {s["session_id"] for s in data["sessions"]})

    r = c.put("/api/sessions/" + conv + "/title", json={"title": "  改个名字  "}, query_string={"user_id": uid})
    check("改名 200", r.status_code == 200, r.get_json())
    check("改名生效并去空白", session_row(conv)["title"] == "改个名字")

    r = c.put("/api/sessions/" + conv + "/title", json={"title": "  "}, query_string={"user_id": uid})
    check("空标题 400", r.status_code == 400, r.status_code)
    r = c.put("/api/sessions/conv-never-z/title", json={"title": "x"}, query_string={"user_id": uid})
    check("别人的/不存在会话 404", r.status_code == 404, r.status_code)
    r = c.put("/api/sessions/" + conv + "/title", json={"other": 1}, query_string={"user_id": uid})
    check("缺 title 字段 400", r.status_code == 400, r.status_code)

    r = c.delete("/api/sessions/" + conv, query_string={"user_id": "u-not-owner"})
    check("非本人删除 404", r.status_code == 404, r.status_code)
    check("非本人删除后还在", session_row(conv) is not None)
    r = c.delete("/api/sessions/" + conv, query_string={"user_id": uid})
    check("本人删除 200", r.status_code == 200, r.get_json())
    check("删除后会话消失", session_row(conv) is None)
    r = c.delete("/api/sessions/conv-never-w")
    check("删除不存在的会话 404", r.status_code == 404, r.status_code)


def test_dashboard_wiring():
    print("\n[7] 前端接线：新建对话不再删库")
    with open(DASHBOARD, encoding="utf-8") as f:
        html = f.read()
    start = html.index("function newConversation()")
    body = html[start:start + 900]
    check("newConversation 存在", start > 0)
    check("新建对话不打删除接口", "/api/conversations/" not in body, body[:200])
    check("新建对话不删 messages", "DELETE" not in body)
    check("新建前先归档", "archiveCurrentConversation()" in body)
    check("有历史对话面板", 'id="conv-panel"' in html and "toggleConvPanel" in html)
    check("有切换函数", "async function switchConversation(" in html)
    check("有改名函数", "renameConversation(" in html and "/title" in html)
    check("有删除函数与两步确认", "async function deleteConv(" in html and "确认删除？" in html)
    check("列表读 /api/sessions", "refreshConvPanel" in html and "/api/sessions?limit=" in html)
    check("切换后从服务端载入历史", "/api/sessions/' + encodeURIComponent(id) + '/messages" in html)
    check("本地归档兜底", "chat_conv_archive" in html)
    check("标题写进属性用 escapeAttr", 'data-id="' in html and "escapeAttr(it.id)" in html)
    check("新建按钮仍在", 'id="new-chat-btn"' in html)


def test_new_conversation_keeps_history():
    print("\n[8] 回归：两条会话各自发消息，内容都还在")
    c1, c2 = "conv-keep-1", "conv-keep-2"
    check("第一条会话聊过", post_chat(c1, "第一条：杭州3天怎么玩")[0] == 200)
    # 前端「新建对话」= 换个 conversation_id 再问，旧的一条一个字都不动
    check("第二条会话聊过", post_chat(c2, "第二条：推荐几家性价比高的酒店")[0] == 200)
    check("第一条仍有 2 条消息", msg_count(c1) == 2, msg_count(c1))
    check("第二条有 2 条消息", msg_count(c2) == 2, msg_count(c2))
    rows = db.list_user_sessions(user_id="default")
    ids = {r["session_id"] for r in rows}
    check("两条都在列表里", {c1, c2} <= ids, sorted(ids))
    check("列表各自带标题",
          all(r["title"] for r in rows if r["session_id"] in (c1, c2)),
          [(r["session_id"], r["title"]) for r in rows])
    # 显式清空仍然可用，并且是「清内容保留会话」，与删除会话是两回事
    r = A.app.test_client().delete("/api/conversations/" + c1)
    check("显式清空返回 success", r.get_json().get("success") is True)
    check("清空后消息为 0", msg_count(c1) == 0, msg_count(c1))
    check("清空后会话记录保留", session_row(c1) is not None)
    check("另一条不受影响", msg_count(c2) == 2)


def main():
    db.init_db()
    A.app.logger.disabled = True   # 不把测试噪声写进 data/logs/app.log
    try:
        for fn in (test_migration, test_title_generation, test_auto_title_on_chat,
                   test_list_sessions, test_rename_and_delete, test_apis,
                   test_dashboard_wiring, test_new_conversation_keeps_history):
            fn()
    finally:
        A.app.logger.disabled = False
    print(f"\n{'=' * 48}\n会话管理: {_PASS} passed / {_FAIL} failed\n{'=' * 48}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
