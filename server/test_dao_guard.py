# -*- coding: utf-8 -*-
"""
DAO 降级护栏测试（server/database.py）
======================================
护栏的边界是这条测试要钉住的重点：
  1. 隐式/后台写入（会话摘要、会话状态、记忆命中与修剪、计数）失败 → 返回类型正确的空值 + 留痕；
  2. 用户主动发起的 CRUD（会话创建、消息保存、行程/营销/笔记/收藏、各类 list/get）失败 → 必须照抛，
     否则接口会回 200 + 空数据，等于告诉用户「保存成功了」；
  3. 降级必须可观测：db_health() 计数 + /api/admin/ops 的 database 段 + database logger 接进文件日志。

用临时库（SMARTTOURISM_DB），不碰 data/smarttourism.db；全程离线。

运行：  python server/test_dao_guard.py
"""

from __future__ import annotations

import logging
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

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="st_daoguard_test_"), "test.db")
os.environ["SMARTTOURISM_DB"] = _TMPDB
os.environ.setdefault("SMARTTOURISM_MEMORY", "0")
os.environ.setdefault("SMARTTOURISM_LLM_CACHE", "0")

import database as db  # noqa: E402
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
        print(f"  [FAIL] {name}{(' -> ' + str(extra))[:300] if extra else ''}")


# 降级安全（有护栏）/ 必须照抛（无护栏）
GUARDED = ("update_session_summary", "save_session_state", "get_session_state",
           "bump_memory_hit", "prune_memories", "memory_count")
UNGUARDED = ("create_session", "save_message", "get_session", "get_session_messages",
             "list_user_sessions", "insert_memory", "list_memories", "save_itinerary",
             "save_marketing_content", "upsert_xhs_note", "save_user_store_item")


class BrokenConn:
    """把 get_connection 换成抛异常，模拟磁盘满 / 库文件被独占 / 表损坏。"""

    def __enter__(self):
        self.saved = db.get_connection
        self.saved_logger = logging.getLogger("database").disabled

        def _boom():
            raise RuntimeError("disk I/O error（模拟）")

        db.get_connection = _boom
        logging.getLogger("database").disabled = True  # 不把测试噪声写进 data/logs
        return self

    def __exit__(self, *exc):
        db.get_connection = self.saved
        logging.getLogger("database").disabled = self.saved_logger
        return False


def test_decorator_semantics():
    print("\n[1] guarded 装饰器：透传 / 降级 / 工厂默认值不共享对象")
    calls = []

    @db.guarded(default=list)
    def fake_read(x):
        calls.append(x)
        if x == "bad":
            raise RuntimeError("boom")
        return [x]

    check("成功时原样返回", fake_read("ok") == ["ok"], fake_read("ok"))
    check("失败返回 default", fake_read("bad") == [])
    a, b = fake_read("bad"), fake_read("bad")
    a.append(1)
    check("工厂默认值每次是新对象（不共享）", b == [], b)
    check("失败后仍继续可用", fake_read("ok2") == ["ok2"])


def test_guarded_daos_degrade():
    print("\n[2] 库炸掉时：隐式/后台 DAO 返回类型正确的空值")
    before = db.db_health()["total_failures"]
    with BrokenConn():
        check("save_session_state → None", db.save_session_state("s1", facts_json="{}") is None)
        check("get_session_state → None", db.get_session_state("s1") is None)
        check("update_session_summary → None", db.update_session_summary("s1", "摘要") is None)
        check("bump_memory_hit → None", db.bump_memory_hit("mem_x", 0.5) is None)
        check("prune_memories → 0", db.prune_memories("u1", keep=5) == 0)
        check("memory_count → 0", db.memory_count("u1") == 0)
    health = db.db_health()
    check("失败被计数", health["total_failures"] >= before + 6, health["total_failures"])
    check("degraded 置位", health["degraded"] is True, health)
    check("按函数计数齐全", all(n in health["by_function"] for n in GUARDED),
          sorted(health["by_function"]))
    sample = health["last_errors"].get("memory_count") or {}
    check("最近错误带 ts/exc_type/message",
          set(sample) >= {"ts", "exc_type", "message"} and sample["exc_type"] == "RuntimeError",
          sample)


