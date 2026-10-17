# -*- coding: utf-8 -*-
"""
SmartTourism · Tool / Function-Calling 层
==========================================
把现有插件（BasePlugin）与业务能力统一抽象成「带 JSON Schema 的工具」，
供三处复用：① OpenAI 风格 function calling ② LangGraph 编排 ③ MCP Server。

核心构件：
  - Tool             单个工具（name / description / parameters(JSON Schema) / handler）
  - ToolRegistry     工具注册中心：注册、列举、生成 OpenAI tools 数组、按名执行
  - tool_from_plugin 把 BasePlugin 适配成 Tool（参数 Schema 按插件真实契约声明）
  - run_function_calling  标准 function-calling 循环：
        LLM 返回 tool_calls → 执行工具 → 以 role=tool 回填 → 再问 LLM → 直到给出最终答复

设计：本模块不 import app.py，通过参数注入 call_llm_fn / plugin_registry，
因此可用 mock 完整单测，也不会与现有代码形成循环依赖。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("agent.tools")


# ==================== Tool 抽象 ====================

@dataclass
class Tool:
    """一个可被大模型调用的工具。

    Attributes:
        name:        工具唯一标识（需匹配 ^[a-zA-Z0-9_-]+$，OpenAI 要求）
        description: 给模型看的自然语言说明——模型据此决定何时调用，务必写清用途与返回
        parameters:  JSON Schema（type=object），描述入参
        handler:     实际执行函数，签名 handler(**arguments) -> str | dict
    """
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Callable[..., Any]
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_openai_schema(self) -> Dict[str, Any]:
        """转成 OpenAI /chat/completions 的 tools[] 元素（国产模型均兼容此格式）。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# 未知插件的兜底 Schema：只暴露一个自由文本 query，保证不报错
_GENERIC_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "查询内容"},
    },
    "required": [],
}


# ==================== 工具注册中心 ====================

