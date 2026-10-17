# -*- coding: utf-8 -*-
"""
SmartTourism · 结构化输出（Pydantic Schema + 校验/自愈）
=========================================================
把 LLM 的自由文本产出，约束成可校验、可入库、可渲染前端组件的结构化对象
（行程 / POI / 营销文案 / 酒店推荐 …）。两种取数模式：

  - prompt 模式：把 JSON Schema 写进提示词，要求「只输出 JSON」，再抽取 + Pydantic 校验；
  - tool 模式：用 Function Calling 把 Schema 作为工具参数，强制模型以 tool_call 形式回填
    （OpenAI 兼容接口最稳的结构化取数方式）。

任一模式校验失败都会触发「自愈重试」：把 Pydantic 的报错回喂给模型让它改，最多 max_repair 次。
最终返回 {data, raw, errors, attempts}，data 为 Pydantic 实例或 None（全部失败时）。

设计原则：与 app.call_llm 解耦（注入 call_llm_fn），纯离线可测，失败可降级不崩溃。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Type

from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger("agent.structured_output")


# ==================== 领域 Schema ====================

class POI(BaseModel):
    """行程中的兴趣点（景点/餐厅/酒店/体验）。价格必须带来源标注。"""
    name: str = Field(..., description="名称")
    category: str = Field("", description="类型：景点/餐厅/酒店/体验/交通")
    reason: str = Field("", description="推荐理由")
    price_hint: str = Field("", description="价格/费用，须含来源或对冲，如「约60元(参考,以官方为准)」")
    source: str = Field("AI推断", description="数据来源：本地知识库/高德/美团/AI推断")


class ItineraryDay(BaseModel):
    """单日行程。"""
    day: int = Field(..., description="第几天，从1开始")
    theme: str = Field("", description="当日主题")
    morning: str = Field("", description="上午安排")
    afternoon: str = Field("", description="下午安排")
    evening: str = Field("", description="晚上安排")
    pois: List[POI] = Field(default_factory=list, description="当日涉及的消费点")
    budget_hint: str = Field("", description="当日预算参考区间")


class Itinerary(BaseModel):
    """完整多日行程。"""
    destination: str = Field(..., description="目的地城市")
    days: List[ItineraryDay] = Field(..., description="按天编排的行程")
    total_budget_hint: str = Field("", description="总预算参考区间")
    notes: List[str] = Field(default_factory=list, description="出行提示")


class MarketingPiece(BaseModel):
    """单条平台化营销内容。"""
    platform: str = Field(..., description="平台：小红书/抖音/公众号/朋友圈/OTA")
    title: str = Field("", description="标题/前3秒钩子")
    body: str = Field(..., description="正文/脚本")
    hashtags: List[str] = Field(default_factory=list, description="话题标签")
    cta: str = Field("", description="转化引导")


class HotelRecommendation(BaseModel):
    """酒店推荐条目。"""
    name: str = Field(..., description="酒店名")
    city: str = Field("", description="所在城市")
    price_per_night: str = Field("", description="每晚价格，须带来源/对冲")
    highlights: List[str] = Field(default_factory=list, description="卖点")
    suitable_for: str = Field("", description="适合客群")


# ==================== Schema → 提示词 ====================

def model_to_json_schema(model: Type[BaseModel]) -> Dict[str, Any]:
    """导出 Pydantic 模型的 JSON Schema（供提示词或 function-calling 工具参数）。"""
    return model.model_json_schema()


def _schema_brief(schema: Dict[str, Any]) -> str:
    return json.dumps(schema, ensure_ascii=False, indent=2)


def build_structured_system_prompt(model: Type[BaseModel], extra: str = "") -> str:
    """构造「只输出 JSON、且严格符合 Schema」的系统提示词。"""
    schema = model_to_json_schema(model)
    return (
        "你是一个严谨的结构化数据生成器。请只输出一个 JSON 对象，不要输出任何解释、"
        "不要使用 Markdown 代码块以外的多余文字。JSON 必须严格符合以下 Schema：\n\n"
        f"{_schema_brief(schema)}\n\n"
        "要求：\n"
        "1. 所有 required 字段必须存在且类型正确；\n"
        "2. 价格/时间类字段必须带来源或对冲语言（如「约/参考/以官方为准」），不得编造精确值；\n"
        "3. 数组无内容时给空数组 []，不要省略字段。\n"
        + (f"\n补充要求：{extra}" if extra else "")
    )


# ==================== JSON 抽取 ====================

def extract_json(text: str) -> Optional[Any]:
    """从模型输出中稳健抽取 JSON：优先 ```json 围栏，其次首个 {..}/[..] 平衡块。"""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    candidates: List[str] = []
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(text.strip())
    # 平衡括号截取
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        i = text.find(open_ch)
        if i >= 0:
            depth = 0
            for j in range(i, len(text)):
                if text[j] == open_ch:
                    depth += 1
                elif text[j] == close_ch:
                    depth -= 1
                    if depth == 0:
                        candidates.append(text[i:j + 1])
                        break
    for c in candidates:
        try:
            return json.loads(c)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


# ==================== 结构化生成 ====================

def _validation_error_text(e: ValidationError) -> str:
    """把 Pydantic 报错压成一句话，回喂给模型自愈。"""
    parts = []
    for err in e.errors()[:6]:
        loc = ".".join(str(x) for x in err.get("loc", []))
        parts.append(f"{loc}: {err.get('msg')}")
    return "; ".join(parts)


def generate_structured(
    model: Type[BaseModel],
    call_llm_fn: Callable[..., Dict[str, Any]],
    user_content: str,
    mode: str = "prompt",
    skill_name: str = "",
    extra_instruction: str = "",
    max_repair: int = 2,
    tool_name: str = "submit_structured_data",
) -> Dict[str, Any]:
    """让 LLM 产出符合 model 的结构化数据。

    Args:
        model: Pydantic 模型类
        call_llm_fn: 签名同 app.call_llm(messages, ..., tools=None, tool_choice=None)
        user_content: 用户需求文本
        mode: "prompt"（提示词约束）或 "tool"（function-calling 强制）
        max_repair: 校验失败后的自愈重试次数
    Returns:
        {data: BaseModel|None, raw: str, errors: [str], attempts: int, mode: str}
    """
    schema = model_to_json_schema(model)
    errors: List[str] = []
    raw = ""
    attempts = 0

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": build_structured_system_prompt(model, extra_instruction)},
        {"role": "user", "content": user_content},
    ]

    tool = None
    if mode == "tool":
        tool = {"type": "function", "function": {
            "name": tool_name,
            "description": f"提交符合 Schema 的 {model.__name__} 结构化数据",
            "parameters": schema,
        }}

    for attempt in range(max_repair + 1):
        attempts += 1
        try:
            if mode == "tool":
                resp = call_llm_fn(messages, tools=[tool],
                                   tool_choice={"type": "function", "function": {"name": tool_name}},
                                   skill_name=skill_name)
            else:
                resp = call_llm_fn(messages, skill_name=skill_name)
        except Exception as e:  # noqa: BLE001
            errors.append(f"call_llm 异常: {e}")
            break

        if not isinstance(resp, dict) or "error" in resp:
            err = (resp or {}).get("error", "未知错误") if isinstance(resp, dict) else "响应非法"
            errors.append(f"LLM 返回错误: {err}")
            # tool 模式失败可降级到 prompt 模式再试一次
            if mode == "tool":
                mode = "prompt"
                tool = None
                continue
            break

        msg = resp["choices"][0]["message"]
        payload: Any = None
        # tool 模式：优先取 tool_calls.arguments
        tcs = msg.get("tool_calls") or []
        if tcs:
            try:
                payload = json.loads(tcs[0]["function"]["arguments"])
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                errors.append(f"tool_call 参数解析失败: {e}")
        if payload is None:
            raw = msg.get("content") or ""
            payload = extract_json(raw)
            if payload is None:
                errors.append("未能从输出中抽取 JSON")

        if payload is not None:
            try:
                data = model.model_validate(payload)
                return {"data": data, "raw": raw or json.dumps(payload, ensure_ascii=False),
                        "errors": errors, "attempts": attempts, "mode": mode}
            except ValidationError as e:
                verr = _validation_error_text(e)
                errors.append(f"校验失败(第{attempts}次): {verr}")
                # 自愈：把报错回喂，要求修正
                messages.append({"role": "assistant", "content": raw or json.dumps(payload, ensure_ascii=False)})
                messages.append({"role": "user", "content":
                    f"上面的 JSON 不符合 Schema，错误：{verr}。请只输出修正后的完整 JSON。"})
                continue

        # payload 为 None 时也进入自愈重试
        messages.append({"role": "user", "content": "你上次没有输出合法 JSON，请只输出符合 Schema 的 JSON 对象。"})

    return {"data": None, "raw": raw, "errors": errors, "attempts": attempts, "mode": mode}
