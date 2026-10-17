# -*- coding: utf-8 -*-
"""
Tool / Function-Calling 层单元测试
==================================
不依赖网络与 API Key：
  - 用 MockLLM 按脚本返回 tool_calls / 最终答复，确定性验证 function-calling 循环；
  - 用真实的 LocalKnowledgePlugin（内置数据、无需 Key）验证「插件→工具」适配与实际执行。

运行：  python server/agent/test_tools.py
"""

from __future__ import annotations

import json
import os
import sys

# Windows 控制台默认 GBK，强制 UTF-8 以免 emoji/中文输出报错
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

# ---- 路径设置：让 agent.* 与 plugins.* 都可导入 ----
_HERE = os.path.dirname(os.path.abspath(__file__))       # .../server/agent
_SERVER = os.path.dirname(_HERE)                          # .../server
_ROOT = os.path.dirname(_SERVER)                          # 项目根
for p in (_SERVER, _ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from agent.tools import (  # noqa: E402
    Tool, ToolRegistry, run_function_calling,
    tool_from_plugin, build_registry_from_plugins,
)

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ✅ {name}")
    else:
        _FAIL += 1
        print(f"  ❌ {name}  {extra}")


# ==================== Mock LLM ====================

class MockLLM:
    """按脚本逐次返回预设响应，并记录每次调用收到的 messages/tools。"""

    def __init__(self, scripted):
        self.scripted = scripted
        self.calls = []

    def __call__(self, messages, stream=False, skill_name="", tools=None, tool_choice=None):
        self.calls.append({
            "messages": [dict(m) for m in messages],
            "tools": tools,
            "tool_choice": tool_choice,
            "skill_name": skill_name,
        })
        idx = len(self.calls) - 1
        return self.scripted[idx] if idx < len(self.scripted) else self.scripted[-1]


def _tool_call_resp(tool_name, arguments, call_id="call_1"):
    return {"choices": [{"message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": tool_name,
                                     "arguments": json.dumps(arguments, ensure_ascii=False)}}],
    }}]}


def _final_resp(content):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


# ==================== 测试用例 ====================

def test_basic_function_calling_loop():
    print("\n[1] 基础 function-calling 循环（mock 工具）")
    reg = ToolRegistry()
    reg.register_function(
        name="get_weather",
        description="查询城市天气",
        parameters={"type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"]},
        handler=lambda city: {"city": city, "weather": "晴", "temp_c": 25},
    )
    llm = MockLLM([
        _tool_call_resp("get_weather", {"city": "杭州"}),
        _final_resp("杭州今天晴，气温 25℃。"),
    ])

    out = run_function_calling(
        [{"role": "user", "content": "杭州天气怎么样"}], llm, reg)

    check("返回最终答复", out.get("content") == "杭州今天晴，气温 25℃。", str(out))
    check("工具调用次数=1", out.get("tool_calls") == 1, str(out.get("tool_calls")))
    check("trace 记录了 get_weather", len(out["trace"]) == 1 and out["trace"][0]["tool"] == "get_weather")
    check("往返轮数=2", out.get("iterations") == 2, str(out.get("iterations")))

    # 第二次调用 LLM 时，消息里应包含 assistant(tool_calls) 与 role=tool 结果
    second_msgs = llm.calls[1]["messages"]
    roles = [m.get("role") for m in second_msgs]
    check("回填了 role=tool 消息", "tool" in roles, str(roles))
    tool_msg = next(m for m in second_msgs if m.get("role") == "tool")
    check("tool 结果含真实数据", "晴" in tool_msg["content"] and tool_msg.get("tool_call_id") == "call_1",
          tool_msg["content"])
    check("首次调用带上了 tools schema", bool(llm.calls[0]["tools"]) and
          llm.calls[0]["tools"][0]["function"]["name"] == "get_weather")


def test_no_tools_registered():
    print("\n[2] 无工具时直接问答（不应带 tools 字段）")
    reg = ToolRegistry()
    llm = MockLLM([_final_resp("你好，我是智旅灵犀。")])
    out = run_function_calling([{"role": "user", "content": "你好"}], llm, reg)
    check("正常返回", out.get("content") == "你好，我是智旅灵犀。")
    check("未传 tools", llm.calls[0]["tools"] in (None, []), str(llm.calls[0]["tools"]))


