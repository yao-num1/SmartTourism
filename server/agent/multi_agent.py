# -*- coding: utf-8 -*-
"""
SmartTourism · Multi-Agent 多智能体协作层
==========================================
在 LangGraph 上实现 supervisor（规划-执行-审校）多智能体模式：

    START → planner → executor → critic ──(pass)──→ finalize → END
                          ▲          │
                          └─(revise)─┘   审校不通过则带反馈回炉重做（最多 max_revisions 次）

三个角色各司其职（都用同一个 call_llm_fn，靠不同 system 提示词区分）：
  planner   规划 Agent：把用户需求拆成结构化执行计划（要点/需查数据/输出结构）
  executor  执行 Agent：按计划产出草稿，可调用工具获取实时数据（复用 tools.run_function_calling）
  critic    审校 Agent：对照评审标准检查草稿（是否答到点上/是否基于工具真实数据/有无幻觉/是否完整），
                       返回严格 JSON {pass, feedback}；不通过则把 feedback 交回 executor 重写

价值：相比单 Agent 一次生成，加入独立「审校」角色可显著降低幻觉与漏答，是生产级 Agent 的常见范式。
"""

from __future__ import annotations

import json
import logging
import operator
import re
from typing import Annotated, Any, Callable, Dict, List, Optional, TypedDict

from langgraph.graph import StateGraph, START, END

from agent.tools import ToolRegistry, run_function_calling

logger = logging.getLogger("agent.multi_agent")


# ==================== 状态 ====================

class MultiAgentState(TypedDict, total=False):
    input: str
    plan: str
    draft: str
    critique: Dict[str, Any]
    revisions: int
    reply: str
    trace: Annotated[List[Dict[str, Any]], operator.add]
    meta: Dict[str, Any]


# ==================== 角色提示词（含 ROLE 标记，便于测试与追踪） ====================

_PLANNER_PROMPT = """[ROLE:planner]
你是文旅多智能体系统中的「规划 Agent」。把用户需求拆解成简洁的执行计划，供执行 Agent 使用。
只输出计划本身，不要寒暄。计划包含：
1. 需要产出的内容类型与结构（如：行程/文案/推荐清单）
2. 需要调用哪些工具获取实时数据（天气/POI/知识库/费用等）
3. 必须覆盖的要点与约束（目的地/天数/预算/人群等）
控制在 200 字以内。"""

_EXECUTOR_PROMPT = """[ROLE:executor]
你是文旅多智能体系统中的「执行 Agent」。严格按照规划 Agent 的计划产出最终内容。
要求：
- 若可用工具，请先调用工具获取真实数据，再基于返回数据作答，严禁编造价格/时间/地址；
- 输出结构清晰（善用小标题、列表、表格）；
- 若收到审校 Agent 的修改意见，必须针对性修正后再输出完整新版本。"""

_CRITIC_PROMPT = """[ROLE:critic]
你是文旅多智能体系统中的「审校 Agent」。对照下列标准检查执行 Agent 的草稿：
1. 相关性：是否直接回答了用户需求；
2. 事实性：价格/时间/地址等是否标注来源、是否存在编造嫌疑；
3. 完整性：计划要求的要点是否都覆盖；
4. 合规性：有无违规/极限词/虚假承诺。

只输出一行严格 JSON，不要任何多余文字：
{"pass": true 或 false, "feedback": "若不通过，给出具体、可执行的修改意见；通过则留空"}"""


# ==================== 工具函数 ====================

