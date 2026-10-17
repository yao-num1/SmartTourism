# -*- coding: utf-8 -*-
"""
SmartTourism · 标准 MCP Server
===============================
把 agent.tools.ToolRegistry 里的工具，通过 Model Context Protocol 暴露成标准 MCP 服务，
任意 MCP 客户端（Claude Desktop / Cursor / 其它 Agent）都能发现并调用这些文旅工具。

设计：
  - 用 lowlevel Server 直接桥接「已有的动态 JSON Schema 工具」——无需为每个工具重写类型签名；
  - 传输层用 stdio（MCP 最常见方式），可被 MCP 客户端作为子进程拉起；
  - 核心逻辑抽成 _list_tools_impl / _call_tool_impl 两个纯函数，便于脱离传输层单测。

启动（作为 MCP server 供客户端连接）：
    python server/agent/mcp_server.py
在 MCP 客户端配置里：
    {"command": "python", "args": ["<项目路径>/server/agent/mcp_server.py"]}
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any, Dict, List, Optional

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

# ---- 路径设置：让 agent.* / plugins.* / app 可导入 ----
_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVER = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_SERVER)
for _p in (_SERVER, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

logger = logging.getLogger("agent.mcp_server")

SERVER_NAME = "smarttourism"


# ==================== 核心逻辑（纯函数，可单测） ====================

async def _list_tools_impl(tool_registry: Any) -> List[types.Tool]:
    """把 ToolRegistry 里的工具转成 MCP Tool 列表。"""
    tools: List[types.Tool] = []
    for t in tool_registry.list():
        tools.append(types.Tool(
            name=t.name,
            description=t.description,
            inputSchema=t.parameters or {"type": "object", "properties": {}},
        ))
    return tools


async def _call_tool_impl(
    tool_registry: Any,
    name: str,
    arguments: Optional[Dict[str, Any]] = None,
) -> List[types.TextContent]:
    """执行指定工具，把结果包成 MCP TextContent 返回。

    ToolRegistry.call 内部已兜底一切异常并返回可读错误字符串，因此这里不会抛。
    """
    result = tool_registry.call(name, arguments or {})
    return [types.TextContent(type="text", text=result)]


# ==================== 组装 MCP Server ====================

def build_server(tool_registry: Any) -> Server:
    """用一个 ToolRegistry 构造标准 MCP lowlevel Server。"""
    server: Server = Server(SERVER_NAME)

    @server.list_tools()
    async def handle_list_tools() -> List[types.Tool]:
        return await _list_tools_impl(tool_registry)

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict) -> List[types.TextContent]:
        return await _call_tool_impl(tool_registry, name, arguments)

    return server


# ==================== 真实接线（延迟导入 app） ====================

def build_registry_from_app(only_configured: bool = True):
    """从 app.py 的真实插件注册中心构造 ToolRegistry。"""
    from app import registry  # type: ignore
    from agent.tools import build_registry_from_plugins  # type: ignore
    return build_registry_from_plugins(registry, only_configured=only_configured)


async def _run_stdio(server: Server) -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    """作为 stdio MCP server 启动（供 MCP 客户端连接）。"""
    logging.basicConfig(level=logging.INFO)
    tool_registry = build_registry_from_app()
    logger.info("MCP server 启动，暴露 %d 个工具: %s",
                len(tool_registry), tool_registry.names())
    server = build_server(tool_registry)
    asyncio.run(_run_stdio(server))


if __name__ == "__main__":
    main()