class ToolRegistry:
    """工具注册中心：注册 / 查询 / 生成 schema / 按名执行。"""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    # ---- 注册 ----
    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            logger.debug("工具覆盖注册: %s", tool.name)
        self._tools[tool.name] = tool

    def register_function(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        handler: Callable[..., Any],
        **metadata: Any,
    ) -> Tool:
        """便捷注册：直接由函数构造 Tool。"""
        tool = Tool(name=name, description=description, parameters=parameters,
                    handler=handler, metadata=metadata)
        self.register(tool)
        return tool

    # ---- 查询 ----
    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return list(self._tools.keys())

    def list(self) -> List[Tool]:
        return list(self._tools.values())

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    # ---- 供模型使用 ----
    def openai_schemas(self) -> List[Dict[str, Any]]:
        """生成传给 call_llm(tools=...) 的完整 tools 数组。"""
        return [t.to_openai_schema() for t in self._tools.values()]

    # ---- 执行 ----
    def call(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        """执行工具并把结果序列化成字符串（供回填给模型）。

        任何异常都被捕获并转成可读的错误字符串——让模型看到失败原因后自行恢复，
        而不是让整个请求崩溃。
        """
        arguments = arguments or {}
        tool = self._tools.get(name)
        if tool is None:
            return json.dumps({"error": f"未知工具: {name}",
                               "available": self.names()}, ensure_ascii=False)
        try:
            result = tool.handler(**arguments)
        except TypeError as e:
            # 入参与 handler 签名不匹配
            logger.warning("工具 %s 入参错误: %s (arguments=%s)", name, e, arguments)
            return json.dumps({"error": f"工具 {name} 入参不正确: {e}"}, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001 — 工具执行需兜底一切异常
            logger.exception("工具 %s 执行失败", name)
            return json.dumps({"error": f"工具 {name} 执行失败: {e}"}, ensure_ascii=False)
        return _serialize(result)


def _serialize(result: Any) -> str:
    """把工具返回值统一序列化成字符串。"""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(result)


# ==================== 插件 → 工具 适配 ====================
#
# 每个 Schema 严格对应插件 execute(params) 的真实读取键（见 plugins/*.py），
# 让模型给出的参数能被插件直接消费。

PLUGIN_TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "local_knowledge": {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "目的地城市，如 杭州 / 三亚 / 成都"},
            "type": {"type": "string",
                     "enum": ["hotels", "spots", "food", "transport", "all"],
                     "description": "查询类别：酒店/景点/美食/交通/全部，默认 all"},
        },
        "required": ["city"],
    },
    "amap_weather": {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "城市名，如 杭州"},
            "extensions": {"type": "string", "enum": ["base", "all"],
                           "description": "base=实况天气，all=实况+预报，默认 all"},
        },
        "required": ["city"],
    },
    "weather": {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "城市名"}},
        "required": ["city"],
    },
    "amap_poi": {
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "POI 关键词，如 西湖 / 酒店 / 火锅"},
            "city": {"type": "string", "description": "所在城市，默认 北京"},
        },
        "required": ["keyword"],
    },
    "amap_map": {
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["regeo", "around", "direction", "ip_location"],
                       "description": "regeo=逆地理编码, around=周边搜索, direction=路线规划, ip_location=IP定位"},
            "lng": {"type": "string", "description": "经度"},
            "lat": {"type": "string", "description": "纬度"},
            "city": {"type": "string", "description": "城市名"},
            "keywords": {"type": "string", "description": "周边搜索关键词，如 景点|酒店|餐饮"},
            "radius": {"type": "integer", "description": "搜索半径(米)，默认 3000"},
            "origin": {"type": "string", "description": "路线起点，格式 lng,lat"},
            "destination": {"type": "string", "description": "路线终点，格式 lng,lat"},
            "mode": {"type": "string", "enum": ["driving", "walking", "transit", "bicycling"],
                     "description": "出行方式，默认 driving"},
        },
        "required": ["action"],
    },
    "meituan": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "查询内容，如 火锅 / 亲子酒店 / 故宫门票"},
            "city": {"type": "string", "description": "城市名，默认 北京"},
            "channel": {"type": "string", "description": "数据渠道，默认 default"},
        },
        "required": ["query"],
    },
    "travel_cost": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["ticket", "route"],
                       "description": "ticket=景点门票价格, route=出行交通费用；不填则自动推断"},
            "city": {"type": "string", "description": "城市名"},
            "spot": {"type": "string", "description": "景点名（查门票时用）"},
            "origin": {"type": "string", "description": "路线起点"},
            "destination": {"type": "string", "description": "路线终点"},
            "mode": {"type": "string", "enum": ["driving", "walking", "transit", "bicycling"],
                     "description": "出行方式"},
        },
        "required": [],
    },
    "fliggy": {
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "搜索关键词"},
            "city": {"type": "string", "description": "城市名"},
            "category": {"type": "string", "description": "类别，默认 all"},
            "limit": {"type": "integer", "description": "返回条数，默认 5"},
        },
        "required": ["keyword"],
    },
}


def tool_from_plugin(plugin: Any, only_if_configured: bool = True) -> Optional[Tool]:
    """把一个 BasePlugin 适配成 Tool。

    Args:
        plugin: BasePlugin 实例（需有 name / description / execute / format_for_llm）
        only_if_configured: 若为 True 且插件未配置好(缺 API Key)，则跳过（返回 None）——
                            避免把注定失败的工具暴露给模型。

    Returns:
        Tool 或 None
    """
    if only_if_configured and hasattr(plugin, "is_configured") and not plugin.is_configured():
        logger.debug("跳过未配置插件: %s", getattr(plugin, "name", "?"))
        return None

    name = plugin.name
    description = getattr(plugin, "description", "") or getattr(plugin, "display_name", name)
    schema = PLUGIN_TOOL_SCHEMAS.get(name, _GENERIC_SCHEMA)

    def _handler(**kwargs: Any) -> str:
        result = plugin.execute(kwargs)
        # 插件自带 format_for_llm；无则退回通用序列化
        if hasattr(plugin, "format_for_llm"):
            return plugin.format_for_llm(result)
        return _serialize(result)

    return Tool(
        name=name,
        description=f"{description}。返回实时/结构化数据，请严格基于返回内容作答，不得编造。",
        parameters=schema,
        handler=_handler,
        metadata={"source": "plugin", "display_name": getattr(plugin, "display_name", name)},
    )


def build_tools_from_plugins(
    plugin_registry: Any,
    only_configured: bool = True,
) -> List[Tool]:
    """遍历插件注册中心，把所有（已配置的）插件转成 Tool 列表。"""
    tools: List[Tool] = []
    plugins = plugin_registry.list_all() if hasattr(plugin_registry, "list_all") else []
    for plugin in plugins:
        tool = tool_from_plugin(plugin, only_if_configured=only_configured)
        if tool is not None:
            tools.append(tool)
    return tools


