# -*- coding: utf-8 -*-
"""
SmartTourism · 安全护栏（Guardrails）+ Prompt 注入防护
========================================================
在 Agent 主链路前后各加一道护栏：

  输入护栏（InputGuard）
    - Prompt 注入 / 越狱检测：忽略指令、角色覆盖、套取 system prompt、
      developer mode / DAN、编码绕过等中英文模式；
    - 风险分级（低/中/高）+ 命中原因，支持 allow / warn / block / sanitize 四种动作。

  输出护栏（OutputGuard）
    - 合规检查：广告法极限词、虚假承诺、绝对化医疗/收益用语；
    - 事实检查：复用 anti_hallucination（无来源精确价格、与插件数据冲突）；
    - 泄露检查：拒绝把 system prompt / 内部指令回吐给用户。

  GuardrailPipeline：把「输入检查 → 业务 run_fn → 输出检查」串起来，
  命中高危输入直接拒绝（不烧 token），输出违规则给出安全提示或净化。

设计原则：护栏是旁路增强，任何内部异常都降级为「放行」，绝不因护栏本身故障拖垮业务。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("agent.guardrails")


# ==================== 结果结构 ====================

@dataclass
class GuardResult:
    ok: bool = True
    action: str = "allow"          # allow / warn / block / sanitize
    risk: float = 0.0              # 0–1
    reasons: List[str] = field(default_factory=list)
    matched: List[str] = field(default_factory=list)
    sanitized_text: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok, "action": self.action, "risk": round(self.risk, 3),
            "reasons": self.reasons, "matched": self.matched,
        }


# ==================== 输入护栏 ====================

# (风险权重, 说明, 正则) —— 命中即累加风险
_INJECTION_PATTERNS: List[tuple] = [
    (0.9, "要求忽略/覆盖既有指令",
     r"(忽略|无视| forget| disregard|覆盖|重写)\s*(以上|之前|前面|上述|all|previous|above|prior)?\s*(的)?\s*(所有)?\s*(指令|提示|规则|设定|instructions?|prompts?|rules?)"),
    (0.9, "要求忽略既有指令(EN)",
     r"(ignore|disregard|forget)\s+(all\s+|any\s+)?(previous|prior|above|earlier|the|your)?\s*(instructions?|prompts?|rules?|directives?)"),
    (0.9, "角色覆盖 / 越权扮演",
     r"(你现在|从现在开始|接下来你)(是|扮演|充当|不再是)|假装你是|pretend\s+(to\s+be|you\s+are)|you\s+are\s+now"),
    (0.95, "套取 system prompt / 内部指令",
     r"(输出|打印|显示|告诉我|repeat|reveal|show|print)\s*(你的)?\s*(系统|初始|完整)?\s*(提示词|指令|prompt|system\s*prompt|instructions?)"),
    (0.85, "越狱模式关键词",
     r"developer\s+mode|开发者模式|越狱|jail\s*break|jailbreak|\bDAN\b|do\s+anything\s+now"),
    (0.5, "伪造系统/角色标签注入",
     r"^\s*(system|assistant|developer)\s*[:：]|\[/?\s*(system|inst)\s*\]|</?\s*system\s*>"),
    (0.6, "诱导绕过安全策略",
     r"(绕过|逃避|bypass|circumvent)\s*(安全|审查|限制|过滤|safety|filter|restriction)|不遵守|不要遵守|无需遵守|ignore\s+your\s+(safety|guidelines)"),
    (0.5, "要求泄露隐藏上下文",
     r"(你的|隐藏的?)\s*(初始|系统)?\s*(提示|设定|prompt)|你被设定为|what\s+are\s+your\s+instructions"),
]

# 命中后从输入中剥离（sanitize 模式）——只去除伪造标签本身，不吞掉用户正常内容
_SANITIZE_RE = re.compile(
    r"(system\s*[:：]|assistant\s*[:：]|developer\s*[:：]|</?system>|\[/?\s*(?:system|inst)\s*\])",
    flags=re.IGNORECASE,
)


class InputGuard:
    """Prompt 注入 / 越狱检测。"""

    def __init__(self, block_threshold: float = 0.6, warn_threshold: float = 0.35) -> None:
        self.block_threshold = block_threshold
        self.warn_threshold = warn_threshold
        self._compiled = [(w, d, re.compile(p, re.IGNORECASE)) for w, d, p in _INJECTION_PATTERNS]

    def check(self, text: str) -> GuardResult:
        if not text or not text.strip():
            return GuardResult(ok=True, action="allow", risk=0.0, reasons=["空输入"])
        risk = 0.0
        reasons: List[str] = []
        matched: List[str] = []
        for weight, desc, rx in self._compiled:
            m = rx.search(text)
            if m:
                risk = max(risk, weight)          # 取最高单项，避免堆叠误杀
                reasons.append(desc)
                matched.append(m.group(0)[:40])

        risk = min(risk, 1.0)
        if risk >= self.block_threshold:
            return GuardResult(ok=False, action="block", risk=risk,
                               reasons=reasons, matched=matched)
        if risk >= self.warn_threshold:
            return GuardResult(ok=True, action="warn", risk=risk,
                               reasons=reasons, matched=matched)
        return GuardResult(ok=True, action="allow", risk=risk, reasons=[], matched=[])

    def sanitize(self, text: str) -> str:
        """剥离伪造的系统/角色标签，降低注入面（不改变正常语义）。"""
        return _SANITIZE_RE.sub(" ", text or "").strip()


# ==================== 输出护栏 ====================

_LEAK_PATTERNS = [
    re.compile(r"(我的|本助手的?)\s*(系统)?\s*(提示词|system\s*prompt|初始指令)\s*(是|为|如下)", re.IGNORECASE),
    re.compile(r"you\s+are\s+(a|an)\s+.*(assistant|model).*instructed\s+to", re.IGNORECASE),
]


class OutputGuard:
    """输出合规 + 事实 + 泄露检查。"""

    def __init__(self, use_fact_check: bool = True) -> None:
        self.use_fact_check = use_fact_check
        try:
            from agent.evaluation import BANNED_ABSOLUTE_TERMS  # type: ignore
        except Exception:  # noqa: BLE001
            from evaluation import BANNED_ABSOLUTE_TERMS  # type: ignore
        self.banned_terms = list(BANNED_ABSOLUTE_TERMS)

    def check_compliance(self, answer: str) -> GuardResult:
        found = [t for t in self.banned_terms if t and t in answer]
        if found:
            return GuardResult(ok=False, action="warn", risk=0.6,
                               reasons=["含广告法极限词/绝对化用语"], matched=found)
        return GuardResult(ok=True, action="allow", risk=0.0, reasons=["合规"])

    def check_leak(self, answer: str) -> GuardResult:
        for rx in _LEAK_PATTERNS:
            m = rx.search(answer or "")
            if m:
                return GuardResult(ok=False, action="block", risk=0.9,
                                   reasons=["疑似泄露系统提示词"], matched=[m.group(0)[:40]])
        return GuardResult(ok=True, action="allow", risk=0.0)

    def check_fact(self, answer: str, plugin_context: str = "",
                   session_id: str = "") -> GuardResult:
        """复用 anti_hallucination 做事实校验（不可用时降级放行）。"""
        if not self.use_fact_check:
            return GuardResult(ok=True, action="allow", risk=0.0, reasons=["事实检查已关闭"])
        try:
            from anti_hallucination import validate_response  # type: ignore
            report = validate_response(answer, plugin_context=plugin_context, session_id=session_id)
            reasons: List[str] = []
            if report.plugin_conflicts:
                reasons.append("与插件实时数据冲突")
            if report.consistency_issues:
                reasons.append("与会话内既有事实不一致")
            fc = report.fact_check
            if fc and fc.has_unsourced_prices:
                reasons.append("存在无来源的精确价格（疑似编造）")
            if reasons:
                return GuardResult(ok=False, action="warn", risk=0.5, reasons=reasons)
            return GuardResult(ok=True, action="allow", risk=0.0, reasons=["事实校验通过"])
        except Exception as e:  # noqa: BLE001 — 反幻觉模块缺失时降级
            logger.debug("check_fact 降级放行: %s", e)
            return GuardResult(ok=True, action="allow", risk=0.0, reasons=["事实检查不可用，跳过"])

    def check(self, answer: str, plugin_context: str = "", session_id: str = "") -> GuardResult:
        """输出综合检查：泄露 > 合规 > 事实，返回风险最高的一条并汇总原因。"""
        leak = self.check_leak(answer)
        compliance = self.check_compliance(answer)
        fact = self.check_fact(answer, plugin_context, session_id)

        results = [leak, compliance, fact]
        reasons = [r for res in results for r in res.reasons if res.action != "allow"]
        matched = [m for res in results for m in res.matched]
        worst = max(results, key=lambda r: r.risk)
        action = worst.action if worst.action != "allow" else "allow"
        ok = worst.ok
        return GuardResult(ok=ok, action=action, risk=worst.risk,
                           reasons=reasons or ["输出检查通过"], matched=matched)


# ==================== 管线 ====================

_DEFAULT_REFUSAL = (
    "抱歉，我不能执行这个请求。我是文旅助手「智旅灵犀」，"
    "可以帮你做行程规划、景点/酒店/美食推荐、营销文案与文创设计。要不要换个旅游相关的问题？"
)


class GuardrailPipeline:
    """输入护栏 → 业务 run_fn → 输出护栏。"""

    def __init__(self, input_guard: Optional[InputGuard] = None,
                 output_guard: Optional[OutputGuard] = None,
                 block_input: bool = True, refusal: str = _DEFAULT_REFUSAL) -> None:
        self.input_guard = input_guard or InputGuard()
        self.output_guard = output_guard or OutputGuard()
        self.block_input = block_input
        self.refusal = refusal

    def run(self, user_input: str, run_fn: Callable[[str], str],
            plugin_context: str = "", session_id: str = "") -> Dict[str, Any]:
        """执行受护栏保护的一次问答。

        Returns:
            {reply, blocked, input_guard, output_guard}
        """
        # ---- 输入护栏 ----
        try:
            in_res = self.input_guard.check(user_input)
        except Exception as e:  # noqa: BLE001 — 护栏故障不得拖垮业务
            logger.warning("输入护栏异常，降级放行: %s", e)
            in_res = GuardResult(ok=True, action="allow", reasons=["护栏异常降级"])

        if self.block_input and in_res.action == "block":
            logger.warning("输入被拦截: %s (命中 %s)", user_input[:60], in_res.matched)
            return {"reply": self.refusal, "blocked": True,
                    "input_guard": in_res.to_dict(), "output_guard": None}

        # sanitize 模式：剥离伪造标签后再送业务
        effective_input = user_input
        if in_res.action == "warn":
            effective_input = self.input_guard.sanitize(user_input) or user_input

        # ---- 业务 ----
        answer = run_fn(effective_input)

        # ---- 输出护栏 ----
        try:
            out_res = self.output_guard.check(answer, plugin_context, session_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("输出护栏异常，降级放行: %s", e)
            out_res = GuardResult(ok=True, action="allow", reasons=["护栏异常降级"])

        if out_res.action == "block":
            # 泄露 system prompt 之类的硬违规：不透出原答复
            return {"reply": self.refusal, "blocked": True,
                    "input_guard": in_res.to_dict(), "output_guard": out_res.to_dict()}

        reply = answer
        if out_res.action == "warn" and out_res.matched:
            # 合规软违规：附加提示（极限词等），保留答复主体
            reply = f"{answer}\n\n> ⚠️ 合规提示：以上内容含「{'、'.join(out_res.matched[:3])}」等表述，正式发布前请替换为合规说法。"

        return {"reply": reply, "blocked": False,
                "input_guard": in_res.to_dict(), "output_guard": out_res.to_dict()}
