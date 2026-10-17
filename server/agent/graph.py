# -*- coding: utf-8 -*-
"""
SmartTourism · LangGraph 编排层
================================
把 agent_engine.py 里的自研 ReAct 循环（IntentClassifier→Planner→Executor→Formatter）
重构为 LangGraph StateGraph，用「图 + 条件边」显式表达 Agent 的推理-行动循环：

    START → classify → agent ⇄ tools → finalize → END
                       │  ▲       │
                       │  └───────┘  (模型要求调用工具时循环)
                       └────────────→ finalize (模型给出最终答复)

节点职责：
  classify  意图分类 + 组装 system(含对应 Skill 提示词)/history/user 消息   —— 复用现有 IntentClassifier
  agent     带 tools 调 LLM，产出 assistant 消息（可能含 tool_calls）
  tools     执行 tool_calls（走 ToolRegistry），把结果以 role=tool 回填
  finalize  取最终答复、汇总 trace / 迭代次数等元数据

设计：call_llm_fn / tool_registry / classifier / build_system_prompt_fn 全部注入，
可脱离 app.py 用 mock 单测；checkpointer 可选，接入后支持断点续跑与 human-in-the-loop。
"""

from __future__ import annotations

import json
import logging
import operator
from typing import Annotated, Any, Callable, Dict, List, Optional, TypedDict

from langgraph.graph import StateGraph, START, END

logger = logging.getLogger("agent.graph")


# ==================== 状态定义 ====================

class AgentState(TypedDict, total=False):
    """图在节点间传递的状态。messages / trace 用 add reducer 累加，其余覆盖写。"""
    input: str                                   # 用户当前输入
    history: List[Dict[str, Any]]                # 多轮历史（可选）
    messages: Annotated[List[Dict[str, Any]], operator.add]  # OpenAI 格式消息流
    intent: str                                  # 分类得到的意图
    intent_params: Dict[str, Any]                # destination/days/budget/crowd
    iterations: int                              # agent 节点执行次数（循环护栏）
    reply: str                                   # 最终答复
    trace: Annotated[List[Dict[str, Any]], operator.add]     # 运行轨迹（可观测性）
    meta: Dict[str, Any]                         # 附加元数据


# 意图 → Skill 名映射（复用 agent_engine.Planner 的约定）
_INTENT_TO_SKILL = {
    "consult": "tourism-advisor",
    "plan": "tourism-advisor",
    "marketing": "tourism-marketing",
    "cultural": "tourism-marketing",
    "other": "",
}


# ==================== 默认组件（可被注入覆盖） ====================

def _default_classifier():
    """默认意图分类器：复用 agent_engine.IntentClassifier；不可用时退化为规则极简版。"""
    try:
        import os
        import sys
        _server = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _server not in sys.path:
            sys.path.insert(0, _server)
        from agent_engine import IntentClassifier  # type: ignore
        return IntentClassifier()
    except Exception as e:  # noqa: BLE001
        logger.warning("无法加载 IntentClassifier，使用极简分类: %s", e)

        class _Mini:
            def classify(self, text: str):
                class _R:
                    intent = type("I", (), {"value": "other"})()
                    destination = None
                    days = None
                    budget = None
                    crowd_type = None
                    confidence = 0.3
                return _R()
        return _Mini()


def _default_system_prompt(skill_name: str = "", extra: str = "") -> str:
    """默认 system 提示词（独立运行时的兜底；接入 app 时会注入 app.build_system_prompt）。"""
    base = (
        '你是文旅行业智能助手"智旅灵犀"。你可以调用工具获取实时数据（天气/POI/知识库/费用等），'
        "必须优先依据工具返回的真实数据作答，不得编造价格、时间、地址。"
    )
    if skill_name:
        base += f"\n当前激活 Skill：{skill_name}，请遵循其工作流程与输出模板。"
    return base


# ==================== 图构造 ====================

