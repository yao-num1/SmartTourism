# -*- coding: utf-8 -*-
"""
SmartTourism · Human-in-the-loop（人机协同确认）层
==================================================
用 LangGraph 的 interrupt + checkpointer，在关键动作前暂停、等人工确认后再继续。
对应项目里两个真实场景（见 README）：
  1. 下单前确认     —— 提交订单这种有实际后果的动作，先让用户核对商品/金额再执行；
  2. 行程修改二次确认 —— 用户改需求时先复述「将要怎么改」，确认后才落库，避免误改。

图结构：
    START → prepare(汇总待确认动作) → confirm(interrupt 暂停) ─┬─(approved)→ commit → END
                                                              └─(rejected)→ cancel → END

interrupt 会让图暂停并把「待确认信息」抛给调用方；调用方拿到后展示给用户，
再用 Command(resume={"approved": bool, ...}) 从断点恢复——checkpointer 保证状态不丢。

设计：commit_fn / cancel_fn 为可注入的副作用钩子（如写订单库/改行程库），
默认空实现，便于脱离数据库单测。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command

logger = logging.getLogger("agent.hitl")


# ==================== 状态 ====================

class HITLState(TypedDict, total=False):
    action: Dict[str, Any]      # 输入：待确认的动作 {type, ...}
    pending: Dict[str, Any]     # prepare 汇总出的可读确认信息
    decision: Dict[str, Any]    # 人工决策 {approved, note}
    status: str                 # committed / cancelled
    result: str                 # 结果文案


# ==================== 动作汇总 ====================

def summarize_action(action: Dict[str, Any]) -> Dict[str, Any]:
    """把原始动作汇总成给用户看的确认信息（纯函数，可单测）。"""
    atype = action.get("type", "order")

    if atype == "order":
        items = action.get("items")
        if not items:
            items = [{
                "name": action.get("product", "商品"),
                "price": action.get("price", 0),
                "qty": action.get("qty", 1),
            }]
        total = 0.0
        lines: List[str] = []
        for it in items:
            price = float(it.get("price", 0) or 0)
            qty = int(it.get("qty", 1) or 1)
            total += price * qty
            lines.append(f"- {it.get('name', '商品')} ×{qty}　¥{price:.2f}")
        return {
            "type": "order",
            "title": "下单确认",
            "question": f"确认提交订单？共 {len(items)} 项，合计 ¥{total:.2f}",
            "detail": lines,
            "total": round(total, 2),
        }

    if atype == "modify":
        changes = action.get("changes") or []
        lines = [f"- {c}" for c in changes] if changes else ["- （未提供具体修改项）"]
        return {
            "type": "modify",
            "title": "行程修改确认",
            "question": action.get("question", "确认按以下方式修改行程？"),
            "detail": lines,
            "target": action.get("target", ""),
        }

    # 兜底：通用确认
    return {
        "type": atype,
        "title": action.get("title", "操作确认"),
        "question": action.get("question", "确认执行该操作？"),
        "detail": action.get("detail", []),
    }


# ==================== 图构造 ====================

def create_hitl_graph(
    checkpointer: Any = None,
    commit_fn: Optional[Callable[[Dict[str, Any], Dict[str, Any]], Any]] = None,
    cancel_fn: Optional[Callable[[Dict[str, Any], Dict[str, Any]], Any]] = None,
):
    """构造带人工确认门的图。

    Args:
        checkpointer: 必需（interrupt 依赖它保存断点）。默认自动创建内存 checkpointer。
        commit_fn:    确认后执行的副作用 (action, decision) -> Any
        cancel_fn:    取消后执行的副作用 (action, decision) -> Any
    """
    if checkpointer is None:
        from agent.graph import get_checkpointer
        checkpointer = get_checkpointer()

    def prepare_node(state: HITLState) -> Dict[str, Any]:
        pending = summarize_action(state.get("action", {}))
        return {"pending": pending}

    def confirm_node(state: HITLState) -> Dict[str, Any]:
        # interrupt 暂停图，把待确认信息抛给调用方；恢复时返回人工决策
        decision = interrupt({
            "question": state["pending"]["question"],
            "title": state["pending"].get("title", "确认"),
            "detail": state["pending"].get("detail", []),
            "pending": state["pending"],
        })
        if not isinstance(decision, dict):
            decision = {"approved": bool(decision)}
        return {"decision": decision}

    def route_after_confirm(state: HITLState) -> str:
        return "commit" if (state.get("decision") or {}).get("approved") else "cancel"

    def commit_node(state: HITLState) -> Dict[str, Any]:
        if commit_fn is not None:
            try:
                commit_fn(state.get("action", {}), state.get("decision", {}))
            except Exception:  # noqa: BLE001
                logger.exception("commit_fn 执行失败")
        title = state["pending"].get("title", "操作")
        return {"status": "committed", "result": f"已确认并执行：{title}"}

    def cancel_node(state: HITLState) -> Dict[str, Any]:
        if cancel_fn is not None:
            try:
                cancel_fn(state.get("action", {}), state.get("decision", {}))
            except Exception:  # noqa: BLE001
                logger.exception("cancel_fn 执行失败")
        note = (state.get("decision") or {}).get("note", "")
        return {"status": "cancelled", "result": f"已取消{('：' + note) if note else ''}"}

    builder = StateGraph(HITLState)
    builder.add_node("prepare", prepare_node)
    builder.add_node("confirm", confirm_node)
    builder.add_node("commit", commit_node)
    builder.add_node("cancel", cancel_node)

    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "confirm")
    builder.add_conditional_edges("confirm", route_after_confirm,
                                  {"commit": "commit", "cancel": "cancel"})
    builder.add_edge("commit", END)
    builder.add_edge("cancel", END)

    return builder.compile(checkpointer=checkpointer)


# ==================== 运行辅助 ====================

def extract_interrupt(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从 invoke 返回值里取出 interrupt 抛出的待确认信息（兼容不同 langgraph 版本）。"""
    if not isinstance(result, dict):
        return None
    intr = result.get("__interrupt__")
    if not intr:
        return None
    first = intr[0] if isinstance(intr, (list, tuple)) else intr
    return getattr(first, "value", first)


def start_confirmation(
    graph: Any,
    action: Dict[str, Any],
    thread_id: str,
) -> Dict[str, Any]:
    """启动确认流程，运行到 interrupt 暂停。

    Returns:
        {"interrupted": bool, "prompt": <待确认信息 或 None>, "thread_id": str}
    """
    config = {"configurable": {"thread_id": thread_id}}
    result = graph.invoke({"action": action}, config=config)
    prompt = extract_interrupt(result)
    return {"interrupted": prompt is not None, "prompt": prompt, "thread_id": thread_id}


def resume_confirmation(
    graph: Any,
    thread_id: str,
    approved: bool,
    note: str = "",
) -> Dict[str, Any]:
    """从断点恢复：传入人工决策，图继续跑到 commit / cancel。

    Returns:
        {"status": "committed"/"cancelled", "result": str, "decision": dict}
    """
    config = {"configurable": {"thread_id": thread_id}}
    result = graph.invoke(Command(resume={"approved": approved, "note": note}), config=config)
    return {
        "status": result.get("status", ""),
        "result": result.get("result", ""),
        "decision": result.get("decision", {}),
    }
