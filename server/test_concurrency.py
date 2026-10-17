# -*- coding: utf-8 -*-
"""
并发安全单元测试（waitress 多线程共用一个进程的场景）
=====================================================
覆盖三处真实竞态：
  1. 插件预订入口必须按请求隔离 —— 挂在模块变量上会让先取走的请求拿到别人的购票链接；
  2. 会话历史缓存必须加锁且只交出副本 —— append_history 是原地改写，同一会话并发两轮会串台；
  3. 会话缓存必须有上限 —— 会话 id 由客户端任意指定，不设上限就是无限内存。

用临时库（SMARTTOURISM_DB），不碰 data/smarttourism.db；全程离线，不调用大模型。

运行：  python server/test_concurrency.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading

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
_TMPDB = os.path.join(tempfile.mkdtemp(prefix="st_conc_test_"), "test.db")
os.environ["SMARTTOURISM_DB"] = _TMPDB
os.environ.setdefault("SMARTTOURISM_MEMORY", "0")
os.environ.setdefault("SMARTTOURISM_LLM_CACHE", "0")

import app as A  # noqa: E402
import context_manager as cm  # noqa: E402
import database as db  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def _bk(url, title="入口"):
    return {"url": url, "title": title, "type": "酒店"}


# ==================== 1. 预订入口按请求隔离 ====================

def test_bookings_isolated_per_request():
    print("\n[预订入口按请求隔离]")
    results, errors = {}, []
    # 两个请求同时持有各自的预订入口（barrier 卡在这一刻，正是旧实现串数据的窗口）
    barrier = threading.Barrier(2)

    def one_request(tag, items):
        try:
            with A.app.test_request_context("/api/chat", method="POST", json={}):
                A._reset_plugin_bookings()
                A._collect_plugin_bookings(items)
                barrier.wait(timeout=15)
                results[tag] = A._take_plugin_bookings()
                barrier.wait(timeout=15)
                # 取走之后同一请求再取应为空，且不能被别的请求回填
                results[tag + "_again"] = A._take_plugin_bookings()
        except Exception as e:  # noqa: BLE001
            errors.append(f"{tag}: {type(e).__name__}: {e}")

    ts = [threading.Thread(target=one_request, args=("A", [_bk("http://a")]))
          , threading.Thread(target=one_request, args=("B", [_bk("http://b")]))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)

    check("并发请求无异常", not errors, str(errors[:2]))
    urls_a = [b["url"] for b in results.get("A", [])]
    urls_b = [b["url"] for b in results.get("B", [])]
    check("A 只拿到自己的链接", urls_a == ["http://a"], str(urls_a))
    check("B 只拿到自己的链接", urls_b == ["http://b"], str(urls_b))
    check("取走即清空（重复取为空）",
          results.get("A_again") == [] and results.get("B_again") == [],
          str((results.get("A_again"), results.get("B_again"))))


def test_bookings_repeated_collect_appends():
    print("\n[预订入口累加与去重]")
    with A.app.test_request_context("/api/chat", method="POST", json={}):
        A._reset_plugin_bookings()
        A._collect_plugin_bookings([_bk("http://x", "酒店")])
        A._collect_plugin_bookings([_bk("http://y", "门票"), _bk("http://x", "酒店重复")])
        out = [b["url"] for b in A._take_plugin_bookings()]
        check("多次采集累加后去重", out == ["http://x", "http://y"], str(out))
        A._collect_plugin_bookings([])
        check("空采集不产生条目", A._take_plugin_bookings() == [])


def test_bookings_fallback_without_request():
    """脚本/后台线程里没有请求上下文，必须仍能工作，且不能无限堆积。"""
    print("\n[无请求上下文时回落模块变量]")
    A._reset_plugin_bookings()
    A._collect_plugin_bookings([_bk("http://only-in-global")])
    got = A._take_plugin_bookings()
    check("无上下文时仍可取回", [b["url"] for b in got] == ["http://only-in-global"], str(got))
    check("取回后模块变量已清空", A._module_bookings == [], str(A._module_bookings[:3]))

    many = [_bk(f"http://{i}") for i in range(200)]
    for chunk in (many[i:i + 20] for i in range(0, 200, 20)):
        A._collect_plugin_bookings(chunk)
    check("回落容器有长度上限", len(A._module_bookings) <= A._MODULE_BOOKINGS_CAP,
          f"len={len(A._module_bookings)}")
    A._reset_plugin_bookings()


# ==================== 2. 会话历史缓存：副本 + 加锁 ====================

def test_history_returns_copy():
    print("\n[会话历史返回副本]")
    sid = "conc-copy-1"
    db.create_session(user_id="u-conc", session_id=sid)
    db.save_message(sid, "user", "第一轮提问")
    A._conv_cache_drop(sid)

    h1 = A._load_conversation_history(sid)
    h2 = A._load_conversation_history(sid)
    check("读到的条数正确", len(h1) == 1, str(len(h1)))
    check("两次读取不是同一个 list 对象", h1 is not h2)

    h1.append({"role": "user", "content": "调用方自己加的一轮"})
    check("调用方原地追加不污染缓存", len(A._load_conversation_history(sid)) == 1,
          str(len(A._load_conversation_history(sid))))
    A._conv_cache_put(sid, h1)
    check("显式写回后缓存才更新", len(A._load_conversation_history(sid)) == 2)

    A._conv_cache_drop(sid)
    check("清缓存后从 SQLite 回源", len(A._load_conversation_history(sid)) == 1)
    check("未知会话返回空列表", A._load_conversation_history("conc-does-not-exist") == [])


def test_history_cache_is_bounded():
    print("\n[会话缓存有上限]")
    old = A.CONV_CACHE_MAX
    try:
        A.CONV_CACHE_MAX = 5
        for i in range(20):
            A._conv_cache_put(f"conc-cap-{i}", [{"role": "user", "content": "x"}])
        check("缓存被压回上限", len(A.conversations) <= 5, f"len={len(A.conversations)}")
        check("保留的是最近写入的", "conc-cap-19" in A.conversations, str(list(A.conversations)[:3]))
        check("淘汰出去的下次仍可从库里回源",
              A._load_conversation_history("conc-cap-0") == [])
    finally:
        A.CONV_CACHE_MAX = old
        for i in range(20):
            A._conv_cache_drop(f"conc-cap-{i}")


def test_concurrent_same_session_turns():
    """同一个会话被并发追问：不能抛异常，也不能写出半截历史。"""
    print("\n[同一会话并发多轮]")
    sid = "conc-race-1"
    db.create_session(user_id="u-conc", session_id=sid)
    A._conv_cache_drop(sid)
    errors = []

    def turn(tid):
        try:
            for i in range(10):
                history = A._load_conversation_history(sid)
                A.append_history(sid, history, f"提问{tid}-{i}", f"回答{tid}-{i}")
                A._conv_cache_put(sid, history)
        except Exception as e:  # noqa: BLE001
            errors.append(f"t{tid}: {type(e).__name__}: {e}")

    ts = [threading.Thread(target=turn, args=(k,)) for k in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    check("并发改写同一会话不抛异常", not errors, str(errors[:2]))

    final = A._load_conversation_history(sid)
    check("缓存长度不超过内存上限", len(final) <= cm.IN_MEMORY_CAP, f"len={len(final)}")
    check("历史结构完整（每条都有 role/content）",
          all(isinstance(m, dict) and m.get("role") and m.get("content") is not None
              for m in final), str(final[:2]))
    check("配对追加不会被并发拆散", len(final) % 2 == 0, f"len={len(final)}")


def main():
    print("=" * 60)
    print("并发安全单元测试")
    print("=" * 60)
    db.init_db()
    test_bookings_isolated_per_request()
    test_bookings_repeated_collect_appends()
    test_bookings_fallback_without_request()
    test_history_returns_copy()
    test_history_cache_is_bounded()
    test_concurrent_same_session_turns()
    total = _PASS + _FAIL
    print("\n" + "=" * 60)
    print(f"结果：{_PASS} passed, {_FAIL} failed（共 {total}）")
    print("=" * 60)
    try:
        os.remove(_TMPDB)
    except Exception:
        pass
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