def create_agent_graph(
    call_llm_fn: Callable[..., Dict[str, Any]],
    tool_registry: Any,
    classifier: Any = None,
    build_system_prompt_fn: Optional[Callable[..., str]] = None,
    max_iters: int = 6,
    checkpointer: Any = None,
):
    """构造并编译 LangGraph StateGraph。

    Args:
        call_llm_fn:    LLM 调用函数，签名 call_llm_fn(messages, tools=..., tool_choice=..., skill_name=...)
        tool_registry:  agent.tools.ToolRegistry（提供 openai_schemas() 与 call()）
        classifier:     意图分类器（需有 .classify(text) 返回带 .intent.value/.destination... 的对象）
        build_system_prompt_fn: 生成 system 提示词，签名 (skill_name:str)->str；默认用内置兜底
        max_iters:      agent↔tools 最大循环轮数
        checkpointer:   LangGraph checkpointer（可选，接入后支持 thread_id 续跑 / 中断）

    Returns:
        compiled graph（用 graph.invoke(state) 运行）
    """
    classifier = classifier or _default_classifier()
    sys_prompt_fn = build_system_prompt_fn or (lambda skill_name="": _default_system_prompt(skill_name))

    # ---- 节点：意图分类 + 组装初始消息 ----
    def classify_node(state: AgentState) -> Dict[str, Any]:
        user_input = state.get("input", "")
        result = classifier.classify(user_input)
        intent_value = getattr(getattr(result, "intent", None), "value", "other")
        skill_name = _INTENT_TO_SKILL.get(intent_value, "")

        try:
            system_prompt = sys_prompt_fn(skill_name)
        except TypeError:
            # 兼容签名为 (skill_name, user_location) 的 app.build_system_prompt
            system_prompt = sys_prompt_fn(skill_name, None)  # type: ignore

        messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        messages.extend(state.get("history") or [])
        messages.append({"role": "user", "content": user_input})

        params = {
            "destination": getattr(result, "destination", None),
            "days": getattr(result, "days", None),
            "budget": getattr(result, "budget", None),
            "crowd_type": getattr(result, "crowd_type", None),
            "confidence": getattr(result, "confidence", 0.0),
        }
        return {
            "messages": messages,
            "intent": intent_value,
            "intent_params": params,
            "iterations": 0,
            "trace": [{"node": "classify", "intent": intent_value, "skill": skill_name}],
        }

    # ---- 节点：调用 LLM（带工具） ----
    def agent_node(state: AgentState) -> Dict[str, Any]:
        msgs = state.get("messages", [])
        schemas = tool_registry.openai_schemas() if tool_registry is not None else []
        skill_name = _INTENT_TO_SKILL.get(state.get("intent", ""), "")
        iteration = state.get("iterations", 0) + 1

        kwargs: Dict[str, Any] = {"skill_name": skill_name}
        if schemas:
            kwargs["tools"] = schemas
            kwargs["tool_choice"] = "auto"

        resp = call_llm_fn(msgs, **kwargs)

        # 带工具失败（如模型不支持 function calling）→ 降级为无工具再试一次
        if isinstance(resp, dict) and "error" in resp and schemas:
            logger.warning("agent 节点带工具调用失败，降级无工具: %s", resp.get("error"))
            resp = call_llm_fn(msgs, skill_name=skill_name)

        if not isinstance(resp, dict) or "error" in resp:
            err = resp.get("error") if isinstance(resp, dict) else "LLM 返回异常"
            return {
                "messages": [{"role": "assistant",
                              "content": f"抱歉，AI 服务暂时不可用：{err}"}],
                "iterations": iteration,
                "trace": [{"node": "agent", "iteration": iteration, "error": err}],
            }

        message = dict(resp["choices"][0]["message"])
        message.setdefault("role", "assistant")
        n_tools = len(message.get("tool_calls") or [])
        return {
            "messages": [message],
            "iterations": iteration,
            "trace": [{"node": "agent", "iteration": iteration, "tool_calls": n_tools}],
        }

    # ---- 条件边：决定去 tools 还是 finalize ----
    def route_after_agent(state: AgentState) -> str:
        last = state["messages"][-1]
        has_tool_calls = bool(last.get("tool_calls"))
        if has_tool_calls and state.get("iterations", 0) < max_iters:
            return "tools"
        return "finalize"

    # ---- 节点：执行工具 ----
    def tools_node(state: AgentState) -> Dict[str, Any]:
        last = state["messages"][-1]
        out_messages: List[Dict[str, Any]] = []
        trace: List[Dict[str, Any]] = []
        for tc in last.get("tool_calls", []) or []:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            raw = fn.get("arguments", "{}")
            try:
                args = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except json.JSONDecodeError:
                args = {}
            result = tool_registry.call(name, args)
            out_messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "name": name,
                "content": result,
            })
            trace.append({"node": "tools", "tool": name, "args": args,
                          "result_preview": result[:160]})
        return {"messages": out_messages, "trace": trace}

    # ---- 节点：收尾 ----
    def finalize_node(state: AgentState) -> Dict[str, Any]:
        last = state["messages"][-1]
        reply = last.get("content") or ""
        if not reply:
            reply = "抱歉，暂时无法生成答复，请稍后重试。"
        tool_total = sum(1 for m in state["messages"] if m.get("role") == "tool")
        return {
            "reply": reply,
            "meta": {
                "intent": state.get("intent"),
                "intent_params": state.get("intent_params"),
                "iterations": state.get("iterations", 0),
                "tool_calls": tool_total,
            },
            "trace": [{"node": "finalize", "iterations": state.get("iterations", 0),
                       "tool_calls": tool_total}],
        }

    # ---- 组图 ----
    builder = StateGraph(AgentState)
    builder.add_node("classify", classify_node)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", tools_node)
    builder.add_node("finalize", finalize_node)

    builder.add_edge(START, "classify")
    builder.add_edge("classify", "agent")
    builder.add_conditional_edges("agent", route_after_agent,
                                  {"tools": "tools", "finalize": "finalize"})
    builder.add_edge("tools", "agent")
    builder.add_edge("finalize", END)

    return builder.compile(checkpointer=checkpointer)


# ==================== 运行辅助 ====================

