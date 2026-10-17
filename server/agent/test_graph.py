# -*- coding: utf-8 -*-
"""
LangGraph 编排层单元测试
========================
用真实 IntentClassifier（验证领域逻辑复用）+ MockLLM，跑通 StateGraph 全流程。
不依赖网络与 API Key。

运行：  python server/agent/test_graph.py
"""

from __future__ import annotations

import json
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

from agent.tools import ToolRegistry  # noqa: E402
from agent.graph import create_agent_graph, run_agent, get_memory_checkpointer  # noqa: E402

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


class MockLLM:
    def __init__(self, scripted):
        self.scripted = scripted
        self.calls = []

    def __call__(self, messages, stream=False, skill_name="", tools=None, tool_choice=None):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools,
                           "tool_choice": tool_choice, "skill_name": skill_name})
        idx = len(self.calls) - 1
        return self.scripted[idx] if idx < len(self.scripted) else self.scripted[-1]


def _tool_call_resp(name, args, call_id="call_1"):
    return {"choices": [{"message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}}]}


def _final(content):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _weather_registry():
    reg = ToolRegistry()
    reg.register_function(
        "get_weather", "查询城市天气",
        {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        lambda city: {"city": city, "weather": "晴", "temp_c": 25})
    return reg


def test_graph_with_tool():
    print("\n[1] 图流程：classify → agent → tools → agent → finalize（真实意图分类）")
    llm = MockLLM([_tool_call_resp("get_weather", {"city": "杭州"}), _final("杭州今天晴，25℃。")])
    graph = create_agent_graph(llm, _weather_registry())
    out = run_agent(graph, "杭州天气怎么样")

    check("最终答复正确", out["reply"] == "杭州今天晴，25℃。", str(out))
    check("意图被正确分类", out["intent"] in ("consult", "plan"), str(out["intent"]))
    check("工具调用次数=1", out["tool_calls"] == 1, str(out["tool_calls"]))
    check("agent 迭代=2", out["iterations"] == 2, str(out["iterations"]))
    nodes = [t["node"] for t in out["trace"]]
    check("轨迹经过 classify/agent/tools/finalize",
          nodes[0] == "classify" and "tools" in nodes and nodes[-1] == "finalize", str(nodes))
    # system 提示词应体现 Skill 注入
    sys_msg = llm.calls[0]["messages"][0]
    check("classify 组装了 system 提示词", sys_msg["role"] == "system" and len(sys_msg["content"]) > 0)


def test_graph_no_tool():
    print("\n[2] 图流程：无工具直接问答")
    llm = MockLLM([_final("你好，我是智旅灵犀。")])
    graph = create_agent_graph(llm, ToolRegistry())  # 空注册中心
    out = run_agent(graph, "你好")
    check("返回答复", out["reply"] == "你好，我是智旅灵犀。", str(out))
    check("未调用工具", out["tool_calls"] == 0)
    check("agent 迭代=1", out["iterations"] == 1, str(out["iterations"]))
    nodes = [t["node"] for t in out["trace"]]
    check("未经过 tools 节点", "tools" not in nodes, str(nodes))


def test_graph_loop_guard():
    print("\n[3] 循环护栏：模型反复要求调工具 → max_iters 截断")
    llm = MockLLM([_tool_call_resp("get_weather", {"city": "北京"})])  # 永远返回 tool_call
    graph = create_agent_graph(llm, _weather_registry(), max_iters=2)
    out = run_agent(graph, "北京天气")
    check("在 max_iters 内停止", out["iterations"] <= 2, str(out["iterations"]))
    check("图正常收尾(有 reply)", bool(out["reply"]), str(out))


def test_graph_llm_error():
    print("\n[4] LLM 报错时优雅降级")
    def err_llm(messages, **kw):
        return {"error": "额度不足"}
    graph = create_agent_graph(err_llm, _weather_registry())
    out = run_agent(graph, "杭州天气")
    check("错误被转成可读答复而非崩溃", "额度不足" in out["reply"] or "不可用" in out["reply"], str(out))


def test_graph_checkpointer():
    print("\n[5] checkpointer + thread_id 可运行")
    cp = get_memory_checkpointer()
    if cp is None:
        check("创建 checkpointer", False, "返回 None")
        return
    llm = MockLLM([_final("已记录。")])
    graph = create_agent_graph(llm, ToolRegistry(), checkpointer=cp)
    out = run_agent(graph, "记一下我想去三亚", thread_id="thread-abc")
    check("带 thread_id 正常运行", out["reply"] == "已记录。", str(out))


def test_checkpointer_selection():
    print("\n[6] checkpointer 选型：持久化优先，缺依赖时静默回退")
    from agent.graph import get_sqlite_checkpointer, get_checkpointer
    sqlite_cp = get_sqlite_checkpointer()
    try:
        import langgraph.checkpoint.sqlite  # noqa: F401
        installed = True
    except Exception:
        installed = False
    if installed:
        check("装了 sqlite 检查点包时返回持久化 checkpointer", sqlite_cp is not None)
    else:
        check("未装 sqlite 检查点包时返回 None 且不抛异常", sqlite_cp is None)
    cp = get_checkpointer()
    check("get_checkpointer 总能给出可用 checkpointer", cp is not None)
    if not installed and cp is not None:
        check("缺依赖时回退到内存版", type(cp).__name__ in ("MemorySaver", "InMemorySaver"),
              type(cp).__name__)


if __name__ == "__main__":
    print("=" * 60)
    print("🧪 LangGraph 编排层测试")
    print("=" * 60)
    test_graph_with_tool()
    test_graph_no_tool()
    test_graph_loop_guard()
    test_graph_llm_error()
    test_graph_checkpointer()
    test_checkpointer_selection()
    print("\n" + "=" * 60)
    print(f"结果: {_PASS} 通过 / {_FAIL} 失败")
    print("=" * 60)
    sys.exit(1 if _FAIL else 0)
