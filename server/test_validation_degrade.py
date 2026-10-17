# -*- coding: utf-8 -*-
"""
反幻觉校验降级可观测测试（server/anti_hallucination.py）
========================================================
事后校验是「保险丝」不是「闸机」：它自己出问题（解析/比对/读会话事实炸）只能让这一轮不校验，
不能把已经花钱生成、已经落库的答复打成 500。但降级也不能静默——这条测试钉住：
  1. validate_response() 三个环节各自坏掉时仍返回完整报告，绝不向上抛；
  2. 被跳过的环节记进 report.degraded，且降级不会被误报成「检出问题」；
  3. validation_stats() 聚合计数（runs / degraded_runs / issues / by_stage / last_error / rag），
     由 /api/admin/ops 的 validation 段透出；
  4. 每个环节首次失败打 WARNING，同类后续降为 DEBUG（不刷屏但看得见）；
  5. 知识库检索：构建失败永久降级且不重复尝试，检索异常从「DEBUG 完全看不见」变成有计数。

用临时库（SMARTTOURISM_DB），全程离线，不打真实大模型与第三方接口。

运行：  python server/test_validation_degrade.py
"""

from __future__ import annotations

import logging
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

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="st_validation_test_"), "test.db")
os.environ["SMARTTOURISM_DB"] = _TMPDB
os.environ.setdefault("SMARTTOURISM_MEMORY", "0")
os.environ.setdefault("SMARTTOURISM_LLM_CACHE", "0")

import anti_hallucination as AH  # noqa: E402
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


REPLY = "杭州西湖门票 90 元，建议上午去。"


class Capture(logging.Handler):
    """接住 anti_hallucination logger 的记录，断言 WARNING/DEBUG 分层。"""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


class Isolated:
    """把 AH logger 的 handler 换成采集器（避免测试噪声写进 data/logs），退出时还原。"""

    def __init__(self):
        self.log = logging.getLogger("anti_hallucination")

    def __enter__(self):
        self.cap = Capture()
        self.saved_handlers = list(self.log.handlers)
        self.saved_level = self.log.level
        self.log.handlers = [self.cap]
        self.log.setLevel(logging.DEBUG)
        return self.cap

    def __exit__(self, *exc):
        self.log.handlers = self.saved_handlers
        self.log.setLevel(self.saved_level)
        return False


def reset_stats():
    with AH._stats_lock:
        AH._validation_stats["runs"] = 0
        AH._validation_stats["degraded_runs"] = 0
        AH._validation_stats["issues"] = 0
        AH._validation_stats["by_stage"] = {}
        AH._validation_stats["last_error"] = {}
        AH._validation_stats["rag"] = {"build_failed": False, "retriever": "", "retrieval_errors": 0}
    AH._warned_stages.clear()


