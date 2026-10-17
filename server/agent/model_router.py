# -*- coding: utf-8 -*-
"""
SmartTourism · 模型智能路由 + 成本核算
========================================
在多家国产模型（DeepSeek / 智谱 / 通义 / Moonshot / 本地 Ollama）之间，按任务类型、
上下文长度、推理需求、延迟/成本预算，自动挑选「性价比最优」的模型，并对每次调用做
成本估算与台账统计——让「热切换比价」从口号变成可量化、可回归的工程能力。

核心组件：
  - MODEL_CATALOG：真实 provider/model 目录 + 参考单价（CNY/百万 token，可配置）
  - ModelRouter.route(RouteRequest) → RouteDecision（首选 + 备选链 + 预估成本）
  - CostTracker：token 估算（中文启发式）+ 单次成本 + 累计台账 + 统计

设计原则：纯逻辑、零网络、零新依赖；单价与阈值全部可注入，便于随官方定价更新。
路由是「建议器」，最终仍由 app.call_llm 执行——通过 build_router_from_app 接线即可。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("agent.model_router")


# ==================== 模型目录（参考单价：CNY / 百万 token） ====================
# ⚠️ 单价为公开定价的参考值，可能随官方调整——生产前请以最新官方定价覆盖，或注入自定义 catalog。

@dataclass
class ModelSpec:
    key: str                 # "provider/model"
    provider: str
    model: str
    display_name: str
    tier: str                # economy / balanced / premium / reasoning / longctx / local
    input_price_per_1m: float
    output_price_per_1m: float
    max_context: int
    supports_tools: bool = True
    typical_latency_ms: int = 1500


def _spec(provider, model, display, tier, pin, pout, ctx, tools=True, lat=1500) -> ModelSpec:
    return ModelSpec(
        key=f"{provider}/{model}", provider=provider, model=model,
        display_name=display, tier=tier,
        input_price_per_1m=pin, output_price_per_1m=pout,
        max_context=ctx, supports_tools=tools, typical_latency_ms=lat,
    )


MODEL_CATALOG: Dict[str, ModelSpec] = {s.key: s for s in [
    # DeepSeek
    _spec("deepseek", "deepseek-chat", "DeepSeek Chat", "balanced", 2.0, 8.0, 64_000, lat=1600),
    _spec("deepseek", "deepseek-reasoner", "DeepSeek Reasoner", "reasoning", 4.0, 16.0, 64_000, lat=6000),
    # 智谱 GLM
    _spec("zhipu", "glm-4-flash", "GLM-4-Flash", "economy", 0.0, 0.0, 128_000, lat=900),
    _spec("zhipu", "glm-4-plus", "GLM-4-Plus", "premium", 50.0, 50.0, 128_000, lat=2200),
    _spec("zhipu", "glm-4-long", "GLM-4-Long", "longctx", 1.0, 1.0, 1_000_000, lat=2500),
    # 通义千问
    _spec("qwen", "qwen-turbo", "Qwen Turbo", "economy", 0.3, 0.6, 128_000, lat=800),
    _spec("qwen", "qwen-plus", "Qwen Plus", "balanced", 0.8, 2.0, 128_000, lat=1400),
    _spec("qwen", "qwen-max", "Qwen Max", "premium", 20.0, 60.0, 32_000, lat=2600),
    # Moonshot Kimi
    _spec("moonshot", "moonshot-v1-8k", "Kimi 8K", "economy", 12.0, 12.0, 8_000, lat=1200),
    _spec("moonshot", "moonshot-v1-32k", "Kimi 32K", "balanced", 24.0, 24.0, 32_000, lat=1600),
    _spec("moonshot", "moonshot-v1-128k", "Kimi 128K", "longctx", 60.0, 60.0, 128_000, lat=2400),
    # 本地 Ollama（零 API 成本）
    _spec("ollama", "qwen2.5:7b", "本地 Qwen2.5-7B", "local", 0.0, 0.0, 32_000, lat=3000),
    _spec("ollama", "qwen2.5:14b", "本地 Qwen2.5-14B", "local", 0.0, 0.0, 32_000, lat=6000),
]}


# ==================== 路由请求 / 决策 ====================

@dataclass
class RouteRequest:
    task: str = "consult"                 # consult/plan/marketing/cultural/matching/other
    est_input_tokens: int = 0
    est_output_tokens: int = 800
    need_reasoning: bool = False
    need_tools: bool = False
    max_latency_ms: Optional[int] = None
    cost_ceiling_yuan: Optional[float] = None
    prefer_local: bool = False
    available_providers: Optional[List[str]] = None  # 只在这些 provider 里选（如仅配了某几家 key）


@dataclass
class RouteDecision:
    spec: ModelSpec
    reason: str
    est_cost_yuan: float
    fallbacks: List[ModelSpec] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model": self.spec.key, "display": self.spec.display_name,
            "tier": self.spec.tier, "reason": self.reason,
            "est_cost_yuan": round(self.est_cost_yuan, 6),
            "fallbacks": [f.key for f in self.fallbacks],
        }


# 任务 → 期望档位（可被推理/长上下文/成本预算覆盖）
_TASK_TIER: Dict[str, List[str]] = {
    "other": ["economy"],
    "consult": ["economy", "balanced"],
    "matching": ["balanced"],
    "cultural": ["balanced", "premium"],
    "marketing": ["balanced", "premium"],
    "plan": ["balanced", "premium"],
}


class CostTracker:
    """token 估算 + 成本计算 + 累计台账。"""

    def __init__(self, catalog: Optional[Dict[str, ModelSpec]] = None) -> None:
        self.catalog = catalog or MODEL_CATALOG
        self.ledger: List[Dict[str, Any]] = []

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """中文启发式 token 估算：CJK 字≈1 token，其它字符≈0.3 token。

        无 tokenizer 依赖下的工程近似，够用于成本量级估算与路由决策。
        """
        if not text:
            return 0
        cjk = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
        other = len(text) - cjk
        return int(cjk + other * 0.3) + 1

    def estimate_cost(self, spec: ModelSpec, in_tokens: int, out_tokens: int) -> float:
        return (in_tokens / 1_000_000 * spec.input_price_per_1m
                + out_tokens / 1_000_000 * spec.output_price_per_1m)

    def record(self, spec: ModelSpec, in_tokens: int, out_tokens: int,
               latency_ms: Optional[float] = None, ok: bool = True) -> Dict[str, Any]:
        cost = self.estimate_cost(spec, in_tokens, out_tokens)
        entry = {
            "ts": time.time(), "model": spec.key, "provider": spec.provider,
            "in_tokens": in_tokens, "out_tokens": out_tokens,
            "cost_yuan": round(cost, 6), "latency_ms": latency_ms, "ok": ok,
        }
        self.ledger.append(entry)
        return entry

    def stats(self) -> Dict[str, Any]:
        total_cost = round(sum(e["cost_yuan"] for e in self.ledger), 6)
        total_calls = len(self.ledger)
        by_model: Dict[str, Dict[str, Any]] = {}
        for e in self.ledger:
            m = by_model.setdefault(e["model"], {"calls": 0, "cost_yuan": 0.0, "tokens": 0})
            m["calls"] += 1
            m["cost_yuan"] = round(m["cost_yuan"] + e["cost_yuan"], 6)
            m["tokens"] += e["in_tokens"] + e["out_tokens"]
        return {
            "total_calls": total_calls,
            "total_cost_yuan": total_cost,
            "avg_cost_yuan": round(total_cost / total_calls, 6) if total_calls else 0.0,
            "by_model": by_model,
        }


class ModelRouter:
    """按策略在 catalog 中挑选模型。"""

    def __init__(self, catalog: Optional[Dict[str, ModelSpec]] = None,
                 tracker: Optional[CostTracker] = None,
                 longctx_token_threshold: int = 24_000,
                 default_providers: Optional[List[str]] = None) -> None:
        self.catalog = catalog or MODEL_CATALOG
        self.tracker = tracker or CostTracker(self.catalog)
        self.longctx_threshold = longctx_token_threshold
        self.default_providers = default_providers

    def _candidates(self, req: RouteRequest) -> List[ModelSpec]:
        specs = list(self.catalog.values())
        providers = req.available_providers or self.default_providers
        if req.prefer_local:
            specs = [s for s in specs if s.tier == "local"] or specs
        elif providers:
            specs = [s for s in specs if s.provider in providers
                     and s.tier != "local"] or specs
        else:
            specs = [s for s in specs if s.tier != "local"]
        if req.need_tools:
            tool_ok = [s for s in specs if s.supports_tools]
            specs = tool_ok or specs
        if req.max_latency_ms:
            fast = [s for s in specs if s.typical_latency_ms <= req.max_latency_ms]
            specs = fast or specs
        return specs

    def route(self, req: RouteRequest) -> RouteDecision:
        cands = self._candidates(req)
        if not cands:
            # 兜底：目录里任选一个
            only = next(iter(self.catalog.values()))
            return RouteDecision(only, "无匹配候选，回退默认模型",
                                 self.tracker.estimate_cost(only, req.est_input_tokens, req.est_output_tokens))

        need_long = req.est_input_tokens > self.longctx_threshold
        if req.need_reasoning:
            want_tiers = ["reasoning", "premium", "balanced"]
        elif need_long:
            want_tiers = ["longctx", "premium", "balanced"]
        else:
            want_tiers = list(_TASK_TIER.get(req.task, ["balanced"]))

        def cost_of(s: ModelSpec) -> float:
            return self.tracker.estimate_cost(s, req.est_input_tokens, req.est_output_tokens)

        def tier_rank(s: ModelSpec) -> int:
            return want_tiers.index(s.tier) if s.tier in want_tiers else len(want_tiers)

        # 档位优先，同档内按「预估成本升序，其次延迟升序」
        pool = sorted(cands, key=lambda s: (tier_rank(s), cost_of(s), s.typical_latency_ms))

        # 成本预算是硬约束：优先在偏好档位内满足；若都不满足，则跨档取最低成本
        if req.cost_ceiling_yuan is not None:
            within = [s for s in pool if cost_of(s) <= req.cost_ceiling_yuan]
            pool = within or sorted(cands, key=lambda s: (cost_of(s), s.typical_latency_ms))

        chosen = pool[0]
        fallbacks = pool[1:4]

        reason = self._explain(req, chosen, need_long)
        if req.cost_ceiling_yuan is not None and cost_of(chosen) > req.cost_ceiling_yuan:
            reason += "（预算内无合适模型，已选最低成本）"
        return RouteDecision(chosen, reason, cost_of(chosen), fallbacks)

    def _explain(self, req: RouteRequest, s: ModelSpec, need_long: bool) -> str:
        bits = [f"任务={req.task}"]
        if req.need_reasoning:
            bits.append("需推理")
        if need_long:
            bits.append(f"长上下文({req.est_input_tokens}tok)")
        if req.cost_ceiling_yuan is not None:
            bits.append(f"成本≤{req.cost_ceiling_yuan}元")
        if req.max_latency_ms:
            bits.append(f"延迟≤{req.max_latency_ms}ms")
        bits.append(f"选中档位={s.tier}")
        return "，".join(bits)


# ==================== 与 app.py 接线（延迟导入，避免循环依赖） ====================

def build_router_from_app(only_configured: bool = True) -> ModelRouter:
    """读取 app.PROVIDERS / API_CONFIG，构造只含「已配置 provider」的路由器。

    only_configured=True 时，把 available_providers 限制为当前配置了 key 的家数，
    避免路由到无法调用的模型。
    """
    import os
    import sys
    _server = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _root = os.path.dirname(_server)
    for p in (_server, _root):
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        from app import PROVIDERS  # type: ignore
        providers = list(PROVIDERS.keys())
    except Exception as e:  # noqa: BLE001
        logger.debug("读取 app.PROVIDERS 失败，使用全量目录: %s", e)
        providers = None
    return ModelRouter(default_providers=providers if only_configured else None)