def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从模型输出中稳健地抽取第一个 JSON 对象。"""
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            return None
    return None


def _content_of(resp: Any) -> str:
    """从 call_llm 返回中取文本内容；出错则返回错误说明。"""
    if not isinstance(resp, dict):
        return ""
    if "error" in resp:
        return f"（上游模型调用失败：{resp['error']}）"
    try:
        return resp["choices"][0]["message"].get("content", "") or ""
    except (KeyError, IndexError, TypeError):
        return ""


# ==================== 图构造 ====================

def create_multi_agent_graph(
    call_llm_fn: Callable[..., Dict[str, Any]],
    tool_registry: Optional[Any] = None,
    max_revisions: int = 2,
    max_tool_iters: int = 5,
    checkpointer: Any = None,
    planner_prompt: str = _PLANNER_PROMPT,
    executor_prompt: str = _EXECUTOR_PROMPT,
    critic_prompt: str = _CRITIC_PROMPT,
):
    """构造并编译「规划-执行-审校」多智能体图。

    Args:
        call_llm_fn:    LLM 调用函数（签名同 app.call_llm，支持 tools/tool_choice/skill_name）
        tool_registry:  执行 Agent 可用的工具注册中心（可为 None → 执行 Agent 不用工具）
        max_revisions:  审校不通过时最多回炉次数
        max_tool_iters: 执行 Agent 内部 function-calling 最大轮数
        checkpointer:   可选 LangGraph checkpointer
    """
    registry = tool_registry if tool_registry is not None else ToolRegistry()

    def planner_node(state: MultiAgentState) -> Dict[str, Any]:
        user_input = state.get("input", "")
        resp = call_llm_fn(
            [{"role": "system", "content": planner_prompt},
             {"role": "user", "content": user_input}],
            skill_name="plan",
        )
        plan = _content_of(resp).strip() or "（规划失败，按通用方式直接产出）"
        return {"plan": plan, "revisions": 0,
                "trace": [{"node": "planner", "plan_preview": plan[:120]}]}

    def executor_node(state: MultiAgentState) -> Dict[str, Any]:
        user_input = state.get("input", "")
        plan = state.get("plan", "")
        # 第二轮起带上审校意见，要求针对性修正
        prev_critique = state.get("critique") or {}
        feedback = prev_critique.get("feedback", "") if not prev_critique.get("pass", True) else ""

        sys_parts = [executor_prompt, f"\n\n## 规划 Agent 的计划\n{plan}"]
        if feedback:
            sys_parts.append(f"\n\n## 审校 Agent 的修改意见（必须逐条修正）\n{feedback}")

        messages = [
            {"role": "system", "content": "\n".join(sys_parts)},
            {"role": "user", "content": user_input},
        ]
        result = run_function_calling(
            messages, call_llm_fn, registry,
            max_iters=max_tool_iters, skill_name="",
        )
        draft = result.get("content") or result.get("error") or "（执行失败）"
        # 首次执行 revisions=0；已有草稿说明是回炉重写 → +1
        revisions = state.get("revisions", 0) + (1 if state.get("draft") else 0)
        return {
            "draft": draft,
            "revisions": revisions,
            "trace": [{"node": "executor", "revision": revisions,
                       "tool_calls": result.get("tool_calls", 0)}],
        }

    def critic_node(state: MultiAgentState) -> Dict[str, Any]:
        draft = state.get("draft", "")
        user_input = state.get("input", "")
        resp = call_llm_fn(
            [{"role": "system", "content": critic_prompt},
             {"role": "user", "content": f"用户需求：{user_input}\n\n执行 Agent 草稿：\n{draft}"}],
            skill_name="",
        )
        raw = _content_of(resp)
        parsed = _extract_json(raw)
        if parsed is None:
            # 解析失败：保守放行，避免死循环
            logger.warning("审校输出无法解析为 JSON，保守放行: %r", raw[:120])
            critique = {"pass": True, "feedback": "", "parse_error": True}
        else:
            critique = {
                "pass": bool(parsed.get("pass", True)),
                "feedback": str(parsed.get("feedback", "") or ""),
            }
        return {"critique": critique,
                "trace": [{"node": "critic", "pass": critique["pass"],
                           "feedback_preview": critique["feedback"][:120]}]}

    def route_after_critic(state: MultiAgentState) -> str:
        critique = state.get("critique") or {}
        if critique.get("pass", True):
            return "finalize"
        if state.get("revisions", 0) >= max_revisions:
            return "finalize"  # 达到回炉上限，强制收尾（避免无限循环）
        return "executor"

    def finalize_node(state: MultiAgentState) -> Dict[str, Any]:
        critique = state.get("critique") or {}
        reply = state.get("draft", "") or "抱歉，未能生成内容。"
        forced = (not critique.get("pass", True)) and state.get("revisions", 0) >= max_revisions
        return {
            "reply": reply,
            "meta": {
                "revisions": state.get("revisions", 0),
                "passed": bool(critique.get("pass", True)),
                "forced_finalize": forced,
                "last_feedback": critique.get("feedback", ""),
            },
            "trace": [{"node": "finalize", "revisions": state.get("revisions", 0),
                       "passed": bool(critique.get("pass", True)), "forced": forced}],
        }

    builder = StateGraph(MultiAgentState)
    builder.add_node("planner", planner_node)
    builder.add_node("executor", executor_node)
    builder.add_node("critic", critic_node)
    builder.add_node("finalize", finalize_node)

    builder.add_edge(START, "planner")
    builder.add_edge("planner", "executor")
    builder.add_edge("executor", "critic")
    builder.add_conditional_edges("critic", route_after_critic,
                                  {"executor": "executor", "finalize": "finalize"})
    builder.add_edge("finalize", END)

    return builder.compile(checkpointer=checkpointer)


def run_multi_agent(
    graph: Any,
    user_input: str,
    thread_id: Optional[str] = None,
) -> Dict[str, Any]:
    """运行多智能体图，返回 {reply, revisions, passed, trace}。"""
    init: Dict[str, Any] = {"input": user_input, "trace": [], "revisions": 0}
    config = {"configurable": {"thread_id": thread_id}} if thread_id else None
    final = graph.invoke(init, config=config) if config else graph.invoke(init)
    meta = final.get("meta", {})
    return {
        "reply": final.get("reply", ""),
        "revisions": meta.get("revisions", final.get("revisions", 0)),
        "passed": meta.get("passed", True),
        "forced_finalize": meta.get("forced_finalize", False),
        "trace": final.get("trace", []),
    }