class Patch:
    """临时替换模块属性，退出时还原。"""

    def __init__(self, target, **attrs):
        self.target = target
        self.attrs = attrs

    def __enter__(self):
        self.saved = {k: getattr(self.target, k) for k in self.attrs}
        for k, v in self.attrs.items():
            setattr(self.target, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(self.target, k, v)
        return False


def raiser(msg="环节炸了（模拟）"):
    def _f(*a, **kw):
        raise RuntimeError(msg)
    return _f


class BadTracker:
    def check_consistency(self, session_id, text):
        raise RuntimeError("读会话事实失败（模拟）")


def section(title):
    print(f"\n[{title}]")


def main():
    print("=" * 60)
    print("反幻觉校验降级可观测测试")
    print("=" * 60)

    section("1] 环节各自坏掉：validate_response 不抛异常，报告仍可用")
    base = AH.validate_response(REPLY, "", "val-base")
    check("正常情况不降级", base.degraded == [] and base.overall_status in ("pass", "warning"),
          base.to_log_dict())
    check("正常情况有 fact_check 结果", base.fact_check is not None)

    with Patch(AH, extract_facts=raiser()):
        r = AH.validate_response(REPLY, "", "val-fact")
        check("事实提取炸了：不抛异常", True)
        check("事实提取炸了：degraded 标出环节", r.degraded == ["fact_check"], r.degraded)
        check("事实提取炸了：fact_check 为 None", r.fact_check is None)
        check("事实提取炸了：不是 error/500 语义", r.overall_status == "pass", r.overall_status)
        check("事实提取炸了：日志字典带 degraded",
              r.to_log_dict().get("degraded") == ["fact_check"], r.to_log_dict())

    with Patch(AH, validate_against_plugin_data=raiser()):
        r = AH.validate_response(REPLY, "插件数据", "val-plugin")
        check("插件比对炸了：degraded 标出环节", r.degraded == ["plugin_conflict"], r.degraded)
        check("插件比对炸了：冲突列表为空而不是崩", r.plugin_conflicts == [], r.plugin_conflicts)

    with Patch(AH, get_fact_tracker=lambda: BadTracker()):
        r = AH.validate_response(REPLY, "", "val-consistency")
        check("会话一致性炸了：degraded 标出环节", r.degraded == ["consistency"], r.degraded)

    with Patch(AH, extract_facts=raiser(), validate_against_plugin_data=raiser()):
        with Patch(AH, get_fact_tracker=lambda: BadTracker()):
            r = AH.validate_response(REPLY, "插件数据", "val-all")
            check("三个环节全炸：仍然返回报告", r is not None and r.overall_status == "pass",
                  r.overall_status)
            check("三个环节全炸：degraded 齐全",
                  sorted(r.degraded) == ["consistency", "fact_check", "plugin_conflict"], r.degraded)
            check("三个环节全炸：不误报成「检出问题」", r.has_issues is False, r.has_issues)

    with Patch(AH, validate_against_plugin_data=lambda *a, **kw: ["价格与美团数据不一致"]):
        r = AH.validate_response(REPLY, "插件数据", "val-issue")
        check("真检出问题时仍标 warning", r.overall_status == "warning" and r.has_issues, r.overall_status)
    with Patch(AH, extract_facts=raiser(), validate_against_plugin_data=lambda *a, **kw: ["冲突"]):
        r = AH.validate_response(REPLY, "插件数据", "val-mix")
        check("降级 + 检出问题并存：warning 优先", r.overall_status == "warning", r.overall_status)
        check("降级 + 检出问题并存：degraded 仍保留", r.degraded == ["fact_check"], r.degraded)

    section("2] validation_stats：计数与最近错误明细")
    reset_stats()
    # 用不含价格的文本做「干净轮次」，否则 extract_facts 会把无来源价格算作检出问题，计数对不上
    ok_reply = "杭州秋天最适合去，西湖边风景很好。"
    for _ in range(3):
        AH.validate_response(ok_reply, "", "val-count-ok")
    with Patch(AH, extract_facts=raiser("x" * 500)):
        for _ in range(2):
            AH.validate_response(ok_reply, "", "val-count-bad")
    with Patch(AH, validate_against_plugin_data=lambda *a, **kw: ["冲突"]):
        AH.validate_response(ok_reply, "插件数据", "val-count-issue")
    st = AH.validation_stats()
    check("runs 累计 6", st["runs"] == 6, st)
    check("degraded_runs 累计 2", st["degraded_runs"] == 2, st)
    check("issues 只统计真检出问题的轮次", st["issues"] == 1, st)
    check("by_stage 按环节计数", st["by_stage"].get("fact_check") == 2, st["by_stage"])
    check("last_error 带 ts/exc_type/message",
          {"ts", "exc_type", "message"} <= set(st["last_error"].get("fact_check", {})),
          st["last_error"].get("fact_check"))
    check("错误消息截断到 200 字符以内",
          len(st["last_error"]["fact_check"]["message"]) <= 200,
          len(st["last_error"]["fact_check"]["message"]))
    check("stats 是副本（改它不影响内部状态）",
          st["by_stage"] is not AH.validation_stats()["by_stage"])

    section("3] 首次 WARNING、后续 DEBUG（降级看得见但不刷屏）")
    reset_stats()
    with Isolated() as cap, Patch(AH, extract_facts=raiser()):
        AH.validate_response(REPLY, "", "val-log-1")
        warns = [r for r in cap.records if r.levelno == logging.WARNING and "fact_check" in r.getMessage()]
        check("首次失败有 WARNING", len(warns) == 1, [r.getMessage() for r in cap.records][:3])
        AH.validate_response(REPLY, "", "val-log-2")
        AH.validate_response(REPLY, "", "val-log-3")
        warns2 = [r for r in cap.records if r.levelno == logging.WARNING and "fact_check" in r.getMessage()]
        debugs = [r for r in cap.records if r.levelno == logging.DEBUG and "fact_check" in r.getMessage()]
        check("同类后续不再刷 WARNING", len(warns2) == 1, len(warns2))
        check("同类后续降级为 DEBUG 但仍记录", len(debugs) == 2, len(debugs))
        check("计数仍然每次都加", AH.validation_stats()["by_stage"]["fact_check"] == 3,
              AH.validation_stats()["by_stage"])

    section("4] 知识库检索：构建失败与检索异常都留痕")
    reset_stats()
    saved_mod = sys.modules.get("agent.rag_hybrid")
    saved_retriever, saved_failed = AH._HYBRID_RETRIEVER, AH._HYBRID_BUILD_FAILED
    broken = types.ModuleType("agent.rag_hybrid")
    broken.build_default_retriever = raiser("embedding 模型缺失（模拟）")
    sys.modules["agent.rag_hybrid"] = broken
    AH._HYBRID_RETRIEVER, AH._HYBRID_BUILD_FAILED = None, False
    try:
        with Isolated():
            check("构建失败 → 返回 None（不抛）", AH._get_hybrid_retriever() is None)
            check("构建失败计入 rag_build", AH.validation_stats()["by_stage"].get("rag_build") == 1,
                  AH.validation_stats()["by_stage"])
            check("构建失败标记 build_failed", AH.validation_stats()["rag"]["build_failed"] is True)
            AH._get_hybrid_retriever()
            check("永久降级：不每次都重试构建",
                  AH.validation_stats()["by_stage"].get("rag_build") == 1,
                  AH.validation_stats()["by_stage"])
            check("构建失败后检索直接返回空串", AH.get_rag_context("杭州西湖") == "")

            class BadRetriever:
                def retrieve(self, q, top_k=3):
                    raise RuntimeError("检索炸了（模拟）")

            with Patch(AH, _get_hybrid_retriever=lambda: BadRetriever()):
                check("检索异常 → 返回空串不抛", AH.get_rag_context("杭州西湖") == "")
                check("检索异常计入 rag_retrieve",
                      AH.validation_stats()["by_stage"].get("rag_retrieve") == 1,
                      AH.validation_stats()["by_stage"])
                check("检索异常计入 rag.retrieval_errors",
                      AH.validation_stats()["rag"]["retrieval_errors"] == 1,
                      AH.validation_stats()["rag"])
    finally:
        AH._HYBRID_RETRIEVER, AH._HYBRID_BUILD_FAILED = saved_retriever, saved_failed
        if saved_mod is not None:
            sys.modules["agent.rag_hybrid"] = saved_mod
        else:
            sys.modules.pop("agent.rag_hybrid", None)

    section("5] /api/admin/ops 的 validation 段与模块内计数一致")
    reset_stats()
    with Patch(AH, extract_facts=raiser()):
        AH.validate_response(REPLY, "", "val-ops")
    body = A.app.test_client().get("/api/admin/ops").get_json()
    v = body.get("validation") or {}
    check("ops 返回 validation 段", bool(v), list(body.keys()))
    check("runs 一致", v.get("runs") == 1, v)
    check("degraded_runs 一致", v.get("degraded_runs") == 1, v)
    check("by_stage 一致", v.get("by_stage", {}).get("fact_check") == 1, v.get("by_stage"))
    check("含 rag 子段", "rag" in v and "build_failed" in v["rag"], v.get("rag"))
    check("ops 段是副本（改不动内部状态）", v["by_stage"] is not AH.validation_stats()["by_stage"])
    page = A.app.test_client().get("/admin")
    check("/admin 面板 200 且含「降级与质检」区块",
          page.status_code == 200 and "降级与质检" in page.get_data(as_text=True), page.status_code)

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
