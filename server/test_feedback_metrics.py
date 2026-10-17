# -*- coding: utf-8 -*-
"""
答复反馈 + 真实答复质量指标测试
================================
「答复好不好」以前只有模型自己知道：用户不满意只能重新生成，平台收不到任何信号，
运维面板也只有请求量与错误率。这里补两条腿：

  1. 点赞/点踩落库（message_feedback），一人对一条答复一票，改票覆盖、可取消，
     删会话/撤回这一轮时票跟着答复一起走（不留孤票把指标永久压低）。
  2. 把内置的规则指标（违规词 / 价格来源标注 / 格式）跑在**库里真实落库的答复**上，
     与票数一起进 /api/admin/ops，让「用户说不满意」和「规则判不合格」能对照。

硬约束照旧：反馈是用户主动提交的写操作，失败必须如实报错，绝不回「感谢反馈」
把没记下的票说成记下了；没落库的答复不给投票入口。

覆盖：
  1. 建表/唯一约束/CHECK/外键级联；
  2. save_feedback 投票与改票；
  3. 归属校验与非法输入（越权、错会话、不存在的 id、给提问投票、非法 rating、缺参数）；
  4. delete_feedback 取消与重复取消；
  5. get_feedback_map 按用户隔离；
  6. feedback_stats 空库与有票时的数；
  7. recent_agent_answers 问答配对与末尾形状；
  8. 级联：删会话、重新生成撤回这一轮后不留孤票；
  9. score_live_answer 四种答复形态的指标判定（coverage 如实标不适用）；
  10. aggregate_live_scores 空样本、通过率、按票分组；
  11. POST /api/feedback 的 200/400/404 与取消；
  12. 历史消息接口带出已投状态；
  13. SSE answer_id 与落库 message_id 一致；非流式返回 message_id；
  14. /api/admin/ops 的 answer_quality 段与库里的数对得上；
  15. 前端与运维面板接线静态检查。

用临时库（SMARTTOURISM_DB），不调用真实大模型与第三方接口。

运行：  python server/test_feedback_metrics.py
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
_TMPDB = os.path.join(tempfile.mkdtemp(prefix="st_feedback_"), "test.db")
os.environ["SMARTTOURISM_DB"] = _TMPDB
os.environ.setdefault("SMARTTOURISM_MEMORY", "0")
os.environ.setdefault("SMARTTOURISM_LLM_CACHE", "0")

import app as A  # noqa: E402
import database as db  # noqa: E402
from agent import evaluation as ev  # noqa: E402

_PASS = 0
_FAIL = 0
REPLY = "西湖建议早上去，雷峰塔门票约 75 元（以景区当日公示为准）。"
DASHBOARD = os.path.join(_ROOT, "dashboard", "index.html")


def check(name, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}{(' -> ' + str(extra))[:300] if extra else ''}")


def seed(user_id, session_id, question, answer, truncated=False, sources=None):
    """建一条属于 user 的会话 + 一问一答，返回 agent 消息 id。"""
    db.create_session(user_id=user_id, session_id=session_id)
    db.save_message(session_id, "user", question)
    row = db.save_message(session_id, "agent", answer, truncated=truncated,
                          sources_json=(json.dumps(sources, ensure_ascii=False) if sources else None))
    return row["message_id"]


def clear_feedback():
    """清空票表（临时库由本测试独占）：让「空库不编数」这类断言不受前面用例影响。"""
    conn = db.get_connection()
    conn.execute("DELETE FROM message_feedback")
    conn.commit()
    conn.close()


# ==================== 用例 ====================

def test_schema():
    print("\n[1] message_feedback：建表、唯一约束、CHECK、外键级联")
    db.init_db()
    db.init_db()
    conn = db.get_connection()
    try:
        ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='message_feedback'").fetchone()
        check("表已建立", ddl is not None and ddl["sql"])
        text = (ddl["sql"] or "") if ddl else ""
        check("rating 只允许 up/down", "up','down" in text.replace(" ", "").replace("'", "'"))
        check("答复外键带级联删除", "ON DELETE CASCADE" in text)
        idx = conn.execute("SELECT sql FROM sqlite_master WHERE name='uq_feedback_message_user'").fetchone()
        check("一人一答复唯一索引", idx is not None and "UNIQUE" in (idx["sql"] or ""))
        try:
            conn.execute("INSERT INTO message_feedback (feedback_id, message_id, session_id, "
                         "user_id, rating) VALUES ('x','m','s','u','meh')")
            ok = False
        except Exception:  # noqa: BLE001
            ok = True
        check("非法 rating 被 CHECK 挡住", ok)
    finally:
        conn.close()


def test_vote_and_change():
    print("\n[2] 投票与改票：一票覆盖，不叠记录")
    mid = seed("u-vote", "s-vote", "杭州怎么玩", REPLY)
    r1 = db.save_feedback(mid, "s-vote", "u-vote", "up")
    check("点赞成功", r1.get("ok") is True, r1)
    check("票面记 up", (r1.get("feedback") or {}).get("rating") == "up")
    fid = (r1.get("feedback") or {}).get("feedback_id")

    r2 = db.save_feedback(mid, "s-vote", "u-vote", "down", "价格不对")
    check("改票成功", r2.get("ok") is True, r2)
    check("改票后是 down", (r2.get("feedback") or {}).get("rating") == "down")
    check("理由写进去了", (r2.get("feedback") or {}).get("reason") == "价格不对")
    check("同一条票（改票不是新增）", (r2.get("feedback") or {}).get("feedback_id") == fid, fid)
    conn = db.get_connection()
    try:
        n = conn.execute("SELECT COUNT(*) AS c FROM message_feedback WHERE message_id = ?",
                         (mid,)).fetchone()["c"]
    finally:
        conn.close()
    check("库里只有一票", n == 1, n)

    long_reason = db.save_feedback(mid, "s-vote", "u-vote", "down", "很" * 300)
    check("理由截到 200 字",
          len((long_reason.get("feedback") or {}).get("reason") or "") == 200,
          len((long_reason.get("feedback") or {}).get("reason") or ""))


def test_ownership_and_bad_input():
    print("\n[3] 归属与非法输入：查不到的答复一律不记票")
    clear_feedback()
    mid = seed("u-own", "s-own", "宁波有什么景点", REPLY)
    other = db.save_feedback(mid, "s-own", "u-other", "up")
    check("别人的答复投不了票", other.get("ok") is False and other.get("code") == "message_not_found", other)
    wrong_sess = db.save_feedback(mid, "s-other-session", "u-own", "up")
    check("会话对不上不记票", wrong_sess.get("code") == "message_not_found", wrong_sess)
    ghost = db.save_feedback("no-such-message", "s-own", "u-own", "up")
    check("不存在的 message_id 不记票", ghost.get("code") == "message_not_found", ghost)

    user_mid = db.get_session_messages("s-own")[0]["message_id"]
    on_question = db.save_feedback(user_mid, "s-own", "u-own", "up")
    check("给自己的提问点赞不给记", on_question.get("code") == "message_not_found", on_question)

    bad = db.save_feedback(mid, "s-own", "u-own", "meh")
    check("非法 rating 直接拒", bad.get("code") == "bad_rating", bad)
    missing = db.save_feedback("", "s-own", "u-own", "up")
    check("缺 message_id 直接拒", missing.get("code") == "bad_request", missing)
    stats = db.feedback_stats()
    check("这一段只有本人那一票没记上（total=0）", stats["total"] == 0, stats)


def test_cancel_and_map():
    print("\n[4][5] 取消票 + 按用户隔离的票映射")
    seed("u-map", "s-map", "绍兴鲁迅故里怎么去", REPLY)
    rows = db.get_session_messages("s-map")
    mid = [r for r in rows if r["role"] == "agent"][0]["message_id"]
    db.save_feedback(mid, "s-map", "u-map", "down")
    m1 = db.get_feedback_map("s-map", "u-map")
    check("本人看得到自己投的票", m1.get(mid, {}).get("rating") == "down", m1)
    check("别人看不到这张票", db.get_feedback_map("s-map", "u-another") == {})
    check("取消一票删 1 条", db.delete_feedback(mid, "u-map") == 1)
    check("取消后映射为空", db.get_feedback_map("s-map", "u-map") == {})
    check("重复取消返回 0", db.delete_feedback(mid, "u-map") == 0)
    check("取消别人的票删不动", db.delete_feedback(mid, "u-map") == 0)


def test_stats():
    print("\n[6] feedback_stats：空库不编数，有票时各项对得上")
    clear_feedback()
    empty = db.feedback_stats()
    check("空库 total=0", empty["total"] == 0, empty)
    check("空库点赞率是 None 而不是 0", empty["up_rate"] is None, empty)
    check("空库没有理由排行", empty["top_down_reasons"] == [])

    a = seed("u-stat", "s-stat", "问题一", REPLY)
    b = seed("u-stat2", "s-stat2", "问题二", REPLY)
    c = seed("u-stat3", "s-stat3", "问题三", REPLY)
    db.save_feedback(a, "s-stat", "u-stat", "up")
    db.save_feedback(b, "s-stat2", "u-stat2", "down", "信息不准确")
    db.save_feedback(c, "s-stat3", "u-stat3", "down", "信息不准确")
    st = db.feedback_stats()
    check("总数 3", st["total"] == 3, st)
    check("1 赞 2 踩", st["up"] == 1 and st["down"] == 2, st)
    check("点赞率 0.333", st["up_rate"] == 0.333, st)
    check("覆盖 3 条会话", st["sessions"] == 3, st)
    check("按天分组有一条", len(st["by_day"]) == 1 and st["by_day"][0]["up"] == 1, st["by_day"])
    top = st["top_down_reasons"]
    check("点踩理由 TOP 计数正确", top and top[0]["reason"] == "信息不准确" and top[0]["count"] == 2, top)


def test_recent_pairs():
    print("\n[7] recent_agent_answers：问答配对、票与出处都带上")
    mid = seed("u-pair", "s-pair", "莫干山住宿推荐", REPLY, truncated=True,
               sources=[{"name": "莫干山", "source": "kb"}])
    out = db.recent_agent_answers(limit=10)
    hit = [x for x in out if x["message_id"] == mid]
    check("答复取到了", len(hit) == 1, out[:1])
    row = hit[0]
    check("配上紧邻的提问", row["question"] == "莫干山住宿推荐", row)
    check("半截标记带出", row["truncated"] is True, row)
    check("有出处", row["has_sources"] is True, row)
    check("没投票时 rating 为 None", row["rating"] is None, row)
    db.save_feedback(mid, "s-pair", "u-pair", "down", "太啰嗦")
    again = [x for x in db.recent_agent_answers(limit=10) if x["message_id"] == mid][0]
    check("票带进样本", again["rating"] == "down" and again["feedback_reason"] == "太啰嗦", again)

    # 末尾形状对不上：agent 前面不是 user（比如两条答复相连）时不给硬凑问题
    conn = db.get_connection()
    conn.execute("INSERT INTO messages (message_id, session_id, role, content) "
                 "VALUES ('x-a1','s-pair','agent','答复甲')")
    conn.execute("INSERT INTO messages (message_id, session_id, role, content) "
                 "VALUES ('x-a2','s-pair','agent','答复乙')")
    conn.commit()
    conn.close()
    pair = [x for x in db.recent_agent_answers(limit=20) if x["message_id"] == "x-a1"]
    check("答复前不是提问时 question 为 None", pair and pair[0]["question"] is None, pair)
    check("样本数受 limit 约束", len(db.recent_agent_answers(limit=3)) == 3)
    check("limit 越界会被夹住", len(db.recent_agent_answers(limit=99999)) <= 500)


def test_cascade():
    print("\n[8] 级联：删会话、撤回这一轮都不能留下指向已删答复的票")
    mid = seed("u-casc", "s-casc", "台州有哪些海岛", REPLY)
    db.save_feedback(mid, "s-casc", "u-casc", "down", "没答到点上")
    check("投票已计入", db.feedback_stats()["total"] >= 1)
    check("删会话成功", db.delete_session("s-casc", "u-casc") is True)
    left = db.get_connection().execute(
        "SELECT COUNT(*) AS c FROM message_feedback WHERE message_id = ?", (mid,)).fetchone()["c"]
    check("会话删掉后票一起消失", left == 0, left)

    mid2 = seed("u-casc2", "s-casc2", "温州怎么安排一天", REPLY)
    db.save_feedback(mid2, "s-casc2", "u-casc2", "down")
    db.delete_last_turn("s-casc2")     # 重新生成会撤回这一轮
    left2 = db.get_connection().execute(
        "SELECT COUNT(*) AS c FROM message_feedback WHERE message_id = ?", (mid2,)).fetchone()["c"]
    check("撤回答复后票不留孤票", left2 == 0, left2)


def test_score_live_answer():
    print("\n[9] score_live_answer：规则指标跑在真实答复上")
    ok = ev.score_live_answer("西湖全天免费开放，建议早上六点前到，苏堤沿路都在湖边。",
                              "杭州有什么好玩的")
    check("正常答复均分 1.0", ok["overall"] == 1.0, ok)
    check("覆盖率如实标不适用",
          ok["metrics"]["coverage"]["applicable"] is False and
          "不适用" in ok["metrics"]["coverage"]["detail"], ok["metrics"]["coverage"])
    check("不参与均分（分母只有 3 项里适用的）", ok["failed"] == [], ok["failed"])

    banned = ev.score_live_answer("雷峰塔门票 75 元，绝对值得去，是全国第一。", "雷峰塔门票")
    check("违规词命中", any("no_banned_terms" in f for f in banned["failed"]), banned["failed"])
    check("无来源价格命中", any("source_tag" in f for f in banned["failed"]), banned["failed"])
    check("均分被拉到 0.333", banned["overall"] == 0.333, banned["overall"])

    hedged = ev.score_live_answer("雷峰塔门票约 75 元（以景区当日公示为准）。", "雷峰塔门票")
    check("带对冲/来源的价格通过", hedged["metrics"]["source_tag"]["passed"] is True, hedged)

    empty = ev.score_live_answer("", "随便问一句")
    check("空答复判死", empty["metrics"]["format"]["passed"] is False, empty)
    check("空答复也照实给数", empty["overall"] == 0.667, empty["overall"])


def test_aggregate():
    print("\n[10] aggregate_live_scores：样本少就照实说样本少")
    none = ev.aggregate_live_scores([])
    check("空样本 sample_size=0", none["sample_size"] == 0, none)
    check("空样本均分是 None 不是 0", none["mean_overall"] is None, none)
    check("空样本没有通过率", none["metric_pass_rate"] == {}, none["metric_pass_rate"])

    samples = [
        {"message_id": "1", "session_id": "s", "question": "门票多少钱",
         "truncated": False, "has_sources": True, "rating": "down",
         "score": ev.score_live_answer("门票 75 元，绝对值得。")},
        {"message_id": "2", "session_id": "s", "question": "天气",
         "truncated": True, "has_sources": False, "rating": "up",
         "score": ev.score_live_answer("晴天为主。")},
        {"message_id": "3", "session_id": "s", "question": "住宿",
         "truncated": False, "has_sources": False, "rating": None,
         "score": ev.score_live_answer("住宿 300 元起。")},
    ]
    agg = ev.aggregate_live_scores(samples)
    check("样本数 3", agg["sample_size"] == 3, agg)
    check("半截计数", agg["truncated"] == 1 and agg["with_sources"] == 1, agg)
    check("违规词通过率 1/3", agg["metric_pass_rate"]["no_banned_terms"] == 0.667,
          agg["metric_pass_rate"])
    check("未通过样本被列出", len(agg["failed_samples"]) >= 1, agg["failed_samples"])
    check("点赞组均分高于点踩组",
          agg["by_rating"]["up"]["mean_overall"] > agg["by_rating"]["down"]["mean_overall"],
          agg["by_rating"])
    check("未投票单独计出来", agg["by_rating"]["unvoted"] == 1 and agg["by_rating"]["voted"] == 2,
          agg["by_rating"])

    many = [{"message_id": str(i), "score": ev.score_live_answer("门票 75 元，绝对。")}
            for i in range(20)]
    check("失败清单只留前 10 条", len(ev.aggregate_live_scores(many)["failed_samples"]) == 10)


# ==================== 路由 ====================

def post_feedback(payload, uid=None):
    body = dict(payload)
    if uid:
        body["user_id"] = uid
    with A.app.test_client() as tc:
        r = tc.post("/api/feedback", json=body)
        return r.status_code, r.get_json()


def test_feedback_api():
    print("\n[11][12] POST /api/feedback 与历史带出票状态")
    mid = seed("u-api", "s-api", "丽水避暑去哪里", REPLY)
    code, body = post_feedback({"message_id": mid, "conversation_id": "s-api",
                                "user_id": "u-api", "rating": "up"})
    check("点赞 200", code == 200 and body.get("success") is True, body)
    check("回票面 rating", body.get("rating") == "up", body)

    code, body = post_feedback({"message_id": mid, "conversation_id": "s-api",
                                "user_id": "u-api", "rating": "down", "reason": "太啰嗦"})
    check("改票 200", code == 200 and body.get("rating") == "down", body)
    check("改票带回理由", body.get("reason") == "太啰嗦", body)

    code, body = post_feedback({"message_id": "ghost-id", "conversation_id": "s-api",
                                "user_id": "u-api", "rating": "down"})
    check("没落库的答复回 404", code == 404 and body.get("code") == "message_not_found", body)
    check("404 说明写清楚不是假装成功", bool(body.get("error")), body)

    code, body = post_feedback({"message_id": mid, "conversation_id": "s-api",
                                "user_id": "u-api", "rating": "meh"})
    check("非法 rating 回 400", code == 400 and body.get("code") == "bad_rating", body)

    code, body = post_feedback({"message_id": "", "conversation_id": "s-api",
                                "user_id": "u-api", "rating": "up"})
    check("缺参数回 400", code == 400, body)

    code, body = post_feedback({"message_id": mid, "conversation_id": "s-api",
                                "user_id": "u-api", "rating": "cancel"})
    check("取消回 200 且 rating 为 null", code == 200 and body.get("rating") is None, body)
    check("取消真的删掉了", db.get_feedback_map("s-api", "u-api") == {})

    # 历史接口把已投状态带出来（重新打开会话不能显示成「你没投过票」）
    db.save_feedback(mid, "s-api", "u-api", "down", "信息不准确")
    with A.app.test_client() as tc:
        r = tc.get("/api/sessions/s-api/messages?user_id=u-api")
        msgs = {m["message_id"]: m for m in r.get_json()["messages"]}
    check("历史接口带出票", msgs[mid].get("feedback_rating") == "down", msgs[mid])
    check("历史接口带出理由", msgs[mid].get("feedback_reason") == "信息不准确", msgs[mid])
    with A.app.test_client() as tc:
        r2 = tc.get("/api/sessions/s-api/messages?user_id=u-nobody")
        msgs2 = {m["message_id"]: m for m in r2.get_json()["messages"]}
    check("别人看不到这张票", "feedback_rating" not in msgs2[mid], msgs2[mid])


# ==================== 流式与非流式的 message_id ====================

class Stub:
    """把 /api/chat 的外部依赖换成替身；stream=True 时走上游流式替身。"""

    def __init__(self, chunks=("第一段。", "第二段。")):
        self.chunks = chunks

    def __enter__(self):
        self.saved = {k: getattr(A, k) for k in (
            "API_CONFIG", "call_llm", "_llm_send", "get_plugin_data", "get_rag_context",
            "retrieve_knowledge", "_extract_locations_from_text")}
        self.logger_disabled = A.app.logger.disabled
        cfg = dict(A.API_CONFIG)
        cfg["api_key"] = "TEST-ONLY-FAKE-KEY"
        cfg["base_url"] = "http://127.0.0.1:1/v1"
        A.API_CONFIG = cfg

        class Upstream:
            encoding = "utf-8"

            def __init__(self, chunks):
                self.chunks = chunks

            def iter_lines(self, decode_unicode=True):
                for c in self.chunks:
                    yield "data: " + json.dumps({"choices": [{"delta": {"content": c}}]},
                                                ensure_ascii=False)
                yield "data: [DONE]"

            def close(self):
                pass

        def _call_llm(messages, stream=False, skill_name="", tools=None, tool_choice=None):
            if stream:
                return {"url": "http://127.0.0.1:1/v1/chat/completions",
                        "headers": {"Authorization": "Bearer TEST-ONLY-FAKE-KEY"},
                        "payload": {"model": "test-model", "messages": messages, "stream": True}}
            return {"choices": [{"message": {"content": REPLY}}]}

        A.call_llm = _call_llm
        A._llm_send = lambda url, headers, payload, stream=False, timeout=120: (
            Upstream(self.chunks), None)
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


def chat(conv, message, uid, stream=True):
    with Stub():
        r = A.app.test_client().post("/api/chat", json={
            "message": message, "skill": "", "conversation_id": conv,
            "user_id": uid, "stream": stream})
        return r.status_code, r.get_data(as_text=True), r


def agent_rows(conv):
    return [m for m in db.get_session_messages(conv) if m["role"] == "agent"]


def test_answer_id_reaches_frontend():
    print("\n[13] answer_id 事件与落库 message_id 是同一个")
    conv = "conv-stream-id"
    code, body, _ = chat(conv, "杭州三天怎么玩", "u-sid", stream=True)
    check("流式 200", code == 200, code)
    check("SSE 里有 answer_id 事件", '"type": "answer_id"' in body, body[:200])
    ids = [m["message_id"] for m in agent_rows(conv)]
    check("落库有一条答复", len(ids) == 1, ids)
    check("下发的 id 就是落库的 id", ids and ('"message_id": "%s"' % ids[0]) in body, ids)
    ev_pos = body.find('"answer_id"')
    done_pos = body.find("[DONE]")
    check("id 在 [DONE] 之前就到（答复一出来就能投票）", 0 <= ev_pos < done_pos, (ev_pos, done_pos))

    # 拿到这个 id 真能投上票——证明前后端说的是同一个东西
    code, fb = post_feedback({"message_id": ids[0], "conversation_id": conv,
                              "user_id": "u-sid", "rating": "down"})
    check("用下发的 id 投票成功", code == 200 and fb.get("rating") == "down", fb)

    conv2 = "conv-nonstream-id"
    with Stub():
        r = A.app.test_client().post("/api/chat", json={
            "message": "宁波两天怎么玩", "conversation_id": conv2, "user_id": "u-nsid",
            "stream": False})
        j = r.get_json()
    check("非流式返回 message_id", bool(j.get("message_id")), j)
    check("非流式的 id 与落库一致",
          j.get("message_id") in [m["message_id"] for m in agent_rows(conv2)], j)
    code, fb2 = post_feedback({"message_id": j.get("message_id"), "conversation_id": conv2,
                               "user_id": "u-nsid", "rating": "up"})
    check("非流式答复也能投票", code == 200, fb2)


def test_ops_snapshot():
    print("\n[14] /api/admin/ops 的 answer_quality：票数和规则指标一起出")
    with A.app.test_client() as tc:
        r = tc.get("/api/admin/ops")
        snap = r.get_json()
    check("ops 有 answer_quality 段", "answer_quality" in snap, list(snap.keys())[:12])
    aq = snap.get("answer_quality") or {}
    check("带 sample_source", bool(aq.get("sample_source")), aq)
    conn = db.get_connection()
    try:
        total_agents = conn.execute("SELECT COUNT(*) AS c FROM messages WHERE role='agent'").fetchone()["c"]
    finally:
        conn.close()
    check("样本就是最近那批已落库答复（上限 30）",
          aq.get("sample_size") == min(30, total_agents), (aq.get("sample_size"), total_agents))
    check("反馈票数带出来了", (aq.get("feedback") or {}).get("total", 0) >= 2, aq.get("feedback"))
    check("票数与 DAO 口径一致",
          (aq.get("feedback") or {}).get("total") == db.feedback_stats()["total"],
          aq.get("feedback"))
    check("按票分组的均分是分开算的", "by_rating" in aq, list(aq.keys()))
    check("coverage 不参与均分说明写在面板数据里",
          aq.get("metric_pass_rate", {}).get("coverage") is None, aq.get("metric_pass_rate"))


def test_frontend_wired():
    print("\n[15] 前端与运维面板接线")
    html = open(DASHBOARD, encoding="utf-8").read()
    check("解析 answer_id 事件", "json.type === 'answer_id'" in html)
    check("答复对象带 answerId", "answerId: answerId" in html)
    check("投票函数存在", "async function voteAnswer" in html)
    check("按钮挂在有 answerId 的答复上", "m.answerId && !m.error" in html)
    check("点踩给理由候选", "FB_REASONS" in html and "已标注" in html)
    check("理由文本走 escapeHtml", "escapeHtml(t)" in html and "escapeHtml(m.feedbackReason)" in html)
    check("失败时本地状态回退", "m.feedback = prev.feedback" in html)
    check("历史回放带出票状态", "feedback_rating" in html and "m.feedback_rating || ''" in html)
    check("本地持久化带上投票落点", html.count("answerId: m.answerId || ''") >= 2)
    check("本地持久化带上票面状态", "feedback: m.feedback || ''" in html)
    check("按钮样式有选中态", ".msg-act.on" in html)
    ops = open(os.path.join(_HERE, "ops_log.py"), encoding="utf-8").read()
    check("运维面板渲染 answer_quality", "d.answer_quality" in ops)
    check("运维面板有点赞/点踩卡片", "反馈票数" in ops and "规则均分" in ops)
    check("未通过样本列出来", "failed_samples" in ops)
    chat_py = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
    check("反馈路由不是降级写（不套 _db_call）",
          'db.save_feedback(message_id, session_id, user_id, rating, data.get(\'reason\'))' in chat_py)
    check("没落库如实回 404", '"message_not_found"' in chat_py)


def main():
    print("=" * 64)
    print("答复反馈落库 + 真实答复质量指标测试")
    print("=" * 64)
    db.init_db()
    for fn in (test_schema, test_vote_and_change, test_ownership_and_bad_input,
               test_cancel_and_map, test_stats, test_recent_pairs, test_cascade,
               test_score_live_answer, test_aggregate, test_feedback_api,
               test_answer_id_reaches_frontend, test_ops_snapshot, test_frontend_wired):
        fn()
    print("\n" + "=" * 64)
    print(f"结果：{_PASS} passed / {_FAIL} failed")
    print("=" * 64)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
