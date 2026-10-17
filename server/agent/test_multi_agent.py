# -*- coding: utf-8 -*-
"""
Multi-Agent 多智能体层单元测试
==============================
用按 [ROLE:*] 标记分发的 MockLLM，验证「规划-执行-审校-回炉」闭环。
不依赖网络与 API Key。

运行：  python server/agent/test_multi_agent.py
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
from agent.multi_agent import create_multi_agent_graph, run_multi_agent  # noqa: E402

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


def _txt(content):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _toolcall(name, args, cid="c1"):
    return {"choices": [{"message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": cid, "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}}]}


class RoleMock:
    """按 system 提示词里的 [ROLE:*] 标记分发到不同脚本，并各自计数。"""

    def __init__(self, planner=None, executor=None, critic=None):
        self.scripts = {"planner": planner or [_txt("(空计划)")],
                        "executor": executor or [_txt("(空草稿)")],
                        "critic": critic or [_txt('{"pass": true, "feedback": ""}')]}
        self.counts = {"planner": 0, "executor": 0, "critic": 0}
        self.roles_called = []

    def __call__(self, messages, stream=False, skill_name="", tools=None, tool_choice=None):
        sys_content = messages[0]["content"] if messages else ""
        if "[ROLE:planner]" in sys_content:
            role = "planner"
        elif "[ROLE:critic]" in sys_content:
            role = "critic"
        else:
            role = "executor"
        self.roles_called.append(role)
        idx = self.counts[role]
        self.counts[role] += 1
        script = self.scripts[role]
        return script[idx] if idx < len(script) else script[-1]


def test_revise_then_pass():
    print("\n[1] 审校不通过 → 回炉重写 → 通过")
    mock = RoleMock(
        planner=[_txt("计划：产出杭州3日亲子游行程；调用天气/POI；覆盖每日主题+预算+亲子要点")],
        executor=[_txt("草稿V1：杭州3日游（未含预算）"),
                  _txt("草稿V2：杭州3日游（含人均预算 1500 元、亲子要点）")],
        critic=[_txt('{"pass": false, "feedback": "缺少预算区间，请补充人均预算"}'),
                _txt('{"pass": true, "feedback": ""}')],
    )
    graph = create_multi_agent_graph(mock, max_revisions=2)
    out = run_multi_agent(graph, "帮我规划杭州3日亲子游")

    check("采用修订后的 V2 草稿", out["reply"] == "草稿V2：杭州3日游（含人均预算 1500 元、亲子要点）", str(out["reply"]))
    check("回炉次数=1", out["revisions"] == 1, str(out["revisions"]))
    check("最终审校通过", out["passed"] is True, str(out["passed"]))
    nodes = [t["node"] for t in out["trace"]]
    check("轨迹为 planner→executor→critic→executor→critic→finalize",
          nodes == ["planner", "executor", "critic", "executor", "critic", "finalize"], str(nodes))


def test_pass_first_try():
    print("\n[2] 一次通过（无回炉）")
    mock = RoleMock(planner=[_txt("计划：三亚攻略")],
                    executor=[_txt("草稿：三亚5日游完整攻略")],
                    critic=[_txt('{"pass": true, "feedback": ""}')])
    graph = create_multi_agent_graph(mock)
    out = run_multi_agent(graph, "三亚怎么玩")
    check("返回草稿", out["reply"] == "草稿：三亚5日游完整攻略", str(out["reply"]))
    check("回炉次数=0", out["revisions"] == 0, str(out["revisions"]))
    check("executor 只调用一次", mock.counts["executor"] == 1, str(mock.counts))


def test_forced_finalize_on_max_revisions():
    print("\n[3] 审校始终不通过 → 达到上限强制收尾（防死循环）")
    mock = RoleMock(planner=[_txt("p")],
                    executor=[_txt("v1"), _txt("v2"), _txt("v3")],
                    critic=[_txt('{"pass": false, "feedback": "还需改进"}')])  # 永远 fail
    graph = create_multi_agent_graph(mock, max_revisions=1)
    out = run_multi_agent(graph, "北京游")
    check("强制收尾标记", out["forced_finalize"] is True, str(out))
    check("未通过", out["passed"] is False, str(out["passed"]))
    check("回炉次数=上限", out["revisions"] == 1, str(out["revisions"]))
    check("critic 调用次数受控(<=2)", mock.counts["critic"] <= 2, str(mock.counts))


def test_executor_uses_tools():
    print("\n[4] 执行 Agent 调用工具获取真实数据")
    reg = ToolRegistry()
    reg.register_function("get_weather", "查询天气",
                          {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
                          lambda city: {"city": city, "weather": "晴", "temp_c": 25})
    mock = RoleMock(
        planner=[_txt("计划：先查杭州天气再推荐")],
        executor=[_toolcall("get_weather", {"city": "杭州"}),
                  _txt("杭州今天晴 25℃，推荐西湖漫步。")],
        critic=[_txt('{"pass": true, "feedback": ""}')],
    )
    graph = create_multi_agent_graph(mock, tool_registry=reg)
    out = run_multi_agent(graph, "杭州天气和游玩建议")
    check("返回基于工具数据的答复", out["reply"] == "杭州今天晴 25℃，推荐西湖漫步。", str(out["reply"]))
    exec_tool_calls = [t.get("tool_calls", 0) for t in out["trace"] if t["node"] == "executor"]
    check("执行 Agent 确实调用了工具", any(c >= 1 for c in exec_tool_calls), str(exec_tool_calls))


def test_critic_json_parse_fallback():
    print("\n[5] 审校输出非 JSON → 保守放行不死循环")
    mock = RoleMock(planner=[_txt("p")], executor=[_txt("草稿内容")],
                    critic=[_txt("我觉得还行，通过！")])  # 非 JSON
    graph = create_multi_agent_graph(mock, max_revisions=2)
    out = run_multi_agent(graph, "随便问问")
    check("非 JSON 审校被保守放行", out["passed"] is True, str(out))
    check("正常返回草稿", out["reply"] == "草稿内容", str(out["reply"]))


if __name__ == "__main__":
    print("=" * 60)
    print("🧪 Multi-Agent 多智能体层测试")
    print("=" * 60)
    test_revise_then_pass()
    test_pass_first_try()
    test_forced_finalize_on_max_revisions()
    test_executor_uses_tools()
    test_critic_json_parse_fallback()
    print("\n" + "=" * 60)
    print(f"结果: {_PASS} 通过 / {_FAIL} 失败")
    print("=" * 60)
    sys.exit(1 if _FAIL else 0)
