# -*- coding: utf-8 -*-
"""
评估体系（Evaluation）单元测试
==============================
确定性验证：内置数据集完整性、四个规则指标、LLM-as-Judge 解析/归一化/失败降级、
报告聚合、JSON 存取、回归对比。judge/run 均用脚本化替身，无需 API Key。

运行：  python server/agent/test_evaluation.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVER = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_SERVER)
for p in (_SERVER, _ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from agent.evaluation import (  # noqa: E402
    EvalCase, Evaluator, EvalReport, build_default_cases,
    _metric_coverage, _metric_banned, _metric_source_tag, _metric_format,
    llm_judge, _extract_json, load_report,
)

_PASS = 0
_FAIL = 0


def check(name, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ✅ {name}")
    else:
        _FAIL += 1
        print(f"  ❌ {name}  {extra}")


# ==================== 数据集 ====================

def test_dataset():
    print("\n[内置数据集]")
    cases = build_default_cases()
    check("用例数量 ≥ 50", len(cases) >= 50, f"got {len(cases)}")
    ids = [c.id for c in cases]
    check("用例 id 唯一", len(ids) == len(set(ids)))
    cats = {c.category for c in cases}
    check("覆盖五类意图", {"consult", "plan", "marketing", "cultural", "matching"} <= cats, str(cats))
    check("营销用例默认带极限词禁用",
          all(c.must_not_contain for c in cases if c.category == "marketing"))


# ==================== 规则指标 ====================

def test_rule_metrics():
    print("\n[规则指标]")
    case = EvalCase(id="t", query="杭州景点", must_contain=["西湖", "灵隐寺"],
                    require_source_tag=False)

    cov = _metric_coverage(case, "推荐西湖和灵隐寺，都很美")
    check("要点全覆盖 → 1.0 通过", cov.score == 1.0 and cov.passed)

    cov2 = _metric_coverage(case, "只提了西湖")
    check("要点部分覆盖 → 0.5 不通过", cov2.score == 0.5 and not cov2.passed)

    ban = _metric_banned(EvalCase(id="b", query="x", must_not_contain=["最好", "第一"]),
                         "这是全国最好的景区")
    check("命中极限词 → 不通过", ban.score == 0.0 and not ban.passed)
    ban2 = _metric_banned(EvalCase(id="b", query="x", must_not_contain=["最好"]),
                          "值得一去的景区")
    check("无极限词 → 通过", ban2.passed)

    fmt_bad = _metric_format(case, "")
    check("空答复 → format 不通过", not fmt_bad.passed)
    fmt_err = _metric_format(case, '{"error": "超时"}')
    check("错误对象 → format 不通过", not fmt_err.passed)
    fmt_ok = _metric_format(case, "这是一段足够长的正常答复内容，结构清晰可用。")
    check("正常答复 → format 通过", fmt_ok.passed)


def test_source_tag_metric():
    print("\n[价格来源标注指标]")
    src_case = EvalCase(id="s", query="故宫门票", require_source_tag=True)
    # 无对冲的精确价格 → 不通过
    bad = _metric_source_tag(src_case, "故宫门票60元，直接买就行")
    check("无来源精确价格 → 不通过", not bad.passed, bad.detail)
    # 带对冲语言 → 通过
    good = _metric_source_tag(src_case, "故宫门票约60元（参考，以官方为准）")
    check("带对冲/来源 → 通过", good.passed, good.detail)
    # 无价格 → 通过（无需标注）
    noprice = _metric_source_tag(src_case, "故宫需要提前预约，建议早去")
    check("无价格 → 通过", noprice.passed)
    # 不要求标注的用例 → 恒通过
    noflag = _metric_source_tag(EvalCase(id="n", query="x", require_source_tag=False),
                                "门票60元")
    check("不要求标注 → 通过", noflag.passed)


# ==================== LLM-as-Judge ====================

def _judge_resp(content):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def test_extract_json():
    print("\n[JSON 提取]")
    check("纯 JSON", _extract_json('{"a":1}') == {"a": 1})
    check("包裹文本的 JSON", _extract_json('评分如下：{"a":2} 完毕') == {"a": 2})
    check("无 JSON → None", _extract_json("没有大括号") is None)


def test_llm_judge():
    print("\n[LLM-as-Judge]")
    case = EvalCase(id="j", query="杭州景点", reference="西湖")

    def good_judge(messages):
        # 校验 rubric 与四维被传入
        assert "relevance" in messages[0]["content"]
        return _judge_resp('{"relevance":5,"factuality":4,"anti_hallucination":5,"helpfulness":4,"reason":"切题"}')

    scores = llm_judge(case, "推荐西湖，信息可信", good_judge)
    check("5 分归一化为 1.0", scores["relevance"] == 1.0)
    check("4 分归一化为 0.75", scores["factuality"] == 0.75)
    check("1 分会归一化为 0.0",
          llm_judge(case, "x", lambda m: _judge_resp('{"relevance":1}'))["relevance"] == 0.0)
    check("保留 reason", scores.get("reason") == "切题")

    check("judge_fn=None → 跳过", llm_judge(case, "x", None) == {})

    def err_judge(messages):
        return {"error": "额度不足"}
    check("judge 报错 → 跳过", llm_judge(case, "x", err_judge) == {})

    def bad_json(messages):
        return _judge_resp("我觉得很好，但我不给 JSON")
    check("judge 非 JSON → 跳过", llm_judge(case, "x", bad_json) == {})


# ==================== 评估器 + 报告 ====================

def test_evaluator_and_report():
    print("\n[评估器与报告]")
    cases = [
        EvalCase(id="a", query="杭州景点", category="consult", must_contain=["西湖"]),
        EvalCase(id="b", query="写文案", category="marketing", must_not_contain=["最好"]),
    ]
    # run_fn：a 给好答复，b 给含极限词的差答复
    def run_fn(q):
        return "推荐西湖，风景优美值得一游。" if "杭州" in q else "这是最好的文案没有之一"

    ev = Evaluator(cases=cases, run_fn=run_fn)
    report = ev.evaluate_all()

    check("共 2 条结果", report.total == 2)
    check("a 通过", next(r for r in report.results if r.case_id == "a").passed)
    check("b 因极限词不通过", not next(r for r in report.results if r.case_id == "b").passed)
    check("通过率 = 0.5", report.pass_rate == 0.5, str(report.pass_rate))
    check("by_category 含两类", set(report.by_category().keys()) == {"consult", "marketing"})
    check("failed_cases 只含 b", [r.case_id for r in report.failed_cases()] == ["b"])


def test_evaluator_with_judge():
    print("\n[评估器接入 judge]")
    cases = [EvalCase(id="a", query="杭州景点", must_contain=["西湖"])]
    def run_fn(q):
        return "推荐西湖，信息可信、结构清晰。"
    def judge_fn(messages):
        return _judge_resp('{"relevance":5,"factuality":5,"anti_hallucination":5,"helpfulness":4}')
    ev = Evaluator(cases=cases, run_fn=run_fn, judge_fn=judge_fn)
    report = ev.evaluate_all()
    mj = report.mean_judge()
    check("judge 均分含四维", set(mj.keys()) == {"relevance", "factuality", "anti_hallucination", "helpfulness"})
    check("relevance 均分 = 1.0", mj["relevance"] == 1.0)
    check("helpfulness 均分 = 0.75", mj["helpfulness"] == 0.75, str(mj))


def test_evaluator_explicit_answer():
    print("\n[直接传入 answer 评估]")
    ev = Evaluator(cases=[EvalCase(id="x", query="q", must_contain=["要点"])])
    res = ev.evaluate_case(EvalCase(id="x", query="q", must_contain=["要点"]),
                           answer="这里包含了要点，且足够长可通过格式检查。")
    check("无需 run_fn 也能评估", res.passed)


# ==================== JSON 存取 + 回归 ====================

def test_json_and_regression():
    print("\n[JSON 存取与回归对比]")
    cases = [
        EvalCase(id="a", query="杭州景点", must_contain=["西湖"]),
        EvalCase(id="b", query="三亚行程", must_contain=["三亚", "第"]),
    ]
    tmp = tempfile.mkdtemp()
    try:
        # baseline：a 满分，b 部分分
        def base_run(q):
            return "推荐西湖。" if "杭州" in q else "只提到三亚。"
        base = Evaluator(cases=cases, run_fn=base_run).evaluate_all()
        path = os.path.join(tmp, "baseline.json")
        base.to_json(path)
        loaded = load_report(path)
        check("JSON 落盘可加载", "summary" in loaded and "cases" in loaded)
        check("summary 含通过率", "pass_rate" in loaded["summary"])

        # 新报告：a 满分，b 也满分（改进）
        def new_run(q):
            return "推荐西湖。" if "杭州" in q else "三亚行程：第一天海滩，第二天蜈支洲。"
        new = Evaluator(cases=cases, run_fn=new_run).evaluate_all()
        cmp = new.compare(loaded)
        check("检测到改进用例 b", "b" in cmp["improved"], str(cmp))
        check("无回退", not cmp["has_regression"])

        # 回退场景：新报告把 a 弄差
        def worse_run(q):
            return "不知道。" if "杭州" in q else "三亚行程：第一天海滩，第二天蜈支洲。"
        worse = Evaluator(cases=cases, run_fn=worse_run).evaluate_all()
        cmp2 = worse.compare(loaded)
        check("检测到回退用例 a", "a" in cmp2["regressed"], str(cmp2))
        check("has_regression=True", cmp2["has_regression"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_report_empty():
    print("\n[空报告不崩溃]")
    r = EvalReport(results=[])
    check("空报告通过率=0", r.pass_rate == 0.0)
    check("空报告均分=0", r.mean_overall == 0.0)
    check("空报告 judge={}", r.mean_judge() == {})


def main():
    print("=" * 60)
    print("Evaluation 单元测试")
    print("=" * 60)
    test_dataset()
    test_rule_metrics()
    test_source_tag_metric()
    test_extract_json()
    test_llm_judge()
    test_evaluator_and_report()
    test_evaluator_with_judge()
    test_evaluator_explicit_answer()
    test_json_and_regression()
    test_report_empty()
    print("\n" + "=" * 60)
    print(f"结果: {_PASS} passed, {_FAIL} failed")
    print("=" * 60)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
