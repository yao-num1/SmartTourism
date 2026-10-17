# -*- coding: utf-8 -*-
"""
模型路由 + 成本核算 单元测试
============================
确定性验证：token 估算、成本计算与台账、按任务/长上下文/推理/延迟/成本预算的路由选择、
provider 过滤、本地优先、备选链。全部离线，无需任何 API Key。

运行：  python server/agent/test_model_router.py
"""

from __future__ import annotations

import os
import sys

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

from agent.model_router import (  # noqa: E402
    MODEL_CATALOG, ModelRouter, CostTracker, RouteRequest,
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


def test_catalog():
    print("\n[模型目录]")
    check("目录非空", len(MODEL_CATALOG) >= 10)
    check("含真实 provider", {"deepseek", "zhipu", "qwen", "moonshot", "ollama"}
          <= {s.provider for s in MODEL_CATALOG.values()})
    check("key 唯一且格式正确",
          all(s.key == f"{s.provider}/{s.model}" for s in MODEL_CATALOG.values()))


def test_token_estimate():
    print("\n[token 估算]")
    check("空文本=0", CostTracker.estimate_tokens("") == 0)
    zh = CostTracker.estimate_tokens("杭州西湖")  # 4 CJK → ~5
    check("中文按字计", 4 <= zh <= 6, str(zh))
    en = CostTracker.estimate_tokens("abcdefghij")  # 10 ascii → ~4
    check("英文按 ~0.3/字", en < zh + 3 and en >= 3, str(en))
    check("长文本 token 更大",
          CostTracker.estimate_tokens("杭州" * 100) > CostTracker.estimate_tokens("杭州" * 10))


def test_cost_and_ledger():
    print("\n[成本计算与台账]")
    t = CostTracker()
    spec = MODEL_CATALOG["deepseek/deepseek-chat"]  # in 2/M, out 8/M
    # 1000 in + 1000 out → 0.002 + 0.008 = 0.01 元
    cost = t.estimate_cost(spec, 1000, 1000)
    check("成本计算正确", abs(cost - 0.01) < 1e-6, str(cost))

    free = MODEL_CATALOG["zhipu/glm-4-flash"]  # 0/0
    check("免费模型成本=0", t.estimate_cost(free, 5000, 2000) == 0.0)

    t.record(spec, 1000, 1000, latency_ms=1200, ok=True)
    t.record(spec, 2000, 500, latency_ms=900, ok=True)
    st = t.stats()
    check("台账记录 2 次", st["total_calls"] == 2)
    check("累计成本正确", abs(st["total_cost_yuan"] - (0.01 + 0.008)) < 1e-6, str(st["total_cost_yuan"]))
    check("按模型聚合", st["by_model"]["deepseek/deepseek-chat"]["calls"] == 2)
    check("平均成本", st["avg_cost_yuan"] > 0)


def test_route_by_task():
    print("\n[路由：按任务档位]")
    r = ModelRouter()
    d_other = r.route(RouteRequest(task="other"))
    check("闲聊→经济档", d_other.spec.tier == "economy", d_other.spec.key)

    d_plan = r.route(RouteRequest(task="plan", est_input_tokens=2000))
    check("行程→均衡/高端档", d_plan.spec.tier in ("balanced", "premium"), d_plan.spec.key)

    check("决策含原因", bool(d_other.reason))
    check("决策含预估成本字段", d_other.est_cost_yuan >= 0)


def test_route_longctx():
    print("\n[路由：长上下文]")
    r = ModelRouter(longctx_token_threshold=24_000)
    d = r.route(RouteRequest(task="plan", est_input_tokens=60_000))
    check("超阈值→长上下文/大窗口模型",
          d.spec.max_context >= 60_000 or d.spec.tier == "longctx", d.spec.key)
    check("原因标注长上下文", "长上下文" in d.reason, d.reason)


def test_route_reasoning():
    print("\n[路由：推理需求]")
    r = ModelRouter()
    d = r.route(RouteRequest(task="plan", need_reasoning=True))
    check("推理→reasoning/premium 档", d.spec.tier in ("reasoning", "premium"), d.spec.key)
    check("原因标注需推理", "需推理" in d.reason)


def test_route_cost_ceiling():
    print("\n[路由：成本预算]")
    r = ModelRouter()
    d = r.route(RouteRequest(task="plan", est_input_tokens=2000,
                             est_output_tokens=2000, cost_ceiling_yuan=0.001))
    check("预算内选中低成本模型", d.est_cost_yuan <= 0.001 + 1e-9, str(d.est_cost_yuan))


def test_route_latency():
    print("\n[路由：延迟预算]")
    r = ModelRouter()
    d = r.route(RouteRequest(task="consult", max_latency_ms=1000))
    check("低延迟约束被满足", d.spec.typical_latency_ms <= 1000, d.spec.key)


def test_route_provider_filter_and_local():
    print("\n[路由：provider 过滤 + 本地优先]")
    r = ModelRouter()
    d = r.route(RouteRequest(task="consult", available_providers=["qwen"]))
    check("限定 qwen", d.spec.provider == "qwen", d.spec.key)

    dl = r.route(RouteRequest(task="plan", prefer_local=True))
    check("本地优先→ollama", dl.spec.provider == "ollama", dl.spec.key)
    check("本地模型零成本", dl.est_cost_yuan == 0.0)

    # default_providers 生效
    r2 = ModelRouter(default_providers=["zhipu"])
    d2 = r2.route(RouteRequest(task="consult"))
    check("默认 provider 限制生效", d2.spec.provider == "zhipu", d2.spec.key)


def test_route_fallbacks():
    print("\n[路由：备选链]")
    r = ModelRouter()
    d = r.route(RouteRequest(task="plan", est_input_tokens=2000))
    check("提供备选模型", len(d.fallbacks) >= 1, str([f.key for f in d.fallbacks]))
    check("备选不含首选自身", d.spec.key not in [f.key for f in d.fallbacks])
    check("to_dict 可序列化", isinstance(d.to_dict(), dict) and "model" in d.to_dict())


def test_route_no_candidate_fallback():
    print("\n[路由：无候选兜底]")
    r = ModelRouter(catalog={"x/y": MODEL_CATALOG["qwen/qwen-turbo"]})
    # 要求一个不存在的 provider + 本地 → 应回退到目录内唯一模型而非崩溃
    d = r.route(RouteRequest(task="consult", available_providers=["nonexistent"]))
    check("无匹配时回退不崩溃", d.spec is not None)


def main():
    print("=" * 60)
    print("Model Router 单元测试")
    print("=" * 60)
    test_catalog()
    test_token_estimate()
    test_cost_and_ledger()
    test_route_by_task()
    test_route_longctx()
    test_route_reasoning()
    test_route_cost_ceiling()
    test_route_latency()
    test_route_provider_filter_and_local()
    test_route_fallbacks()
    test_route_no_candidate_fallback()
    print("\n" + "=" * 60)
    print(f"结果: {_PASS} passed, {_FAIL} failed")
    print("=" * 60)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