def test_unknown_tool_and_bad_args():
    print("\n[3] 异常兜底：未知工具 / 非法参数 JSON")
    reg = ToolRegistry()
    reg.register_function("echo", "回显", {"type": "object", "properties": {}}, lambda: "ok")
    # 模型要求调用不存在的工具
    llm = MockLLM([
        {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "not_exist", "arguments": "{invalid json"}}]}}]},
        _final_resp("已处理"),
    ])
    out = run_function_calling([{"role": "user", "content": "x"}], llm, reg)
    check("未知工具不崩溃、循环继续到最终答复", out.get("content") == "已处理", str(out))
    check("trace 记录了失败调用", out["trace"][0]["tool"] == "not_exist")
    check("错误信息回填给模型", "未知工具" in llm.calls[1]["messages"][-1]["content"],
          llm.calls[1]["messages"][-1]["content"])


def test_max_iters_truncation():
    print("\n[4] 死循环保护：始终要求调用工具 → max_iters 截断")
    reg = ToolRegistry()
    reg.register_function("ping", "ping", {"type": "object", "properties": {}}, lambda: "pong")
    always_tool = _tool_call_resp("ping", {})
    llm = MockLLM([always_tool])  # 每次都返回同一个 tool_call
    out = run_function_calling([{"role": "user", "content": "loop"}], llm, reg, max_iters=3)
    check("触发截断标记", out.get("truncated") is True, str(out))
    check("往返轮数=max_iters", out.get("iterations") == 3, str(out.get("iterations")))


def test_real_plugin_as_tool():
    print("\n[5] 真实插件适配：LocalKnowledgePlugin（内置数据，无需 Key）")
    try:
        from plugins.travel_data import LocalKnowledgePlugin
    except Exception as e:  # noqa: BLE001
        check("导入 LocalKnowledgePlugin", False, str(e))
        return

    plugin = LocalKnowledgePlugin()
    tool = tool_from_plugin(plugin, only_if_configured=True)
    check("插件成功适配为 Tool", tool is not None)
    if tool is None:
        return
    check("工具名=local_knowledge", tool.name == "local_knowledge", tool.name)
    schema = tool.to_openai_schema()
    check("生成合法 OpenAI schema",
          schema["type"] == "function" and "city" in schema["function"]["parameters"]["properties"],
          json.dumps(schema, ensure_ascii=False)[:120])

    # 实际执行工具（离线内置数据）
    reg = ToolRegistry()
    reg.register(tool)
    result_str = reg.call("local_knowledge", {"city": "杭州", "type": "spots"})
    check("工具实际执行返回非空", bool(result_str) and "error" not in result_str[:20].lower(),
          result_str[:120])
    print(f"     ↳ 返回预览: {result_str[:100].replace(chr(10), ' ')}...")


def test_full_loop_with_real_plugin():
    print("\n[6] 端到端：mock 模型 → 调用真实 local_knowledge 工具 → 汇总")
    try:
        from plugins.travel_data import LocalKnowledgePlugin
    except Exception as e:  # noqa: BLE001
        check("导入 LocalKnowledgePlugin", False, str(e))
        return

    reg = build_registry_from_plugins(_FakePluginRegistry([LocalKnowledgePlugin()]))
    check("注册中心已装入插件工具", "local_knowledge" in reg.names(), str(reg.names()))

    llm = MockLLM([
        _tool_call_resp("local_knowledge", {"city": "成都", "type": "food"}),
        _final_resp("成都美食推荐：火锅、串串、担担面（数据来自本地知识库）。"),
    ])
    out = run_function_calling([{"role": "user", "content": "成都吃什么"}], llm, reg)
    check("端到端返回最终答复", "成都" in out.get("content", ""), str(out))
    check("工具确实被调用", out.get("tool_calls") == 1, str(out.get("tool_calls")))


class _FakePluginRegistry:
    """最小插件注册中心替身，仅提供 list_all()。"""
    def __init__(self, plugins):
        self._plugins = plugins

    def list_all(self):
        return self._plugins


if __name__ == "__main__":
    print("=" * 60)
    print("🧪 Tool / Function-Calling 层测试")
    print("=" * 60)
    test_basic_function_calling_loop()
    test_no_tools_registered()
    test_unknown_tool_and_bad_args()
    test_max_iters_truncation()
    test_real_plugin_as_tool()
    test_full_loop_with_real_plugin()
    print("\n" + "=" * 60)
    print(f"结果: {_PASS} 通过 / {_FAIL} 失败")
    print("=" * 60)
    sys.exit(1 if _FAIL else 0)