def get_memory_checkpointer():
    """返回一个内存 checkpointer（兼容不同 langgraph 版本的命名）。"""
    try:
        from langgraph.checkpoint.memory import MemorySaver  # type: ignore
        return MemorySaver()
    except Exception:  # noqa: BLE001
        try:
            from langgraph.checkpoint.memory import InMemorySaver  # type: ignore
            return InMemorySaver()
        except Exception as e:  # noqa: BLE001
            logger.warning("无法创建 checkpointer: %s", e)
            return None


def get_sqlite_checkpointer(db_path: Optional[str] = None):
    """返回 SQLite 持久化 checkpointer（服务重启后 HITL 断点可续跑）。

    依赖可选包 langgraph-checkpoint-sqlite —— 未安装就返回 None，调用方回退内存版，
    不引入新依赖、不抛异常。
    """
    try:
        import os
        import sqlite3
        from langgraph.checkpoint.sqlite import SqliteSaver  # type: ignore
        if not db_path:
            data_dir = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), '..', 'data')
            os.makedirs(data_dir, exist_ok=True)
            db_path = os.path.join(data_dir, 'agent_checkpoint.db')
        conn = sqlite3.connect(db_path, check_same_thread=False)
        logger.info("使用 SQLite checkpointer: %s", db_path)
        return SqliteSaver(conn)
    except Exception as e:  # noqa: BLE001
        logger.info("SQLite checkpointer 不可用（%s），回退内存版", e)
        return None


def get_checkpointer():
    """优先持久化 checkpointer，装了 langgraph-checkpoint-sqlite 才生效，否则内存版。"""
    return get_sqlite_checkpointer() or get_memory_checkpointer()


def run_agent(
    graph: Any,
    user_input: str,
    history: Optional[List[Dict[str, Any]]] = None,
    thread_id: Optional[str] = None,
    tracer: Any = None,
) -> Dict[str, Any]:
    """运行图并返回整洁结果 {reply, intent, iterations, tool_calls, trace}。

    传入 tracer（agent.tracing.Tracer）时，本次运行会被包成一个根 span，
    结束后自动 flush 落盘——追踪全程旁路，绝不影响主链路。
    """
    init_state: Dict[str, Any] = {
        "input": user_input,
        "history": history or [],
        "messages": [],
        "trace": [],
        "iterations": 0,
    }
    config = {"configurable": {"thread_id": thread_id}} if thread_id else None

    if tracer is not None:
        tracer.start_trace("agent.run", input=user_input)
        with tracer.span("graph.invoke", input={"thread_id": thread_id}) as _sp:
            try:
                final_state = graph.invoke(init_state, config=config) if config else graph.invoke(init_state)
                _sp.set_output({"reply_chars": len(final_state.get("reply", "") or "")})
            except Exception as _e:  # noqa: BLE001
                _sp.set_error(str(_e))
                tracer.flush()
                raise
        tracer.flush()
    else:
        final_state = graph.invoke(init_state, config=config) if config else graph.invoke(init_state)

    return {
        "reply": final_state.get("reply", ""),
        "intent": final_state.get("intent"),
        "iterations": final_state.get("iterations", 0),
        "tool_calls": final_state.get("meta", {}).get("tool_calls", 0),
        "trace": final_state.get("trace", []),
    }


# ==================== 与 app.py 接线（延迟导入，避免循环依赖） ====================

def build_graph_from_app(checkpointer: Any = None, only_configured_tools: bool = True,
                         tracer: Any = None):
    """用 app.py 的真实 call_llm、真实插件注册中心构造图。

    传入 tracer（agent.tracing.Tracer）时，call_llm 与工具注册中心会被自动包一层追踪，
    run_agent(graph, ..., tracer=tracer) 即可产出全链路 trace（本地 JSONL + 可选 LangFuse）。

    用法：
        from agent.graph import build_graph_from_app, run_agent
        graph = build_graph_from_app()
        print(run_agent(graph, "杭州有什么好玩的")["reply"])
    """
    import os
    import sys
    _server = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _root = os.path.dirname(_server)
    for p in (_server, _root):
        if p not in sys.path:
            sys.path.insert(0, p)

    from app import call_llm, registry, build_system_prompt  # type: ignore
    from agent.tools import build_registry_from_plugins  # type: ignore

    tool_registry = build_registry_from_plugins(registry, only_configured=only_configured_tools)

    if tracer is not None:
        from agent.tracing import wrap_call_llm, TracedToolRegistry  # type: ignore
        call_llm = wrap_call_llm(call_llm, tracer)
        tool_registry = TracedToolRegistry(tool_registry, tracer)

    def _sys_prompt(skill_name: str = "") -> str:
        # app.build_system_prompt 接收 Skill 名（如 tourism-advisor）
        try:
            return build_system_prompt(skill_name or None)
        except Exception:  # noqa: BLE001
            return _default_system_prompt(skill_name)

    return create_agent_graph(
        call_llm_fn=call_llm,
        tool_registry=tool_registry,
        build_system_prompt_fn=_sys_prompt,
        checkpointer=checkpointer,
    )
