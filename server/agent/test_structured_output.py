# -*- coding: utf-8 -*-
"""
结构化输出（Pydantic）单元测试
==============================
确定性验证：JSON 抽取、Schema 导出、prompt/tool 两种取数模式、校验失败自愈重试、
tool 模式失败降级、全部失败时优雅返回。全部用脚本化 mock LLM，无需 API Key。

运行：  python server/agent/test_structured_output.py
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

from agent.structured_output import (  # noqa: E402
    POI, Itinerary, MarketingPiece, extract_json, model_to_json_schema,
    build_structured_system_prompt, generate_structured,
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


class MockLLM:
    """按脚本顺序返回响应；记录每次调用的 tools/tool_choice。"""
    def __init__(self, scripted):
        self.scripted = scripted
        self.calls = []

    def __call__(self, messages, stream=False, skill_name="", tools=None, tool_choice=None):
        self.calls.append({"n_messages": len(messages), "tools": tools, "tool_choice": tool_choice})
        i = len(self.calls) - 1
        item = self.scripted[i] if i < len(self.scripted) else self.scripted[-1]
        return item() if callable(item) else item


def _content_resp(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def _tool_resp(arguments: dict, name="submit_structured_data"):
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}}]}


_GOOD_ITIN = {
    "destination": "杭州",
    "days": [{"day": 1, "theme": "西湖", "morning": "断桥", "pois": [
        {"name": "西湖", "category": "景点", "price_hint": "免费(参考)", "source": "本地知识库"}]}],
    "total_budget_hint": "约800元(参考)",
    "notes": ["提前预约"],
}


# ==================== extract_json ====================

def test_extract_json():
    print("\n[JSON 抽取]")
    check("```json 围栏", extract_json('```json\n{"a":1}\n```') == {"a": 1})
    check("裸 JSON", extract_json('{"a":2}') == {"a": 2})
    check("文本中嵌入 JSON", extract_json('好的：{"a":3} 完毕') == {"a": 3})
    check("数组", extract_json('[1,2,3]') == [1, 2, 3])
    check("非法 → None", extract_json("这不是 JSON") is None)
    check("空 → None", extract_json("") is None)


def test_schema_export():
    print("\n[Schema 导出]")
    s = model_to_json_schema(Itinerary)
    check("含 properties", "properties" in s)
    check("含 required", "destination" in s.get("required", []))
    prompt = build_structured_system_prompt(Itinerary, extra="控制在3天内")
    check("提示词含 Schema", "destination" in prompt)
    check("提示词含补充要求", "控制在3天内" in prompt)
    check("提示词要求只输出JSON", "只输出" in prompt)


# ==================== prompt 模式 ====================

def test_prompt_mode_ok():
    print("\n[prompt 模式：一次成功]")
    llm = MockLLM([_content_resp("```json\n" + json.dumps(_GOOD_ITIN, ensure_ascii=False) + "\n```")])
    res = generate_structured(Itinerary, llm, "帮我规划杭州1日游", mode="prompt")
    check("产出 Pydantic 实例", isinstance(res["data"], Itinerary))
    check("字段正确", res["data"].destination == "杭州" and len(res["data"].days) == 1)
    check("嵌套 POI 解析", isinstance(res["data"].days[0].pois[0], POI))
    check("attempts=1", res["attempts"] == 1)
    check("无致命错误", res["data"] is not None)


def test_prompt_mode_repair():
    print("\n[prompt 模式：校验失败自愈]")
    bad = {"destination": "杭州"}  # 缺 days（required）
    llm = MockLLM([
        _content_resp(json.dumps(bad, ensure_ascii=False)),          # 第1次不合法
        _content_resp(json.dumps(_GOOD_ITIN, ensure_ascii=False)),   # 修正后合法
    ])
    res = generate_structured(Itinerary, llm, "杭州1日游", mode="prompt", max_repair=2)
    check("自愈后成功", isinstance(res["data"], Itinerary))
    check("attempts=2", res["attempts"] == 2, str(res["attempts"]))
    check("记录了校验错误", any("校验失败" in e for e in res["errors"]), str(res["errors"]))
    check("第2次调用回喂了报错", llm.calls[1]["n_messages"] > llm.calls[0]["n_messages"])


def test_prompt_mode_all_fail():
    print("\n[prompt 模式：始终不合法 → 优雅返回]")
    llm = MockLLM([_content_resp("我就是不给JSON")])
    res = generate_structured(Itinerary, llm, "杭州", mode="prompt", max_repair=1)
    check("data 为 None", res["data"] is None)
    check("有错误信息", len(res["errors"]) > 0)
    check("尝试次数受控", res["attempts"] <= 2, str(res["attempts"]))


def test_llm_error():
    print("\n[LLM 返回 error]")
    llm = MockLLM([{"error": "额度不足"}])
    res = generate_structured(MarketingPiece, llm, "写文案", mode="prompt")
    check("data 为 None", res["data"] is None)
    check("错误被记录", any("额度不足" in e for e in res["errors"]))


# ==================== tool 模式 ====================

def test_tool_mode_ok():
    print("\n[tool 模式：function-calling 取数]")
    llm = MockLLM([_tool_resp(_GOOD_ITIN)])
    res = generate_structured(Itinerary, llm, "杭州1日游", mode="tool")
    check("产出实例", isinstance(res["data"], Itinerary))
    check("传入了 tools", llm.calls[0]["tools"] is not None)
    check("强制了 tool_choice", isinstance(llm.calls[0]["tool_choice"], dict))
    check("mode 仍为 tool", res["mode"] == "tool")


def test_tool_mode_degrade_to_prompt():
    print("\n[tool 模式：报错降级到 prompt]")
    llm = MockLLM([
        {"error": "该模型不支持 tools"},                          # tool 模式失败
        _content_resp(json.dumps(_GOOD_ITIN, ensure_ascii=False)),  # 降级 prompt 成功
    ])
    res = generate_structured(Itinerary, llm, "杭州1日游", mode="tool", max_repair=2)
    check("降级后成功", isinstance(res["data"], Itinerary))
    check("mode 变为 prompt", res["mode"] == "prompt", res["mode"])
    check("第二次不再带 tools", llm.calls[1]["tools"] is None)


def test_tool_mode_bad_args_repair():
    print("\n[tool 模式：参数不合法自愈]")
    llm = MockLLM([
        _tool_resp({"destination": "杭州"}),   # 缺 days
        _tool_resp(_GOOD_ITIN),
    ])
    res = generate_structured(Itinerary, llm, "杭州1日游", mode="tool", max_repair=2)
    check("自愈后成功", isinstance(res["data"], Itinerary))
    check("attempts=2", res["attempts"] == 2, str(res["attempts"]))


# ==================== 营销 Schema ====================

def test_marketing_schema():
    print("\n[营销结构化]")
    piece = {"platform": "小红书", "title": "西湖日落", "body": "傍晚的西湖……",
             "hashtags": ["#西湖", "#杭州"], "cta": "点击收藏"}
    llm = MockLLM([_content_resp(json.dumps(piece, ensure_ascii=False))])
    res = generate_structured(MarketingPiece, llm, "写条小红书", mode="prompt")
    check("营销实例产出", isinstance(res["data"], MarketingPiece))
    check("平台/标签正确", res["data"].platform == "小红书" and len(res["data"].hashtags) == 2)


def main():
    print("=" * 60)
    print("Structured Output 单元测试")
    print("=" * 60)
    test_extract_json()
    test_schema_export()
    test_prompt_mode_ok()
    test_prompt_mode_repair()
    test_prompt_mode_all_fail()
    test_llm_error()
    test_tool_mode_ok()
    test_tool_mode_degrade_to_prompt()
    test_tool_mode_bad_args_repair()
    test_marketing_schema()
    print("\n" + "=" * 60)
    print(f"结果: {_PASS} passed, {_FAIL} failed")
    print("=" * 60)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