def build_registry_from_plugins(
    plugin_registry: Any,
    only_configured: bool = True,
) -> ToolRegistry:
    """便捷函数：直接得到一个装好插件工具的 ToolRegistry。"""
    reg = ToolRegistry()
    for tool in build_tools_from_plugins(plugin_registry, only_configured):
        reg.register(tool)
    return reg


# ==================== Function-Calling 执行循环 ====================

def run_function_calling(
    messages: List[Dict[str, Any]],
    call_llm_fn: Callable[..., Dict[str, Any]],
    tool_registry: ToolRegistry,
    max_iters: int = 5,
    skill_name: str = "",
    on_tool_call: Optional[Callable[[str, Dict[str, Any], str], None]] = None,
) -> Dict[str, Any]:
    """标准 function-calling 循环（provider 无关）。

    流程：
      1. 带上 tools schema 调 LLM；
      2. 若返回 tool_calls：逐个执行工具，把 assistant(含tool_calls) 与每条 role=tool 结果追加进消息；
      3. 回到 1 继续问，直到模型不再要求调用工具、给出最终自然语言答复；
      4. 超过 max_iters 强制收尾，防止死循环。

    Args:
        messages:       初始消息列表（OpenAI 格式）
        call_llm_fn:    LLM 调用函数，签名需支持
                        call_llm_fn(messages, tools=..., tool_choice=..., skill_name=...)
                        并返回 {"choices":[{"message":{...}}]} 或 {"error": ...}
        tool_registry:  工具注册中心
        max_iters:      最大「模型↔工具」往返轮数
        skill_name:     透传给 call_llm_fn 用于动态 temperature
        on_tool_call:   可选回调 (tool_name, arguments, result_str)，用于追踪/流式提示

    Returns:
        {
          "content":    最终答复文本,
          "trace":      [{"tool","arguments","result_preview"}...],
          "iterations": 实际往返轮数,
          "tool_calls": 累计工具调用次数,
        }
        失败时返回 {"error": "...", "trace": [...]}
    """
    schemas = tool_registry.openai_schemas()
    msgs: List[Dict[str, Any]] = list(messages)
    trace: List[Dict[str, Any]] = []
    total_tool_calls = 0

    for i in range(max_iters):
        if schemas:
            resp = call_llm_fn(msgs, tools=schemas, tool_choice="auto", skill_name=skill_name)
            # 某些模型（如 deepseek-reasoner）不支持 function calling：降级为无工具再试一次
            if isinstance(resp, dict) and "error" in resp:
                logger.warning("带工具调用失败，降级为无工具: %s", resp.get("error"))
                resp = call_llm_fn(msgs, skill_name=skill_name)
        else:
            resp = call_llm_fn(msgs, skill_name=skill_name)

        if not isinstance(resp, dict) or "error" in resp:
            err = resp.get("error") if isinstance(resp, dict) else "LLM 返回异常"
            return {"error": err, "trace": trace}

        message = resp["choices"][0]["message"]
        tool_calls = message.get("tool_calls")

        # 模型没有要求调用工具 → 最终答复
        if not tool_calls:
            return {
                "content": message.get("content", "") or "",
                "trace": trace,
                "iterations": i + 1,
                "tool_calls": total_tool_calls,
            }

        # 把带 tool_calls 的 assistant 消息原样追加（协议要求随后必须跟对应的 role=tool 结果）
        msgs.append(message)

        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            raw_args = fn.get("arguments", "{}")
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except json.JSONDecodeError:
                logger.warning("工具参数非法 JSON: %s -> %r", name, raw_args)
                arguments = {}

            result_str = tool_registry.call(name, arguments)
            total_tool_calls += 1
            trace.append({
                "tool": name,
                "arguments": arguments,
                "result_preview": result_str[:200],
            })
            if on_tool_call is not None:
                try:
                    on_tool_call(name, arguments, result_str)
                except Exception:  # noqa: BLE001 — 回调失败不应中断主流程
                    logger.exception("on_tool_call 回调异常")

            msgs.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "name": name,
                "content": result_str,
            })

    # 超出最大轮数
    logger.warning("function-calling 达到最大轮数 %s，强制收尾", max_iters)
    return {
        "content": "（工具调用轮数过多，已中止。请简化问题后重试。）",
        "trace": trace,
        "iterations": max_iters,
        "tool_calls": total_tool_calls,
        "truncated": True,
    }
