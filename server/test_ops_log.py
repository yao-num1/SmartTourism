"""server/ops_log.py 的离线单元测试（零依赖、无网络）。运行: python test_ops_log.py"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from ops_log import OpsStats, normalize_path, is_static_noise, render_admin_html  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}")


def test_normalize_path():
    check("uuid→:id", normalize_path("/api/sessions/666ec62-5ced-4b2b-a4d8-a56aebfda3d8")
          == "/api/sessions/:id" or ":id" in normalize_path("/api/x/12345678-1234-1234-1234-123456789abc"))
    check("纯数字→:id", normalize_path("/api/card/12345") == "/api/card/:id")
    check("普通段保留", normalize_path("/api/chat") == "/api/chat")
    check("多段混合", normalize_path("/api/a/123/b") == "/api/a/:id/b")


def test_is_static_noise():
    check("static前缀", is_static_noise("/static/images/x.jpg") is True)
    check("图片后缀", is_static_noise("/foo/bar.png") is True)
    check("css", is_static_noise("/x.css") is True)
    check("favicon", is_static_noise("/favicon.ico") is True)
    check("api不算静态", is_static_noise("/api/chat") is False)
    check("根路径不算静态", is_static_noise("/") is False)
    check("带query的图片", is_static_noise("/a.jpg?v=1") is True)


def test_record_basic():
    s = OpsStats()
    s.record("GET", "/api/health", 200, 10)
    s.record("GET", "/api/health", 200, 30)
    snap = s.snapshot()
    check("总请求=2", snap["total_requests"] == 2)
    check("无错误", snap["total_errors"] == 0 and snap["error_rate"] == 0.0)
    ep = snap["busiest"][0]
    check("端点聚合", ep["endpoint"] == "GET /api/health" and ep["count"] == 2)
    check("平均耗时=20", ep["avg_ms"] == 20.0)
    check("最大耗时=30", ep["max_ms"] == 30.0)
    check("状态码分布", snap["by_status"].get("200") == 2)


def test_record_errors():
    s = OpsStats()
    s.record("POST", "/api/chat", 500, 5)
    s.record("POST", "/api/chat", 200, 5)
    s.record("GET", "/api/x", 404, 5)   # 4xx 不算错误
    snap = s.snapshot()
    check("5xx计一次错误", snap["total_errors"] == 1)
    check("错误率=1/3", abs(snap["error_rate"] - round(1 / 3, 4)) < 1e-6)
    ep = [e for e in snap["busiest"] if e["endpoint"] == "POST /api/chat"][0]
    check("端点错误数=1", ep["errors"] == 1)
    check("404不计错误", snap["total_errors"] == 1)
    check("most_errors只含有错端点", all(e["errors"] for e in snap["most_errors"]))


def test_record_error_detail():
    s = OpsStats(max_errors=3)
    s.record_error("/api/plan", "boom" * 200, "req-1", 500, "RuntimeError")
    snap = s.snapshot()
    check("错误明细入列", len(snap["recent_errors"]) == 1)
    e = snap["recent_errors"][0]
    check("含request_id", e["request_id"] == "req-1")
    check("含类型", e["exc_type"] == "RuntimeError")
    check("消息被截断", len(e["message"]) <= OpsStats.MAX_ERROR_MSG)
    check("record_error不重复计total_errors", snap["total_errors"] == 0)
    # 环形缓冲上限
    for i in range(5):
        s.record_error("/api/x", f"e{i}", f"r{i}")
    check("环形缓冲上限=3", len(s.snapshot()["recent_errors"]) == 3)
    check("最近错误在前(倒序)", s.snapshot()["recent_errors"][0]["message"] == "e4")


def test_cardinality_guard():
    s = OpsStats()
    s.MAX_ENDPOINTS = 5
    for i in range(10):
        s.record("GET", f"/api/ep{i}", 200, 1)   # 不同路径 → 不同端点
    snap = s.snapshot()
    check("端点数不超过上限", len(snap["busiest"]) <= 5)
    check("总请求仍全计", snap["total_requests"] == 10)


def test_path_normalization_aggregates():
    s = OpsStats()
    s.record("GET", "/api/card/111", 200, 5)
    s.record("GET", "/api/card/222", 200, 5)
    snap = s.snapshot()
    check("动态id聚合成一个端点", len(snap["busiest"]) == 1)
    check("聚合键为:id", snap["busiest"][0]["endpoint"] == "GET /api/card/:id")
    check("聚合计数=2", snap["busiest"][0]["count"] == 2)


def test_static_skip():
    s = OpsStats()
    s.skip_static()
    s.skip_static()
    check("静态跳过计数", s.snapshot()["static_skipped"] == 2)


def test_admin_html():
    h = render_admin_html()
    check("面板含标题", "运维概览" in h)
    check("面板拉取ops接口", "/api/admin/ops" in h)
    check("面板自动刷新", "setInterval" in h)
    check("面板含「降级与质检」区块", "降级与质检" in h and 'id="degrade"' in h)
    check("降级区块读 database 段", "d.database" in h)
    check("降级区块读 validation 段", "d.validation" in h)
    check("降级区块读 degradation 段（文件日志）", "d.degradation" in h)
    check("知识库检索降级单独一行", "build_failed" in h)
    check("面板含 Agent 能力清单区块", "Agent 能力清单" in h and 'id="agents"' in h)
    check("能力清单读 agent_modules 段", "d.agent_modules" in h)
    check("状态词翻译成中文（未接线不藏）", "not_wired:'未接线'" in h.replace(" ", "")
          or "not_wired:'未接线'" in h)


def test_evaluate_alerts():
    # 超阈值：20 请求中 5 个 5xx → 错误率 0.25 > 0.05
    s = OpsStats()
    for i in range(15):
        s.record("GET", "/api/ok", 200, 5)
    for i in range(5):
        s.record("GET", "/api/boom", 500, 5)
    alert = s.evaluate_alerts(error_rate_threshold=0.05, min_requests=20, cooldown_s=300)
    check("超阈值触发告警", isinstance(alert, dict) and alert["type"] == "high_error_rate")
    check("告警携带窗口错误数", alert and alert["window_errors"] == 5 and alert["window_requests"] == 20)
    check("告警错误率正确", alert and abs(alert["error_rate"] - 0.25) < 1e-6)
    # 触发后窗口清零：无新流量时不再告警
    check("窗口已清零", s.evaluate_alerts(min_requests=20) is None)

    # 样本不足：仅 5 个请求（<min_requests）即使全错也不告警
    s2 = OpsStats()
    for i in range(5):
        s2.record("GET", "/api/x", 500, 1)
    check("样本不足不告警", s2.evaluate_alerts(min_requests=20) is None)

    # 低于阈值：20 个请求全 200
    s3 = OpsStats()
    for i in range(20):
        s3.record("GET", "/api/ok", 200, 1)
    check("健康窗口不告警", s3.evaluate_alerts(error_rate_threshold=0.05, min_requests=20) is None)

    # 冷却：连续两次超阈值，第二次因 cooldown 被抑制
    s4 = OpsStats()
    for i in range(20):
        s4.record("GET", "/api/e", 500, 1)
    a1 = s4.evaluate_alerts(min_requests=20, cooldown_s=300)
    for i in range(20):
        s4.record("GET", "/api/e", 500, 1)
    a2 = s4.evaluate_alerts(min_requests=20, cooldown_s=300)
    check("首次告警触发", a1 is not None)
    check("冷却期内抑制", a2 is None)


def test_persist_snapshot():
    import json
    import tempfile
    s = OpsStats()
    s.record("GET", "/api/ok", 200, 3)
    s.record("GET", "/api/bad", 500, 9)
    with tempfile.TemporaryDirectory() as d:
        path = s.persist_snapshot(d)
        check("返回落盘路径", bool(path) and path.endswith(".jsonl"))
        check("文件已创建", path is not None and os.path.exists(path))
        with open(path, encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        check("写入一行快照", len(lines) == 1)
        rec = json.loads(lines[0])
        check("快照含关键字段", all(k in rec for k in
              ("generated_at", "total_requests", "total_errors", "error_rate", "by_status")))
        check("快照计数正确", rec["total_requests"] == 2 and rec["total_errors"] == 1)
        # 再落盘一次应追加
        s.persist_snapshot(d)
        with open(path, encoding="utf-8") as f:
            lines2 = [ln for ln in f.read().splitlines() if ln.strip()]
        check("追加快照", len(lines2) == 2)


def main():
    print("=" * 50)
    print("ops_log 单元测试")
    test_normalize_path()
    test_is_static_noise()
    test_record_basic()
    test_record_errors()
    test_record_error_detail()
    test_cardinality_guard()
    test_path_normalization_aggregates()
    test_static_skip()
    test_admin_html()
    test_evaluate_alerts()
    test_persist_snapshot()
    print("=" * 50)
    print(f"结果: {_PASS} passed, {_FAIL} failed")
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