def test_unguarded_daos_still_raise():
    print("\n[3] 用户 CRUD 的 DAO 必须照抛（不能返回空值假装保存成功）")
    with BrokenConn():
        for name in UNGUARDED:
            fn = getattr(db, name)
            try:
                if name == "create_session":
                    fn(user_id="u", session_id="s-raise")
                elif name == "save_message":
                    fn("s-raise", "user", "hi")
                elif name in ("get_session", "get_session_messages", "list_user_sessions",
                              "insert_memory", "list_memories"):
                    fn("s-raise") if name in ("get_session", "get_session_messages") else fn()
                elif name == "save_itinerary":
                    fn("s-raise", "杭州", 3, "{}")
                elif name == "save_marketing_content":
                    fn("s-raise", "wechat", "post", "正文")
                elif name == "upsert_xhs_note":
                    fn({"note_id": "n1", "title": "t", "content": "c"})
                elif name == "save_user_store_item":
                    fn("u1", "orders", {"a": 1})
                raised = False
            except Exception:  # noqa: BLE001
                raised = True
            check(f"{name} 失败时抛异常", raised)


def test_normal_path_unaffected():
    print("\n[4] 库正常时护栏不改变行为（真实读写一遍）")
    sid = "dao-guard-ok"
    sess = db.create_session(user_id="u-guard", session_id=sid)
    check("create_session 返回会话字典", isinstance(sess, dict) and sess.get("session_id") == sid, sess)
    db.save_message(sid, "user", "去杭州")
    db.save_message(sid, "agent", "好的")
    rows = db.get_session_messages(sid)
    check("消息可读回", len(rows) == 2 and rows[0]["content"] == "去杭州", rows)
    db.save_session_state(sid, facts_json='{"destination":"杭州"}', folded_rounds=2)
    st = db.get_session_state(sid)
    check("会话状态可读回", st and st.get("folded_rounds") == 2, st)
    db.update_session_summary(sid, "早期摘要")
    check("摘要写进 sessions", (db.get_session(sid) or {}).get("history_summary") == "早期摘要")
    mid = "mem_guard_1"
    db.insert_memory({"memory_id": mid, "user_id": "u-guard", "kind": "preference",
                      "slot": "budget", "content": "预算2000"})
    check("memory_count 真实计数", db.memory_count("u-guard") == 1, db.memory_count("u-guard"))
    db.bump_memory_hit(mid, 0.8)
    check("bump_memory_hit 生效",
          (db.list_memories(user_id="u-guard") or [{}])[0].get("hit_count") == 1)
    check("prune_memories 返回删除条数", db.prune_memories("u-guard", keep=0) == 1)
    check("修剪后计数归零", db.memory_count("u-guard") == 0)


def test_observability_wiring():
    print("\n[5] 降级可观测：ops 接口有 database 段，database logger 接进文件日志")
    snap = A.app.test_client().get("/api/admin/ops").get_json() or {}
    d = snap.get("database") or {}
    check("/api/admin/ops 返回 database 段", bool(d), list(snap)[:8])
    check("database.ready 反映启动状态", d.get("ready") is A.DB_READY, d.get("ready"))
    check("database.path 是临时库", d.get("path") == _TMPDB, d.get("path"))
    check("database.total_failures 与 db_health 一致",
          d.get("total_failures") == db.db_health()["total_failures"], d.get("total_failures"))
    check("database.by_function 透出", isinstance(d.get("by_function"), dict), d.get("by_function"))

    lg = logging.getLogger("database")
    check("database logger 挂了文件 handler", len(lg.handlers) >= 1, lg.handlers)
    check("database logger 不向 root 冒泡（避免重复打印）", lg.propagate is False, lg.propagate)


def main():
    print("=" * 60)
    print("DAO 降级护栏测试")
    print("=" * 60)
    test_decorator_semantics()
    test_guarded_daos_degrade()
    test_unguarded_daos_still_raise()
    test_normal_path_unaffected()
    test_observability_wiring()
    total = _PASS + _FAIL
    print("\n" + "=" * 60)
    print(f"结果：{_PASS} passed, {_FAIL} failed（共 {total}）")
    print("=" * 60)
    try:
        os.remove(_TMPDB)
    except Exception:  # noqa: BLE001
        pass
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
